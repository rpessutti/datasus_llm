from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import unicodedata
import time
from pathlib import Path
from typing import Any

# Adiciona o diretorio raiz do projeto ao sys.path para permitir execucao direta
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import duckdb

from config.settings import PROJECT_DIR, DATASUS_DATA_DIR
from config.constants import DIAGNOSIS_COLUMN_NAMES, DEFAULT_FETCH_ROWS, FILE_METADATA_COLUMNS
from core.io_utils import _discover_files_in, _retry_copyfile, _retry_stat, _sync_to_official_cache
from core.logging_config import log_calls
from core.path_utils import _grouped_cache_path, _schema_rows, _migrate_legacy_cache_paths, _extract_period_from_path
from db.group_cache import _GroupCache
from db.column_dictionary import ColumnDictionaryStore, persist_to_duckdb
from db.period_fields import (
    PERIOD_FIELDS,
    ATEND_ANO,
    ATEND_MES,
    ano_expr,
    ano_int_expr,
    mes_expr,
)

LOGGER = logging.getLogger("datasus_llm")

# ============================================================================
# MODO GRUPOS E VIEW UNIFICADA (UNION) - decisoes #3/#4/#5 (documentacao PARTE 8)
# ----------------------------------------------------------------------------
# Cada grupo do DATASUS (er, si, ps, sp, ...) vive em seu proprio banco anexado
# (<grupo>.datasus_cache). A pergunta e respondida sobre a view unificada
# `datasus_query_cache`, um UNION ALL dos grupos selecionados.
#
# #3 - Colunas de periodo MATERIALIZADAS na ingestao: atend_ano/atend_mes entram
#      como colunas FISICAS de cada grupo (derivadas do campo NATIVO de periodo),
#      evitando parsing/derivacao em tempo de consulta.
# #4 - View unificada ALINHADA ao campo NATIVO de periodo de cada grupo: o UNION
#      usa o campo de periodo real do grupo (nao um derivado por nome de arquivo),
#      para que o recorte temporal corresponda ao atendimento.
# #5 - Grupos SEM campo de periodo sao EXCLUIDOS do UNION: evita colunas de
#      periodo nulas/ambiguas no UNION ALL.
# ============================================================================


# Nomes reservados que NAO sao grupos (banco principal, fallback e a pasta raiz
# de Parquet, que costuma ficar dentro de CACHE_DIR).
_RESERVED_GROUP_NAMES = {"datasus", "datasus_fallback", "parquet", "cache", "tmp"}


def _is_valid_group_name(name: str) -> bool:
    """True se ``name`` pode ser usado com seguranca como nome de grupo/schema.

    Um nome de grupo e usado tanto como identificador SQL (schema anexado:
    ``ATTACH ... AS <name>``) quanto como nome de arquivo ``<name>.duckdb``.
    Nomes com caracteres como ``[`` ``]`` ``"`` ``'`` ``(`` ``)`` ``;`` espacos
    etc. quebram a geracao de SQL e derrubavam a inicializacao com
    ``ParserException`` (ex.: o grupo espurio ``datasus[1]``).

    Regra: aceita apenas identificadores seguros (letras, digitos e ``_``,
    comecando por letra ou ``_``) e rejeita nomes reservados.
    """
    if not name:
        return False
    if name in _RESERVED_GROUP_NAMES:
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


class DatasusCache:
    def __init__(self, settings: Any) -> None:
        started = time.perf_counter()
        self.settings = settings
        self._ingest_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_error: str | None = None
        self.groups: dict[str, _GroupCache] = {}
        # >>> FASE 3 (S2): CACHE DO SCHEMA CANONICO <<<
        # Guarda {safe_name: set(colunas)} e a uniao de todas as colunas dos
        # grupos. Sem isso, prepare_question_cache faz DESCRIBE em cada um dos
        # 16 grupos E recomputa a uniao de ~469 colunas A CADA pergunta. O
        # conteudo e estavel durante a execucao (so muda com nova ingestao).
        self._canonical_schema_cache: dict[str, Any] | None = None
        # >>> PARTE 7 (#9): ultima SQL preparada (p/ reprojecao minima). <<<
        self._last_sql: str | None = None
        # >>> FASE 3 (S3): VIEW UNIFICADA (lazy) em vez de TEMP TABLE <<<
        # Quando True, datasus_query_cache e criada como VIEW (nao materializada),
        # permitindo push-down do filtro e leitura lazy. Reversivel via
        # DATASUS_UNIFIED_VIEW=false.
        self._unified_view_enabled = (
            os.getenv("DATASUS_UNIFIED_VIEW", "false").casefold() in {"1", "true", "yes", "on"}
        )
        self._db_path = settings.database_path
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        # ESTRATEGIA NOVA: cache DuckDB e SEMPRE local (settings.cache_dir).
        LOGGER.info("duckdb open start path=%s", self._db_path)
        try:
            self.connection = duckdb.connect(str(self._db_path))
            self._configure_memory()
        except Exception as e:
            LOGGER.error("falha ao abrir DuckDB em %s: %s", self._db_path, e)
            alt_path = self.settings.cache_dir / "datasus_fallback.duckdb"
            try:
                self.connection = duckdb.connect(str(alt_path))
                self._db_path = alt_path
                self._configure_memory()
                LOGGER.warning("usando banco fallback em %s", alt_path)
            except Exception as e2:
                LOGGER.error("falha crítica ao abrir DuckDB: %s", e2)
                raise

        try:
            self._discover_groups()
        except Exception as e:
            LOGGER.error("erro ao descobrir grupos: %s", e)
            self.groups = {}

        # ANEXA os bancos DuckDB dos grupos como schemas na conexao principal.
        if self.groups:
            for name, group in self.groups.items():
                try:
                    safe_name = name.replace('"', '""')
                    if group.db_path and group.db_path.exists():
                        try:
                            db_check = self.connection.execute(
                                "SELECT COUNT(*) FROM duckdb_databases() WHERE database_name = ?",
                                [safe_name]
                            ).fetchone()[0]
                        except Exception:
                            db_check = 0

                        if db_check > 0:
                            LOGGER.info("grupo %s: banco %s já anexado como schema %s", name, group.db_path, safe_name)
                        else:
                            try:
                                self.connection.execute(f"ATTACH '{group.db_path}' AS {safe_name} (READ_WRITE)")
                                LOGGER.info("grupo %s: anexado como schema %s (READ_WRITE)", name, safe_name)
                            except Exception as e:
                                LOGGER.warning("grupo %s: falha ao anexar READ_WRITE: %s; tentando READ_ONLY", name, e)
                                try:
                                    self.connection.execute(f"ATTACH '{group.db_path}' AS {safe_name} (READ_ONLY)")
                                    LOGGER.info("grupo %s: anexado como schema %s (READ_ONLY)", name, safe_name)
                                except Exception as e2:
                                    LOGGER.warning("grupo %s: falha ao anexar READ_ONLY: %s", name, e2)
                    else:

                        # >>> CORRECAO: cria o banco do grupo e ANEXA agora, em vez de
                        # deixar para "sob demanda" (que nao acontecia). Sem o ATTACH,
                        # as queries qualificadas (ex.: pa.file_manifest) falhavam com
                        # "Catalog Error: Table with name file_manifest does not exist!".
                        # NUNCA apaga arquivos .duckdb: apenas cria e anexa.
                        try:
                            # NAO abre uma conexao temporaria so para materializar o
                            # arquivo: isso adquire um lock momentaneo no <grupo>.duckdb
                            # que pode colidir com a conexao principal
                            # (IOException: Could not set lock on file ...). O ATTACH
                            # abaixo cria o arquivo se ele nao existir.
                            group.db_path.parent.mkdir(parents=True, exist_ok=True)
                            self.connection.execute(
                                f"ATTACH IF NOT EXISTS '{group.db_path}' AS {safe_name} (READ_WRITE)"
                            )
                            LOGGER.info("grupo %s: banco %s criado e anexado como schema %s",
                                        name, group.db_path, safe_name)
                        except Exception as e:
                            # >>> CORRECAO: falha de ATTACH nao deve ficar mascarada em
                            # WARNING. Sem o schema anexado, qualquer query qualificada
                            # (<grupo>.<tabela>) falha com "Catalog Error: Schema with name
                            # <grupo> does not exist!". Registra como ERROR para
                            # diagnostico e tambem marca o grupo para (re)ingestao.
                            LOGGER.error("grupo %s: falha ao criar/anexar banco %s: %s",
                                         name, group.db_path, e, exc_info=True)
                except Exception as e:
                    # >>> CORRECAO: idem — erro de anexacao do grupo em nivel ERROR.
                    LOGGER.error("falha ao anexar grupo %s: %s", name, e, exc_info=True)
        try:
            self.connection.execute("CREATE TABLE IF NOT EXISTS file_manifest (source VARCHAR PRIMARY KEY, size BIGINT, modified DOUBLE, cached VARCHAR, in_db INTEGER DEFAULT 0)")
        except Exception as e:
            LOGGER.warning("erro ao criar file_manifest: %s", e)
        # Migracao: adiciona a coluna in_db caso a tabela ja exista sem ela.
        manifest_cols = {row[0] for row in self.connection.execute("DESCRIBE file_manifest").fetchall()}
        if "in_db" not in manifest_cols:
            LOGGER.info("Migração: adicionando coluna in_db ao file_manifest")
            self.connection.execute("ALTER TABLE file_manifest ADD COLUMN in_db INTEGER DEFAULT 0")
            table_exists = self._fetch_scalar(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name = 'datasus_cache'",
                default=0,
            )
            if table_exists:
                self.connection.execute("UPDATE file_manifest SET in_db = 1 WHERE in_db = 0")
        self.connection.execute("CREATE TABLE IF NOT EXISTS cache_metadata (key VARCHAR PRIMARY KEY, value BIGINT)")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS parquet_period_index ("
            "path VARCHAR PRIMARY KEY, parquet_year VARCHAR, parquet_month VARCHAR)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS cache_checkpoint ("
            "key VARCHAR PRIMARY KEY, value VARCHAR)"
        )
        # >>> PARTE 6 (#7): PRE-COMPUTACAO DE PREFIXOS DE CID <<<
        # `_known_cid_prefixes` fazia DESCRIBE + SELECT DISTINCT SUBSTR por
        # coluna de diagnostico de cada grupo A CADA REQUISICAO. Agora os
        # prefixos sao pre-computados na ingestao nesta tabela e apenas LIDOS
        # nas consultas subsequentes. Idempotente (PRIMARY KEY + INSERT OR IGNORE).
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS cid_prefixes ("
            "prefixo VARCHAR PRIMARY KEY)"
        )
        # NOVO: carga UNICA do CID_10.CSV no DuckDB (tabela cid10).
        # A LLM consulta esta tabela para decidir o CID, em vez de o Python
        # ler o CSV a cada pergunta. Idempotente: so carrega se a tabela nao
        # existir ou estiver vazia.
        try:
            self._load_cid10_table()
        except Exception as e:
            LOGGER.warning("falha ao carregar tabela cid10 no DuckDB: %s", e)
        LOGGER.info("duckdb open complete duration_ms=%.2f fallback=%s",
                    (time.perf_counter() - started) * 1000,
                    self._db_path != settings.database_path)

        # >>> NOVO: Construir caches de colunas por grupo (persistidos, uma vez) <<<
        try:
            self._columns_cache = self._build_all_grupo_columns_caches()
        except Exception as e:
            LOGGER.error("erro ao construir caches de colunas: %s", e)
            self._columns_cache = {}

        # >>> NOVO: Dicionario consolidado de colunas (em memoria + opcional em DuckDB) <<<
        # Consolida os caches por grupo em uma estrutura indexada (coluna->grupo,
        # coluna->tipo, grupo->colunas, conceito->colunas) reutilizada a cada
        # requisicao, evitando recomputar uniao/dedup a cada pergunta.
        self._column_dict_store = ColumnDictionaryStore()
        self._column_dictionary = None
        try:
            self._column_dictionary = self._column_dict_store.get(self._columns_cache)
            persist_to_duckdb(self.connection, self._column_dictionary)
        except Exception as e:
            LOGGER.warning("erro ao inicializar dicionário de colunas: %s", e)

        # >>> NOVO: registra a conexao principal no cid_map, para que o de-para
        # de CID-10 passe a ser resolvido consultando a tabela ``cid10`` do
        # DuckDB (carregada uma unica vez), em vez de reler o CSV em Python.
        try:
            from db.cid_map import set_cid_connection
            set_cid_connection(self.connection)
        except Exception as e:
            LOGGER.debug("não foi possível registrar conexão no cid_map: %s", e)

    @property
    def database_path(self) -> Path:
        """Caminho do banco DuckDB principal."""
        return self._db_path

    @database_path.setter
    def database_path(self, value: Path) -> None:
        self._db_path = value

    def _configure_memory(self) -> None:
        """Configura a memoria do DuckDB (centralizada em Settings)."""
        try:
            memory_limit = getattr(self.settings, "memory_limit", "1GB")
            threads = int(getattr(self.settings, "threads", 4))
            temp_dir = getattr(self.settings, "temp_dir", Path("/tmp"))
            # Permite override por variavel de ambiente
            memory_limit = os.getenv("DATASUS_MEMORY_LIMIT", memory_limit)
            threads = int(os.getenv("DATASUS_THREADS", str(threads)))
            self.connection.execute(f"SET memory_limit='{memory_limit}'")
            self.connection.execute(f"SET threads={threads}")
            self.connection.execute("SET enable_progress_bar=false")
            # Nota: no DuckDB, temp_directory nao usa aspas simples, mas o caminho
            self.connection.execute(f"SET temp_directory='{temp_dir}'")
            # >>> OTIMIZACAO OOM (alivio de memoria sem perder velocidade) <<<
            # 1) preserve_insertion_order=false: libera o DuckDB de manter a ordem
            #    de insercao em agregacoes/UNION grandes. O resultado final continua
            #    correto (as queries usam ORDER BY explicito quando precisam de
            #    ordenacao). Reduz bastante o estado em memoria.
            try:
                self.connection.execute("SET preserve_insertion_order=false")
            except Exception:
                pass
            # 2) Espill do operador de hash/agregacao limitado para nao encher /tmp:
            #    o DuckDB descarrega operacoes intermediarias para temp_directory.
            try:
                self.connection.execute("SET max_temp_directory_size='60GB'")
            except Exception:
                pass
        except Exception as e:
            LOGGER.warning("erro ao configurar memória do DuckDB: %s", e)

    @staticmethod
    def _referenced_columns(sql: str, available: set[str]) -> set[str]:
        """Extrai (best-effort) as colunas da view referenciadas em ``sql``.

        >>> OTIMIZACAO OOM: em vez de projetar TODAS as colunas unificadas
        (~469) em cada um dos 16 grupos, projetamos apenas as que a consulta
        realmente usa (mais as colunas de periodo). Isso reduz drasticamente
        o volume materializado pelo UNION ALL e o consumo de memoria, sem
        alterar o resultado. Se a extracao nao encontrar nada (ex.: SELECT *),
        o chamador deve cair para o conjunto completo.
        """
        if not sql:
            return set()
        if re.search(r"\bSELECT\s+\*", sql, flags=re.IGNORECASE):
            return set()
        referenced: set[str] = set()
        # Colunas citadas entre aspas duplas.
        for match in re.findall(r'"([^"]+)"', sql):
            referenced.add(match)
        # Identificadores "soltos" (sem aspas) que coincidam com colunas existentes.
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql):
            if token in available or token.upper() in available:
                referenced.add(token)
        # Mantem apenas as que existem de fato no conjunto disponivel (case-insensitive).
        upper_map = {c.upper(): c for c in available}
        return {upper_map[c.upper()] for c in referenced if c.upper() in upper_map}

    def _ensure_groups_attached(self) -> None:
        """Garante que os bancos dos grupos estejam anexados na conexao principal.

        Antes, esse ATTACH sob demanda ficava embutido no laço de montagem do
        UNION em ``prepare_question_cache``, o que exigia reconsultar o catalogo
        (``duckdb_databases``/``duckdb_tables``/``duckdb_views``) POR GRUPO A
        CADA PERGUNTA. Aqui ele e isolado e so faz o necessario: para cada grupo
        cujo banco ainda nao esta anexado, tenta o ATTACH (READ_WRITE, com
        fallback READ_ONLY). A existencia de ``datasus_cache`` (TABELA ou VIEW)
        NAO e reconsultada aqui — ela vem do cache S2 (ver
        :meth:`_get_canonical_schema`).

        Chamado por ``prepare_question_cache`` antes de ``_get_canonical_schema``
        para manter o comportamento de "attach sob demanda" sem o custo de
        reconsultas de catalogo por grupo.
        """
        for name, group in self.groups.items():
            safe_name = name.replace('"', '""')
            try:
                db_check = self.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_databases() WHERE database_name = ?",
                    [safe_name]
                ).fetchone()
                if db_check and db_check[0] > 0:
                    continue  # já anexado
                try:
                    self.connection.execute(
                        f"ATTACH IF NOT EXISTS '{group.db_path}' AS {safe_name} (READ_WRITE)"
                    )
                except duckdb.IOException as lock_exc:
                    if "Could not set lock" in str(lock_exc):
                        LOGGER.error(
                            "grupo %s: arquivo %s BLOQUEADO por outro processo "
                            "(conflicting lock). Verifique com `lsof %s`. "
                            "Tentando READ_ONLY...",
                            name, group.db_path, group.db_path,
                        )
                    self.connection.execute(
                        f"ATTACH IF NOT EXISTS '{group.db_path}' AS {safe_name} (READ_ONLY)"
                    )
                LOGGER.info("grupo %s: anexado como banco %s (sob demanda)", name, safe_name)
            except Exception as attach_exc:
                LOGGER.warning("grupo %s: falha ao anexar banco: %s", name, attach_exc)

    def _get_canonical_schema(self) -> tuple[set[str], dict[str, set[str]]]:
        """FASE 3 (S2): cache do schema canonico dos grupos.

        Sem isso, ``prepare_question_cache`` roda ``DESCRIBE`` em cada um dos
        ~16 grupos anexados E recomputa a uniao de ~469 colunas A CADA pergunta.

        O conteudo e estavel durante a execucao (so muda com nova ingestao /
        (re)attach de grupo), entao memoizamos em ``self._canonical_schema_cache``.
        Invalide com :meth:`_invalidate_canonical_schema` apos mudancas estruturais.

        Retorna (all_columns, group_columns) onde:
          * all_columns: uniao de colunas de todos os grupos (+ atend_ano/atend_mes);
          * group_columns: {safe_name: set(colunas)} por grupo.
        """
        cached = self._canonical_schema_cache
        if cached is not None:
            return cached["all_columns"], cached["group_columns"]

        # Descobre os grupos com datasus_cache presente (TABELA ou VIEW).
        group_tables: list[str] = []
        _safe_to_name: dict[str, str] = {}
        for name, group in self.groups.items():
            safe_name = name.replace('"', '""')
            try:
                db_ok = self.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_databases() WHERE database_name = ?",
                    [safe_name]
                ).fetchone()
                if not db_ok or db_ok[0] == 0:
                    continue
                rel_exists = self.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_tables() "
                    "WHERE database_name = ? AND table_name = 'datasus_cache'",
                    [safe_name]
                ).fetchone()[0]
                if not rel_exists:
                    rel_exists = self.connection.execute(
                        "SELECT COUNT(*) FROM duckdb_views() "
                        "WHERE database_name = ? AND view_name = 'datasus_cache'",
                        [safe_name]
                    ).fetchone()[0]
                if rel_exists:
                    group_tables.append(safe_name)
                    _safe_to_name[safe_name] = name
            except Exception as e:
                LOGGER.warning("grupo %s: não foi possível inspecionar schema (S2): %s", name, e)

        all_columns: set[str] = set()
        group_columns: dict[str, set[str]] = {}
        for safe_name in group_tables:
            try:
                cols = {
                    row[0]
                    for row in self.connection.execute(
                        f"DESCRIBE {safe_name}.datasus_cache"
                    ).fetchall()
                }
            except Exception as e:
                LOGGER.warning("grupo %s: falha no DESCRIBE (S2): %s", safe_name, e)
                continue
            # Metadados de arquivo NAO entram na view (nao sao dados do DATASUS).
            cols.discard('filename')
            cols.discard('parquet_path')
            cols.discard('parquet_year')
            cols.discard('parquet_month')
            group_columns[safe_name] = cols
            all_columns.update(cols)

        # Colunas derivadas de periodo expostas pela view unificada.
        all_columns.add(ATEND_ANO)
        all_columns.add(ATEND_MES)

        self._canonical_schema_cache = {
            "all_columns": all_columns,
            "group_columns": group_columns,
            "safe_names": group_tables,
            "safe_to_name": {safe: name for safe, name in _safe_to_name.items()},
        }
        LOGGER.info(
            "S2 schema canônico computado: grupos=%d colunas=%d",
            len(group_tables), len(all_columns),
        )
        return all_columns, group_columns

    def _invalidate_canonical_schema(self) -> None:
        """FASE 3 (S2): descarta o cache de schema canonico.

        Chamar apos qualquer mudanca estrutural (nova ingestao, attach/detach
        de grupo, rebuild de tabelas) para que a proxima pergunta recompute.
        """
        if self._canonical_schema_cache is not None:
            LOGGER.info("S2 cache de schema canônico invalidado")
        self._canonical_schema_cache = None

    def _load_cid10_table(self) -> None:
        """Carrega o CID_10.CSV no DuckDB como a tabela ``cid10`` (UMA vez).

        A tabela fica no schema ``main`` e e consultada pela LLM para decidir
        qual CID usar ANTES de montar o SQL final, em vez de o Python ler o
        CSV a cada pergunta. Idempotente: se a tabela ja existir com linhas,
        nao recarrega.

        Colunas (cabecalho REAL do CSV, separador ';', encoding latin-1):
            SUBCAT;CLASSIF;RESTRSEXO;CAUSAOBITO;DESCRICAO;DESCRABREV;REFER;EXCLUIDOS;
        """
        # 1) Ja existe? Entao nao recarrega (carga UNICA).
        try:
            existing = self._fetch_scalar(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name = 'cid10'",
                default=0,
            )
            if existing:
                rows = self._fetch_scalar("SELECT COUNT(*) FROM cid10", default=0)
                if rows and int(rows) > 0:
                    LOGGER.info("tabela cid10 já carregada no DuckDB linhas=%s", rows)
                    return
        except Exception as e:
            LOGGER.debug("checagem da tabela cid10 falhou: %s", e)

        csv_path = PROJECT_DIR / "CID_10.CSV"
        if not csv_path.exists():
            LOGGER.warning("CID_10.CSV não encontrado em %s; tabela cid10 não sera criada.", csv_path)
            return

        # 2) Carrega via read_csv do DuckDB, especificando o cabecalho REAL.
        #    Usa encoding latin-1 (o arquivo e ISO-8859-1).
        path_sql = "'" + str(csv_path).replace("'", "''") + "'"
        self.connection.execute("DROP TABLE IF EXISTS cid10")
        self.connection.execute(
            "CREATE TABLE cid10 AS "
            "SELECT "
            "  UPPER(TRIM(CAST(SUBCAT AS VARCHAR)))   AS subcat, "
            "  CAST(CLASSIF AS VARCHAR)               AS classif, "
            "  CAST(RESTRSEXO AS VARCHAR)             AS restrsexo, "
            "  CAST(CAUSAOBITO AS VARCHAR)            AS causaobito, "
            "  TRIM(CAST(DESCRICAO AS VARCHAR))       AS descricao, "
            "  TRIM(CAST(DESCRABREV AS VARCHAR))      AS descabrev, "
            "  CAST(REFER AS VARCHAR)                 AS refer, "
            "  CAST(EXCLUIDOS AS VARCHAR)             AS excluidos "
            "FROM read_csv(" + path_sql + ", delim=';', header=true, "
            "  encoding='latin-1', quote='', escape='', "
            "  columns={"
            "    'SUBCAT': 'VARCHAR', 'CLASSIF': 'VARCHAR', 'RESTRSEXO': 'VARCHAR', "
            "    'CAUSAOBITO': 'VARCHAR', 'DESCRICAO': 'VARCHAR', 'DESCRABREV': 'VARCHAR', "
            "    'REFER': 'VARCHAR', 'EXCLUIDOS': 'VARCHAR'"
            "  }, ignore_errors=true)"
        )
        rows = self._fetch_scalar("SELECT COUNT(*) FROM cid10", default=0)
        LOGGER.info("tabela cid10 carregada no DuckDB linhas=%s path=%s", rows, csv_path)


    # >>> PARTE 6 (#7): PREFIXOS DE CID PRE-COMPUTADOS <<<
    def _cid_diagnosis_columns(self, relation: str) -> list[str]:
        """Colunas de diagnostico (CID) reais de ``relation`` (best-effort)."""
        try:
            desc = self.connection.execute(f"DESCRIBE {relation}").fetchall()
        except Exception as exc:
            LOGGER.debug("cid_prefixes: DESCRIBE falhou p/ %s: %s", relation, exc)
            return []
        cols_upper = {str(row[0]).upper(): str(row[0]) for row in desc}
        diag = [
            cols_upper[c.upper()] for c in DIAGNOSIS_COLUMN_NAMES if c.upper() in cols_upper
        ]
        if not diag:
            diag = [
                real for up, real in cols_upper.items()
                if "CID" in up or "DIAG" in up or "CAUSA" in up
            ]
        return diag

    def _populate_cid_prefixes(self, force: bool = False, min_len: int = 3, limit: int = 500) -> int:
        """Pre-computa os prefixos de CID reais dos dados na tabela cid_prefixes.

        Idempotente: se a tabela ja tem linhas e force=False, nao recomputa.
        Retorna o numero de prefixos da tabela ao final.
        """
        try:
            existentes = int(self._fetch_scalar("SELECT COUNT(*) FROM cid_prefixes", default=0) or 0)
        except Exception:
            existentes = 0
        if existentes > 0 and not force:
            LOGGER.info("cid_prefixes: ja populado (%d prefixos); nada a fazer", existentes)
            return existentes

        prefixos: set[str] = set()
        relations: list[str] = []
        if self.groups:
            for name in self.groups:
                safe = name.replace('"', '""')
                relations.append('"' + safe + '".datasus_cache')
        else:
            relations.append("datasus_cache")

        for relation in relations:
            diag_cols = self._cid_diagnosis_columns(relation)
            for col in diag_cols[:10]:
                esc = col.replace('"', '""')
                sql = (
                    f"SELECT DISTINCT SUBSTR(UPPER(TRIM(CAST(\"{esc}\" AS VARCHAR))), 1, {min_len}) AS p "
                    f"FROM {relation} "
                    f"WHERE \"{esc}\" IS NOT NULL AND TRIM(CAST(\"{esc}\" AS VARCHAR)) <> ''"
                )
                try:
                    for row in self.connection.execute(sql).fetchmany(limit):
                        val = str(row[0] or "").upper()
                        if len(val) >= 2 and val[0].isalpha():
                            prefixos.add(val)
                except Exception as exc:
                    LOGGER.debug("cid_prefixes: falha em %s.%s: %s", relation, col, exc)
                    continue

        # Prefixos do de-para OFICIAL (nunca devem faltar).
        try:
            from db.cid_map import CID_ALIASES, get_cid_map
            for codigos in CID_ALIASES.values():
                for codigo in codigos:
                    prefixos.add(codigo.upper()[:max(2, min_len)])
            cid_map = get_cid_map()
            if cid_map.loaded:
                for entry in cid_map.entries:
                    prefixos.add(entry.inicio.upper()[:3])
        except Exception as exc:
            LOGGER.debug("cid_prefixes: de-para indisponivel: %s", exc)

        prefixos = {p for p in prefixos if len(p) >= 2 and p[0].isalpha()}
        if prefixos:
            try:
                self.connection.executemany(
                    "INSERT OR IGNORE INTO cid_prefixes (prefixo) VALUES (?)",
                    [(p,) for p in sorted(prefixos)],
                )
            except Exception as exc:
                LOGGER.warning("cid_prefixes: falha ao inserir: %s", exc)
        total = int(self._fetch_scalar("SELECT COUNT(*) FROM cid_prefixes", default=0) or 0)
        LOGGER.info("cid_prefixes: populados %d prefixos (tabela total=%d)", len(prefixos), total)
        return total

    def get_cid_prefixes(self, min_len: int = 2) -> list[str]:
        """Le os prefixos de CID pre-computados (PARTE 6). [] se vazio/ausente."""
        try:
            rows = self.connection.execute("SELECT prefixo FROM cid_prefixes").fetchall()
        except Exception:
            return []
        vals = {
            str(r[0]).upper() for r in rows
            if r and r[0] is not None and len(str(r[0])) >= 2 and str(r[0])[0].isalpha()
        }
        return sorted(vals)


    def _grupo_source_signature(self, grupo_name: str) -> float:
        """Assinatura de versao da FONTE de colunas de um grupo.

        Usa o mtime mais recente entre o banco DuckDB do grupo (.duckdb) e os
        parquets ingeridos. Quando a fonte muda (novos parquets/colunas), o
        mtime aumenta e o cache de colunas e considerado obsoleto. Retorna 0.0
        quando nao ha fonte conhecida (cache nunca e invalidado por mtime)."""
        mtimes: list[float] = []
        grupo = self.groups.get(grupo_name)
        if grupo is not None and getattr(grupo, "db_path", None):
            try:
                db_path = Path(grupo.db_path)
                if db_path.exists():
                    mtimes.append(db_path.stat().st_mtime)
            except OSError:
                pass
        try:
            grupo_dir = self.settings.cache_dir / grupo_name
            if grupo_dir.is_dir():
                for parquet in grupo_dir.rglob("*.parquet"):
                    try:
                        mtimes.append(parquet.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            pass
        return max(mtimes) if mtimes else 0.0

    def _build_grupo_columns_cache(self, grupo_name: str) -> list[dict[str, str]]:
        """Constroi e persiste o cache de colunas de um grupo.

        O cache JSON guarda uma assinatura ``source_mtime`` da fonte; se a fonte
        mudou desde a ultima gravacao, o cache e reconstruido automaticamente.
        """
        if grupo_name not in self.groups:
            return []
        cache_dir = self.settings.cache_dir / "colunas_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{grupo_name}.json"

        source_mtime = self._grupo_source_signature(grupo_name)

        # Usa cache persistido se existir E estiver atualizado em relacao a fonte.
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cols = data.get("columns", [])
                # >>> CORRECAO (parquet_year/parquet_month): filtra metadados de
                # arquivo tambem no cache JSON ja persistido. Caches antigos
                # (gravados antes desta correcao) ainda continham parquet_year/
                # parquet_month/filename/parquet_path; sem filtrar aqui, eles
                # continuariam vazando ao prompt/validacao ate a proxima
                # reconstrucao do cache.
                _meta_upper = {c.upper() for c in FILE_METADATA_COLUMNS}
                _before = len(cols)
                cols = [c for c in cols if str(c.get("column", "")).upper() not in _meta_upper]
                if len(cols) != _before:
                    LOGGER.info(
                        "grupo %s: %d metadados de arquivo removidos do cache de colunas "
                        "(parquet_year/parquet_month/filename/parquet_path)",
                        grupo_name, _before - len(cols),
                    )
                cached_mtime = float(data.get("source_mtime", 0) or 0)
                # So reutiliza se a fonte nao mudou (ou se nao e possivel medir).
                if source_mtime == 0.0 or cached_mtime >= source_mtime:
                    LOGGER.info("grupo %s: %d colunas carregadas do cache %s", grupo_name, len(cols), cache_file)
                    return cols
                LOGGER.info(
                    "grupo %s: cache de colunas obsoleto (fonte mtime=%.3f > cache mtime=%.3f); reconstruindo",
                    grupo_name, source_mtime, cached_mtime,
                )
            except Exception as e:
                LOGGER.warning("erro ao ler cache de colunas %s: %s", cache_file, e)

        # Constroi cache.
        #
        # IMPORTANTE: em modo grupos os dados vivem em <grupo>.datasus_cache
        # (bancos anexados como schema na conexao principal), NAO em
        # main.datasus_cache. Portanto NUNCA consultamos "datasus_cache" sem
        # qualificacao aqui: isso gerava "Catalog Error: Table with name
        # datasus_cache does not exist!" a cada inicializacao.
        grupo = self.groups[grupo_name]
        cols = []
        try:
            # 1) Conexao do grupo ja aberta (uso principal, schema qualificado).
            if grupo.connection is not None:
                conn = grupo.connection
                if getattr(grupo, "_using_main_connection", False):
                    relation = f'"{grupo_name.replace(chr(34), chr(34) * 2)}".datasus_cache'
                else:
                    relation = "datasus_cache"
                try:
                    schema_rows = conn.execute(f"DESCRIBE {relation}").fetchall()
                    cols = [{"column": row[0], "type": row[1]} for row in schema_rows]
                    LOGGER.info("grupo %s: %d colunas lidas do banco do grupo", grupo_name, len(cols))
                except Exception as e:
                    LOGGER.warning("grupo %s: erro ao ler schema do banco do grupo: %s", grupo_name, e)
                    cols = []

            # 2) Le as colunas diretamente do schema anexado na conexao principal
            #    (o banco <grupo>.duckdb esta anexado como schema com o nome do grupo).
            if not cols:
                try:
                    safe = grupo_name.replace('"', '""')
                    schema_rows = self.connection.execute(
                        f'DESCRIBE "{safe}".datasus_cache'
                    ).fetchall()
                    cols = [{"column": row[0], "type": row[1]} for row in schema_rows]
                    LOGGER.info("grupo %s: %d colunas lidas do schema anexado", grupo_name, len(cols))
                except Exception as e:
                    LOGGER.debug("grupo %s: schema anexado indisponível (%s)", grupo_name, e)

            # 3) Amostra de Parquet da pasta de dados (fallback quando o banco
            #    .duckdb do grupo ainda nao existe).
            if not cols:
                grupo_data_dir = DATASUS_DATA_DIR / grupo_name
                if grupo_data_dir.exists():
                    parquet_files = sorted(grupo_data_dir.glob("*.parquet"))
                    if parquet_files:
                        sample_path = str(parquet_files[0])
                        try:
                            df = duckdb.read_parquet(sample_path).df()
                            cols = [{"column": col, "type": str(df[col].dtype)} for col in df.columns]
                            LOGGER.info("grupo %s: %d colunas lidas de exemplo %s", grupo_name, len(cols), sample_path)
                        except Exception as e:
                            LOGGER.error("grupo %s: erro ao ler exemplo: %s", grupo_name, e)
                            cols = []

            # 4) Modo legado (sem grupos): so ai main.datasus_cache faz sentido.
            if not cols and not self.groups and self._cache_table_exists():
                schema_rows = self.connection.execute("DESCRIBE datasus_cache").fetchall()
                cols = [{"column": row[0], "type": row[1]} for row in schema_rows]
                LOGGER.info("modo legado: %d colunas lidas da tabela datasus_cache", len(cols))
        except Exception as e:
            LOGGER.error("grupo %s: erro ao ler colunas: %s", grupo_name, e)
            cols = []

        # >>> CORRECAO (parquet_year/parquet_month): REMOVE metadados de arquivo <<<
        # O dicionario de colunas (usado no prompt do LLM e na validacao de
        # colunas) era construido a partir de `DESCRIBE <grupo>.datasus_cache`,
        # que inclui as colunas de METADADOS DE ARQUIVO (filename, parquet_path,
        # parquet_year, parquet_month). Esses metadados vem do NOME do arquivo
        # Parquet (competencia de remessa), NAO sao datas reais de atendimento e
        # NUNCA devem ser usados para filtrar/agrupar consultas — o recorte
        # temporal correto e atend_ano/atend_mes (derivados do campo NATIVO de
        # cada grupo; ver db/period_fields.py).
        #
        # Sem esta remocao, o LLM "aprendia" a usar parquet_year como filtro e o
        # DuckDB falhava com:
        #   Binder Error: Referenced column "parquet_year" not found in FROM clause!
        metadata_upper = {c.upper() for c in FILE_METADATA_COLUMNS}
        cols = [c for c in cols if str(c.get("column", "")).upper() not in metadata_upper]

        # GUARDA: nunca sobrescreve um cache existente com lista VAZIA.
        # Isso evita zerar um cache bom quando a fonte nao esta acessivel no
        # momento (ex.: parquets em USB desconectado, grupo sem conexao aberta).
        if not cols and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                prev_cols = prev.get("columns", [])
                if prev_cols:
                    LOGGER.warning(
                        "grupo %s: leitura retornou 0 colunas; mantendo cache existente com %d colunas",
                        grupo_name, len(prev_cols),
                    )
                    return prev_cols
            except Exception as e:
                LOGGER.warning("grupo %s: falha ao ler cache anterior: %s", grupo_name, e)

        # Persiste o cache (com assinatura da fonte para invalidacao por mtime).
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": "1.1",
                        "grupo": grupo_name,
                        "source_mtime": source_mtime,
                        "column_count": len(cols),
                        "columns": cols,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            LOGGER.info("grupo %s: %d colunas persistidas em %s", grupo_name, len(cols), cache_file)
        except Exception as e:
            LOGGER.error("erro ao persistir cache de colunas %s: %s", cache_file, e)

        return cols

    def _cache_table_exists(self) -> bool:
        """Verifica se a tabela datasus_cache existe no banco PRINCIPAL (main).

        ATENCAO: nao use ``information_schema.tables`` para isso. O DuckDB rotula
        incorretamente como schema 'main' tabelas que na verdade pertencem a outro
        banco anexado (ex.: ab.datasus_cache aparece como ('main','datasus_cache')).
        Isso fazia este metodo retornar True em modo grupos e, em seguida,
        ``DESCRIBE datasus_cache`` (sem qualificacao) falhar com
        "Catalog Error: Table with name datasus_cache does not exist!".

        ``duckdb_tables()`` informa o ``database_name`` real, entao filtramos pelo
        nome interno do banco principal ('datasus').
        """
        try:
            result = self.connection.execute(
                "SELECT COUNT(*) FROM duckdb_tables() "
                "WHERE database_name = 'datasus' "
                "AND schema_name = 'main' AND table_name = 'datasus_cache'"
            ).fetchone()
            if result and result[0] > 0:
                return True
        except Exception:
            pass
        # Fallback: em conexoes sem catalogo (ex.: :memory:) usa information_schema,
        # mas somente quando NAO ha grupos (nesse caso 'main' e realmente o banco principal).
        if self.groups:
            return False
        try:
            result = self.connection.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name = 'datasus_cache'"
            ).fetchone()
            return bool(result and result[0] > 0)
        except Exception:
            return False

    def _build_all_grupo_columns_caches(self) -> dict[str, list[dict[str, str]]]:
        """Constroi (se necessario) e carrega os caches de colunas de todos os grupos."""
        cache_root = self.settings.cache_dir / "colunas_cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        result: dict[str, list[dict[str, str]]] = {}
        for name in self.groups:
            try:
                result[name] = self._build_grupo_columns_cache(name)
            except Exception as e:
                LOGGER.error("erro ao construir cache de colunas do grupo %s: %s", name, e)
                result[name] = []
        return result

    def _discover_groups(self) -> None:
        """Descobre os grupos a partir dos bancos DuckDB existentes no cache.

        Cada banco <grupo>.duckdb em CACHE_DIR representa um grupo. Esses bancos
        sao a FONTE DE VERDADE: quando existem, sao carregados diretamente e a
        pasta de dados (DATASUS_DATA_DIR) NAO e varrida.

        DATASUS_DATA_DIR so e varrido como FALLBACK (quando nenhum <grupo>.duckdb
        existe no cache), para incluir grupos que tenham Parquet mas ainda nao
        possuam banco .duckdb (ex.: uma nova sub-pasta adicionada apos a ultima
        ingestao). Esses grupos serao ingeridos na inicializacao.

        Observacoes importantes:
          * A pasta de dados costuma ficar DENTRO de CACHE_DIR (ex.:
            D:\\datasus\\cache\\parquet). Ela NAO e um grupo — e a raiz dos
            Parquet originais; por isso o nome 'parquet' e reservado.
          * Nomes que nao sao identificadores SQL validos (ex.: 'datasus[1]',
            resultante de um arquivo corrompido) sao descartados, em vez de
            derrubarem a inicializacao com ParserException no ATTACH.
        """
        base = self.settings.data_dir
        group_names: list[str] = []
    
        # 1. Descobre grupos a partir dos arquivos .duckdb no cache
        #    (exclui datasus.duckdb, datasus_fallback.duckdb e nomes reservados).
        if self.settings.cache_dir.is_dir():
            for db_file in sorted(self.settings.cache_dir.glob("*.duckdb")):
                name = db_file.stem
                # Pula o banco principal, o fallback e a pasta raiz de Parquet.
                if name in _RESERVED_GROUP_NAMES:
                    continue
                # Pula nomes que NAO sao identificadores SQL validos (ex.:
                # 'datasus[1]'). Tentar ATTACH/CREATE com esses nomes gera
                # ParserException e derrubava a inicializacao inteira.
                if not _is_valid_group_name(name):
                    LOGGER.warning(
                        "grupo ignorado: nome inválido %r (%s); renomeie o banco "
                        "para um identificador válido (letras/dígitos/_)",
                        name, db_file,
                    )
                    continue
                group_names.append(name)

        # 2. So varre DATASUS_DATA_DIR como FALLBACK: em modo grupos os bancos
        #    .duckdb ja sao a fonte de verdade. Varrer a pasta de dados quando ja
        #    existem bancos so introduz grupos espurios (ex.: 'parquet', que e a
        #    propria raiz dos Parquet) e dispara migracoes indevidas.
        if not group_names and base.is_dir():
            try:
                entries = list(base.iterdir())
                for entry in entries:
                    try:
                        if not entry.is_dir():
                            continue
                        if entry.resolve() == base.resolve():
                            continue
                        if not _is_valid_group_name(entry.name):
                            LOGGER.warning("pasta ignorada (nome inválido/reservado de grupo): %s", entry)
                            continue
                        if _discover_files_in(entry):
                            group_names.append(entry.name)
                    except OSError as exc:
                        LOGGER.warning("I/O falha ao inspecionar sub-pasta %s (USB?): %s", entry, exc)
                        continue
            except OSError as exc:
                LOGGER.error("não foi possível listar DATASUS_DATA_DIR (%s) — USB desconectado?: %s", base, exc)
    
        group_names = sorted(set(group_names))
        LOGGER.info("grupos detectados (cache duckdb + data_dir): %s", group_names)

        for name in group_names:
            # Migra caches existentes para a nova estrutura ano/mes (se necessario)
            _migrate_legacy_cache_paths(self.settings.cache_dir, name)
            # Cria o objeto do grupo passando a conexao principal como fallback
            source_dir = base / name if base.is_dir() else self.settings.cache_dir / name
            self.groups[name] = _GroupCache(name, source_dir, self.settings.cache_dir, self.connection)
        # O conjunto de grupos (e portanto o schema canonico) mudou.
        self._invalidate_canonical_schema()

    def groups_needing_ingest(self) -> list[str]:
        """Retorna os grupos que precisam de ingestao na inicializacao.

        Um grupo precisa de ingestao quando:
          * a tabela ``datasus_cache`` ainda nao existe no seu banco (cache
            ausente/vazio — ex.: grupo novo sem <grupo>.duckdb), OU
          * a tabela existe, mas ha Parquets do grupo ainda nao ingeridos
            (cache parcial / parquets faltantes).

        E usado no ``lifespan`` da app para decidir se a ingestao deve ser
        disparada, garantindo que grupos novos ou incompletos sejam
        sincronizados automaticamente no boot.
        """
        needing: list[str] = []
        for name, group in self.groups.items():
            # Garante que o schema/conexao do grupo esteja disponivel para a checagem.
            # Fix 3: a conexao aberta aqui e SEMPRE liberada no finally, para nao
            # deixar locks pendurados em <grupo>.duckdb caso algo falhe no meio
            # (evita o IOException: Could not set lock on file ... na proxima execucao).
            opened_here = group.connection is None
            try:
                self._open_group_if_needed(name)
            except Exception as exc:
                LOGGER.warning("grupo %s: não foi possível abrir para checagem (%s); marcando para ingestão", name, exc)
                needing.append(name)
                if opened_here:
                    group.close()
                continue

            try:
                # 1) Sem tabela de cache => grupo totalmente ausente do DuckDB.
                try:
                    if not group._cache_table_exists():
                        needing.append(name)
                        continue
                except Exception as exc:
                    LOGGER.warning("grupo %s: falha ao checar tabela de cache (%s); marcando para ingestão", name, exc)
                    needing.append(name)
                    continue

                # 2) Tabela existe, mas pode faltar algum Parquet (cache parcial).
                try:
                    pending, _, _ = group.pending_batch(limit=1)
                    if pending:
                        needing.append(name)
                except Exception as exc:
                    LOGGER.warning("grupo %s: falha ao checar pendências (%s); marcando para ingestão", name, exc)
                    needing.append(name)
            finally:
                # Libera a conexao aberta apenas para a checagem. Se o grupo ja
                # estava aberto antes (opened_here=False), nao fecha — outro
                # fluxo pode estar usando a conexao principal dele.
                if opened_here:
                    group.close()

        return needing

    def ensure_ready_sync(self, max_rounds: int = 3) -> dict[str, Any]:
        """Garante, de forma SINCRONA, que todos os grupos estejam no cache.

        Fluxo (projetado para rodar dentro do ``lifespan`` da app, ANTES de
        servir requisicoes):

          1. Detecta os grupos com cache ausente/incompleto
             (``groups_needing_ingest``).
          2. Se houver algum, executa ``ingest()`` bloqueante e completo.
          3. Re-verifica. Repete ate ``max_rounds`` vezes enquanto ainda
             restarem pendencias (cobre casos de interrupcao parcial).

        Retorna um resumo com o estado antes/depois. Se, ao final, ainda
        existirem grupos pendentes, levanta ``RuntimeError`` — assim o lifespan
        registra o erro e ``/api/ask`` responde 503 em vez de servir dados
        incompletos silenciosamente.
        """
        before = self.groups_needing_ingest()
        LOGGER.info("[ensure_ready_sync] grupos pendentes ANTES: %s", before or "nenhum")

        result: dict[str, Any] = {
            "pending_before": before,
            "rounds": 0,
            "ingest_results": [],
        }

        pending = before
        for round_index in range(1, max_rounds + 1):
            if not pending:
                break
            result["rounds"] = round_index
            LOGGER.info(
                "[ensure_ready_sync] rodada %d/%d: ingerindo grupos pendentes %s (sincronamente)",
                round_index, max_rounds, pending,
            )
            ingest_result = self.ingest()
            result["ingest_results"].append(ingest_result)
            pending = self.groups_needing_ingest()
            LOGGER.info(
                "[ensure_ready_sync] rodada %d concluída. pendentes após rodada: %s",
                round_index, pending or "nenhum",
            )

        result["pending_after"] = pending

        if pending:
            raise RuntimeError(
                "Cache de grupos incompleto após ingestão síncrona na inicialização: "
                f"{pending}. Verifique os arquivos Parquet e os logs de ingestão."
            )

        LOGGER.info("[ensure_ready_sync] todos os grupos estão prontos no cache DuckDB.")
        return result

    def _fetch_scalar(self, query: str, params: list | None = None, default: Any = None) -> Any:
        """Executa uma query e retorna o primeiro valor da primeira linha."""
        try:
            cur = self.connection.execute(query, params or [])
            row = cur.fetchone()
            return row[0] if row else default
        except Exception as e:
            LOGGER.error("erro ao executar query scalar: %s | %s", query, e)
            return default

    def query_kb(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        """Executa uma query SQL na conexao principal e retorna resultados como list[dict]."""
        try:
            df = self.connection.execute(sql, params or []).fetchdf()
            return df.to_dict("records")
        except Exception as e:
            LOGGER.error("erro em query_kb: %s | SQL: %s", e, sql)
            return []

    def clear_cache(self) -> bool:
        """Limpa todos os dados de cache (tabelas principais)."""
        try:
            with self._build_lock:
                self.connection.execute("DROP TABLE IF EXISTS datasus_cache")
                self.connection.execute("CREATE TABLE datasus_cache AS SELECT * FROM datasus_cache LIMIT 0")
                for group in self.groups.values():
                    if group.connection:
                        group.connection.execute("DROP TABLE IF EXISTS datasus_cache")
                        group.connection.execute("CREATE TABLE datasus_cache AS SELECT 1 AS dummy WHERE 1=0")
                self.connection.execute("DELETE FROM file_manifest")
            return True
        except Exception as e:
            LOGGER.error("erro ao limpar cache: %s", e)
            return False

    def close(self) -> None:
        """Fecha a conexao com o DuckDB."""
        try:
            for group in self.groups.values():
                if group.connection:
                    group.close()
            self.connection.close()
        except Exception as e:
            LOGGER.debug("erro ao fechar conexões: %s", e)

    def get_grupo_columns(self, grupo_name: str | None = None) -> dict[str, list[dict[str, str]]]:
        """
        Retorna as colunas de grupos especificos (ou todos).
        Se o cache nao foi construido ainda, constroi.
        """
        if not hasattr(self, "_columns_cache") or self._columns_cache is None:
            self._build_all_grupo_columns_caches()
        
        if grupo_name:
            return {grupo_name: self._columns_cache.get(grupo_name, [])}
        return dict(self._columns_cache)

    def get_column_dictionary(self):
        """Retorna o dicionario consolidado de colunas (em memoria, cacheado).

        O dicionario e reconstruido apenas quando o cache de colunas por grupo
        muda (comparacao por assinatura), evitando reprocessamento a cada
        requisicao do LLMQueryService.
        """
        # Garante que o cache por grupo esta carregado.
        if not hasattr(self, "_columns_cache") or self._columns_cache is None:
            self._columns_cache = self._build_all_grupo_columns_caches()
        try:
            dictionary = self._column_dict_store.get(self._columns_cache)
        except Exception as e:
            LOGGER.warning("get_column_dictionary: falha ao obter do store (%s); reconstruindo.", e)
            dictionary = ColumnDictionaryStore().get(self._columns_cache)
        # Atualiza a referencia principal quando a assinatura muda.
        if self._column_dictionary is None or not dictionary.matches(self._column_dictionary.signature):
            self._column_dictionary = dictionary
        return dictionary

    def refresh_column_dictionary(self, persist: bool = True):
        """Reconstroi o dicionario de colunas (chamado apos nova ingestao).

        Sempre tenta persistir a tabela ``column_dictionary`` no DuckDB quando
        ``persist`` for True (best-effort).
        """
        try:
            self._columns_cache = self._build_all_grupo_columns_caches()
        except Exception as e:
            LOGGER.error("refresh_column_dictionary: falha ao reconstruir caches: %s", e)
        try:
            self._column_dict_store.invalidate()
            dictionary = self._column_dict_store.get(self._columns_cache)
            self._column_dictionary = dictionary
            if persist:
                persist_to_duckdb(self.connection, dictionary)
            LOGGER.info(
                "dicionário de colunas atualizado grupos=%d colunas_unicas=%d",
                len(self._column_dictionary.by_group),
                len(self._column_dictionary.unique_columns),
            )
            return dictionary
        except Exception as e:
            LOGGER.warning("refresh_column_dictionary: falha ao reconstruir dicionário: %s", e)
            return self._column_dictionary

    def _cached_parquet_files(self) -> list[Path]:
        """Retorna todos os Parquet cacheados, agregados das sub-pastas (grupos)."""
        files: list[Path] = []
        if self.groups:
            for group in self.groups.values():
                files.extend(_discover_files_in(self.settings.cache_dir / group.name))
        else:
            files.extend(_discover_files_in(self.settings.cache_dir / "parquet"))
        return sorted(set(files))

    def _indexed_parquet_files(self) -> list[Path]:
        return [Path(row[0]) for row in self._indexed_parquet_records()]

    def indexed_parquet_records(self) -> list[tuple[Path, str, str]]:
        """API PUBLICA dos registros (path, ano, mes) do indice de parquets.

        Delega para ``_indexed_parquet_records`` (mantido para compatibilidade
        e mock em testes). Prefira ESTE metodo em codigo de aplicacao.
        """
        return self._indexed_parquet_records()

    def _indexed_parquet_records(self) -> list[tuple[Path, str, str]]:
        rows = self.connection.execute(
            "SELECT path, parquet_year, parquet_month FROM parquet_period_index ORDER BY path"
        ).fetchall()
        return [(Path(path), str(year), str(month)) for path, year, month in rows]

    def _refresh_period_index(self, files: list[Path]) -> None:
        records = []
        for path in files:
            match = re.search(r"(\d{2})(0[1-9]|1[0-2])[a-z]*\.parquet$", path.name.casefold())
            if match:
                records.append((str(path), "20" + match.group(1), match.group(2)))
        self.connection.execute("DELETE FROM parquet_period_index")
        if records:
            self.connection.executemany(
                "INSERT INTO parquet_period_index VALUES (?, ?, ?)", records
            )
        LOGGER.info("parquet period index refreshed files=%d", len(records))

    def _parse_periods(
        self, question: str, available_periods: set[tuple[str, str]]
    ) -> tuple[set[str], set[str], set[tuple[str, str]] | None, bool]:
        """Interpreta a pergunta e devolve o recorte temporal (fonte unica).

        Extraido de ``select_files_for_question`` para ser reutilizado tanto no
        modo arquivos (Parquet) quanto no modo grupos (DuckDB), sem duplicar a
        logica de linguagem natural (anos, meses, intervalos, "ultimos N anos").

        Retorna (years, months, recent_period_keys, agrupar_por_mes).
        """
        year_values = [int(value) for value in re.findall(r"\b(20\d{2})\b", question)]
        years = {str(value)[-2:] for value in year_values}
        month_names = {
            "janeiro": "01", "fevereiro": "02", "marco": "03", "abril": "04",
            "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
            "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
        }
        normalized_question = "".join(
            char for char in unicodedata.normalize("NFD", question.casefold())
            if unicodedata.category(char) != "Mn"
        )
        range_match = re.search(r"\b(20\d{2})\s*(?:a|ate|até|e)\s*(20\d{2})\b", normalized_question)
        if range_match:
            first_year, last_year = map(int, range_match.groups())
            if first_year > last_year:
                first_year, last_year = last_year, first_year
            years = {str(year)[-2:] for year in range(first_year, last_year + 1)}
        # >>> CORRECAO: Interpreta expressoes com "hoje"/"atual" (ex: "2023 ate hoje") usando o
        # ano mais recente disponivel no cache local <<<
        elif "hoje" in normalized_question or "atual" in normalized_question:
            if not available_periods:
                raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
            # Detecta padrao "2023 ate hoje" ou "de 2023 a hoje"
            hoje_base = re.search(r"\b(20\d{2})\s*(?:a|ate|até|)\s*hoje\b", normalized_question)
            recent_available = sorted({int(period[0]) for period in available_periods}, reverse=True)
            if not recent_available:
                raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
            if hoje_base:
                first_hoje = int(hoje_base.group(1))
                last_hoje = recent_available[0]
                if first_hoje > last_hoje:
                    first_hoje, last_hoje = last_hoje, first_hoje
                years = {str(year)[-2:] for year in range(first_hoje, last_hoje + 1)}
            else:
                # "ate hoje" sem ano inicial = usar TODOS os anos disponiveis no cache
                # para nao perder dados (o usuario provavelmente quer a serie completa)
                years = {str(year)[-2:] for year in recent_available}
        months = {month_names[name] for name in month_names if name in normalized_question}
        months.update(re.findall(r"\b(?:mes|m[eê]s)\s*(0?[1-9]|1[0-2])\b", normalized_question))
        months.update(re.findall(r"\b(0?[1-9]|1[0-2])\s*[/.-]\s*20\d{2}\b", normalized_question))
        months.update(re.findall(r"\b20\d{2}\s*[/.-]\s*(0?[1-9]|1[0-2])\b", normalized_question))
        months = {month.zfill(2) for month in months}

        # >>> CORRECAO 1: numero POR EXTENSO em periodos relativos ("dois anos").
        # Antes o regex exigia digitos (\d+), entao "nos ultimos dois anos" nao
        # casava, recent_match ficava None e a funcao levantava ValueError. <<<
        numeros_extenso = {
            "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
            "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
            "onze": 11, "doze": 12,
        }
        # >>> CORRECAO 2: "agrupe por mes"/"por mes"/"mensal" indicam AGRUPAMENTO,
        # nao selecao de um mes especifico. Marcamos a intencao para nao restringir a
        # selecao a um mes e para propagar a dica de GROUP BY atend_mes. <<<
        agrupar_por_mes = bool(
            re.search(r"\bmes(?:es)?\b", normalized_question)
            and ("agrup" in normalized_question or "por mes" in normalized_question
                 or "mensal" in normalized_question or "mes a mes" in normalized_question)
        )
        recent_match = re.search(
            r"\b(?:nos?\s+)?(?:últim\w*|ultim\w*|utlim\w*)\s+?(?:(\d+|[a-z]+)\s+)?(anos?|mes(?:es)?|ano)\b",
            normalized_question,
        )
        if not recent_match:
            # Tenta novamente sem o qualificador "nos " e com variacoes ainda mais flexiveis
            recent_match = re.search(
                r"\b(?:ultim\w*|utlim\w*|últim\w*)\s+?(?:(\d+|[a-z]+)\s+)?(anos?|mes(?:es)?|ano)\b",
                normalized_question,
            )
        if recent_match:
            raw_amount = recent_match.group(1)
            if raw_amount is None:
                amount = 1
            elif raw_amount.isdigit():
                amount = int(raw_amount)
            else:
                # Numero por extenso ("dois", "tres", ...). Palavra desconhecida
                # (ex.: "alguns") assume 1 para nao interromper a consulta.
                amount = numeros_extenso.get(raw_amount, 1)
            unit = recent_match.group(2)
            if amount < 1:
                raise ValueError("O período dos últimos anos ou meses deve ser maior que zero.")
            if not available_periods:
                raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
            if unit.startswith("ano"):
                recent_years = sorted({period[0] for period in available_periods}, reverse=True)[:amount]
                if not recent_years:
                    raise ValueError("Não há anos disponíveis no cache para o período solicitado.")
                years = {year[-2:] for year in recent_years}
                # >>> CORRECAO 2: so zera o filtro de mes quando NAO e agrupamento
                # mensal (no agrupamento, o mes e separado pelo GROUP BY). <<<
                if not agrupar_por_mes:
                    months = set()
                recent_period_keys = None
            else:
                recent_periods = sorted(available_periods, reverse=True)[:amount]
                if not recent_periods:
                    raise ValueError("Não há períodos disponíveis no cache para o período solicitado.")
                recent_period_keys = set(recent_periods)
        else:
            recent_period_keys = None
        if not years and not months:
            if recent_match and not available_periods:
                raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
            if not recent_match:
                raise ValueError("Informe na pergunta um ano, mês ou período relativo válido para selecionar os Parquets.")
        return years, months, recent_period_keys, agrupar_por_mes

    def select_files_for_question(self, question: str) -> list[Path]:
        """Select cached Parquets whose filename period matches the question."""
        indexed_records = self._indexed_parquet_records()
        if not indexed_records:
            files = self._cached_parquet_files()
            if files:
                self._refresh_period_index(files)
                indexed_records = self._indexed_parquet_records()
        available_periods = {(year, month) for _, year, month in indexed_records}
        years, months, recent_period_keys, agrupar_por_mes = self._parse_periods(
            question, available_periods
        )
        selected: list[Path] = []
        for path, year, month in indexed_records:
            period = (year, month)
            matches_recent = recent_period_keys is None or period in recent_period_keys
            if matches_recent and (not years or year[-2:] in years) and (not months or month in months):
                selected.append(path)
        LOGGER.info(
            "selected parquet files=%d of %d years=%s months=%s agrupar_por_mes=%s sample=%s",
            len(selected), len(indexed_records), years, months, agrupar_por_mes,
            [path.name for path in selected[:5]],
        )
        return selected

    def select_groups_periods_for_question(self, question: str) -> list[tuple[str, str, str]]:
        """Resolve (grupo, ano, mes) SEM tocar em Parquet no disco.

        Le os periodos REAIS de atendimento dos grupos consultando o CAMPO
        NATIVO de periodo (SP_DTINTER, DT_INTER, DT_ATEND, PA_CMP, AP_DTINIC,
        conforme db/period_fields.py) no <grupo>.datasus_cache. Nao usa o nome
        do arquivo (parquet_year/parquet_month) nem enumera arquivos.
        """
        available: set[tuple[str, str, str]] = set()
        for name in self.groups:
            safe = name.replace('"', '""')
            try:
                self._open_group_if_needed(name)
            except Exception:
                pass
            # Periodos derivados do CAMPO NATIVO de periodo do grupo
            # (SP_DTINTER, DT_INTER, DT_ATEND, PA_CMP, AP_DTINIC, ...),
            # NAO mais do nome do arquivo (parquet_year/parquet_month).
            pf = PERIOD_FIELDS.get(name)
            if pf is None:
                continue
            pcol = pf.columns[0]
            ano_sql = ano_expr(pcol)
            mes_sql = mes_expr(pcol)
            rows = []
            try:
                rows = self.connection.execute(
                    f"SELECT DISTINCT {ano_sql} AS a, {mes_sql} AS m "
                    f"FROM {safe}.datasus_cache "
                    f"WHERE \"{pcol}\" IS NOT NULL"
                ).fetchall()
            except Exception:
                rows = []
            for year, month in rows:
                if year is None or month is None:
                    continue
                available.add((name, str(year), str(month).zfill(2)))
        if not available:
            return []
        periods_available = {(y, m) for _, y, m in available}
        # >>> CORRECAO (pergunta SEM periodo): nao falha mais. <<<
        # Quando a pergunta NAO informa ano/mes (ex.: "cids M05,M06,M10"), o
        # _parse_periods levanta ValueError("Informe a pergunta um ano, mes...").
        # Antes essa excecao escapava de prepare_question_cache (que, em ask.py,
        # so captura RuntimeError) e derrubava a requisicao -- MESMO com CIDs
        # VALIDOS. Agora tratamos esse caso degradando para TODOS os periodos
        # disponiveis no cache, que e exatamente a intencao do fallback de
        # ask.py ("tentar com todos os periodos"). Assim a consulta roda.
        try:
            years, months, recent_period_keys, agrupar_por_mes = self._parse_periods(
                question, periods_available
            )
        except ValueError as period_exc:
            LOGGER.warning(
                "pergunta sem período interpretável (%s); usando TODOS os %d "
                "períodos disponíveis do cache.",
                period_exc, len(periods_available),
            )
            years, months, recent_period_keys, agrupar_por_mes = set(), set(), None, False
        self._last_question_years = years
        self._last_question_months = months
        self._last_question_recent = recent_period_keys
        self._last_question_agrupar_mes = agrupar_por_mes
        selected: list[tuple[str, str, str]] = []
        for grupo, year, month in available:
            period = (year, month)
            match_recent = recent_period_keys is None or period in recent_period_keys
            if match_recent and (not years or year[-2:] in years) and (not months or month in months):
                selected.append((grupo, year, month))
        selected.sort()
        LOGGER.info(
            "selected groups periods=%d of %d grupos=%d years=%s months=%s",
            len(selected), len(available), len(self.groups), years, months,
        )
        return selected

    @staticmethod
    def question_wants_monthly_grouping(question: str) -> bool:
        """True quando a pergunta pede AGRUPAMENTO por mes (ex.: 'agrupe por mes').

        Reconhece variacoes como 'agrupe por mes', 'por mes', 'mensal' e
        'mes a mes'. E usado pelo LLMQueryService para instruir o SQL a agrupar
        pela coluna JA EXISTENTE ``atend_mes`` da view ``datasus_query_cache``
        (sem inventar colunas de mes).
        """
        normalized_question = "".join(
            char for char in unicodedata.normalize("NFD", question.casefold())
            if unicodedata.category(char) != "Mn"
        )
        # Referencia a MES em qualquer forma: 'mes'/'meses', 'mensal',
        # 'mensalmente' etc. Usamos PREFIXO (nao limite de palavra) porque
        # 'mensalmente'/'mensal' NAO contem o token isolado 'mes'.
        menciona_mes = bool(
            re.search(r"\bmes(?:es)?\b", normalized_question)
            or "mensal" in normalized_question
        )
        if not menciona_mes:
            return False
        # Indicios EXPLICITOS de agrupamento (mesmo sem 'mes' isolado):
        # 'agrupe/agrupado/agrupamento' + referencia a mes -> agrupamento.
        if "agrup" in normalized_question:
            return True
        # 'mensal'/'mensalmente' sozinhos ja indicam agrupamento por mes.
        if "mensal" in normalized_question:
            return True
        # Expressoes que pedem quebra por mes.
        return bool(
            "por mes" in normalized_question
            or "mes a mes" in normalized_question
            or "ao mes" in normalized_question
            or "cada mes" in normalized_question
        )

    @log_calls
    def question_scope(self, question: str) -> dict[str, Any]:
        try:
            files = self.select_files_for_question(question)
        except ValueError as e:
            LOGGER.warning("question_scope sem arquivos: %s", e)
            # Se nao conseguimos selecionar arquivos, retorna escopo vazio
            return {"files": 0, "names": [], "periods": set(), "error": str(e)}
        indexed_paths = {path: (year, month) for path, year, month in self._indexed_parquet_records()}
        periods = {indexed_paths[path] for path in files if path in indexed_paths}
        return {"files": len(files), "names": [path.name for path in files], "periods": periods}

    @staticmethod
    def _file_periods(files: list[Path]) -> set[tuple[str, str]]:
        periods: set[tuple[str, str]] = set()
        for path in files:
            match = re.search(r"(\d{2})(0[1-9]|1[0-2])[a-z]*\.parquet$", path.name.casefold())
            if match:
                periods.add(("20" + match.group(1), match.group(2)))
        return periods

    @log_calls
    def prepare_question_cache(
        self,
        question: str,
        request_id: str,
        sql: str | None = None,
        extra_columns: set[str] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        # Guarda a pergunta corrente para permitir RECONSTRUIR a tabela temporaria
        # em ``execute()`` caso a SQL mude de colunas depois (refazer/revisao).
        self._last_question = question
        # >>> PARTE 7 (#9): guarda a ULTIMA SQL para reprojecao minima no fallback.
        self._last_sql = sql
        LOGGER.info("question cache start request_id=%s question_length=%d", request_id, len(question))
        # >>> CAMINHO RAPIDO: LEITURA DIRETA DO DUCKDB (sem Parquet no disco) <<<
        # Com cache DuckDB (self.groups), resolve o recorte temporal pelos
        # indices nativos dos grupos (dentro do .duckdb). Nao varre Parquet
        # nem parquet_period_index. Fallback legado apenas sem grupos.
        files: list[Path] = []
        periods: set[tuple[str, str]] = set()
        if self.groups:
            selected_groups = self.select_groups_periods_for_question(question)
            if not selected_groups:
                raise RuntimeError("Nenhum período do cache corresponde ao informado na pergunta.")
            periods = {(year, month) for _, year, month in selected_groups}
        else:
            files = self.select_files_for_question(question)
            if not files:
                raise RuntimeError("Nenhum Parquet do cache corresponde ao período informado na pergunta.")
            indexed_paths = {path: (year, month) for path, year, month in self._indexed_parquet_records()}
            periods = {indexed_paths[path] for path in files if path in indexed_paths}
        
        # >>> AJUSTE: recorte temporal pelos CAMPOS NATIVOS do DATASUS <<<
        period_years = sorted({int(year) for year, _ in periods}) if periods else []
        period_months = sorted({month for _, month in periods}) if periods else []
        # O filtro EFETIVO de ano/mes e montado POR GRUPO, usando o campo
        # NATIVO do DATASUS (ver db/period_fields.py). parquet_year e
        # parquet_month NAO sao mais usados em filtros.
        LOGGER.info(
            "question cache period filter request_id=%s years=%s months=%s (via campos nativos)",
            request_id, period_years, period_months,
        )
        # <<< FIM DO AJUSTE >>>>
        
        # Limpa a view (formato antigo) e a tabela temporaria (formato novo) para
        # garantir que uma materializacao anterior da MESMA conexao nao seja reusada.
        self.connection.execute("DROP VIEW IF EXISTS datasus_query_cache")
        self.connection.execute("DROP TABLE IF EXISTS datasus_query_cache")
        self.connection.execute("DROP VIEW IF EXISTS datasus_query_files")
        
        if self.groups:
            # NOVO: usa as tabelas materializadas nos grupos (.duckdb) em vez
            # de re-ler os arquivos Parquet do disco. Isso evita OOM e torna
            # as consultas muito mais rapidas.
            group_selects = []
            # >>> FASE 3 (S2): schema canonico memoizado como fonte unica <<<
            # Antes rodavamos, POR GRUPO E A CADA PERGUNTA, consultas de catalogo
            # (duckdb_databases/duckdb_tables/duckdb_views) para decidir se o
            # grupo entrava no UNION. Agora reaproveitamos o cache S2
            # (self._canonical_schema_cache), que ja sabe quais grupos tem
            # ``datasus_cache`` (TABELA ou VIEW) e quais colunas projetar.
            #
            # O ATTACH sob demanda continua: se um grupo ainda nao estiver
            # anexado, anexamos antes de (re)computar o schema S2. Isso mantem o
            # comportamento anterior para o caso (raro) de grupo recém-criado.
            self._ensure_groups_attached()
            all_columns, group_columns = self._get_canonical_schema()
            # >>> GUARD: S2 vazio mas ha grupos <<<
            # Se o cache S2 nao conhece nenhum grupo, mas ``self.groups`` nao
            # esta vazio (ex.: grupos anexados/ingeridos apos a ultima
            # invalidacao), descartamos e recomputamos UMA vez antes de desistir.
            if not group_columns and self.groups:
                self._invalidate_canonical_schema()
                all_columns, group_columns = self._get_canonical_schema()
            # Fonte unica de verdade dos grupos efetivamente disponiveis
            # (TABELA ou VIEW), a partir do cache S2.
            _s2_safe_to_name = (self._canonical_schema_cache or {}).get("safe_to_name", {})
            s2_groups = [
                (safe, _s2_safe_to_name.get(safe, safe))
                for safe in (self._canonical_schema_cache or {}).get("safe_names", [])
                if safe in group_columns
            ]

            if s2_groups:

                # >>> OTIMIZACAO OOM: projecao MINIMA <<<
                # Quando a SQL final e conhecida, projetamos apenas as colunas
                # que ela realmente usa (mais as colunas de periodo). Sem isso,
                # cada um dos 16 grupos projeta as ~469 colunas unificadas — a
                # maioria vira CAST(NULL AS VARCHAR) — e o UNION ALL materializa
                # um volume enorme, estourando a memoria. Com a projecao minima o
                # conjunto materializado cai para umas poucas colunas.
                # >>> PROJECAO MINIMA (VIEW LAZY) <<<
                # Como datasus_query_cache e SEMPRE uma VIEW recriada a cada
                # prepare_question_cache, NAO ha risco de a "tabela fixa" ficar
                # sem uma coluna pedida depois. Por isso projetamos SOMENTE:
                #   1) as colunas realmente referenciadas na SQL;
                #   2) as colunas de periodo derivadas (atend_ano/atend_mes).
                # Antes incluiamos as ~248 colunas "plausiveis" (de 468), o que
                # fazia cada um dos 16 grupos projetar dezenas de CAST(NULL) e
                # elevava o execute a ~31s. Com ~poucas colunas, o push-down e o
                # UNION ALL ficam ordens de magnitude mais baratos.
                projected_columns = set(all_columns)
                if sql:
                    referenced = self._referenced_columns(sql, all_columns)
                    if referenced:
                        referenced.add(ATEND_ANO)
                        referenced.add(ATEND_MES)
                        projected_columns = referenced
                # >>> PARTE 7 (#9): colunas EXTRA forcam a projecao a incluir
                # a coluna FALTANTE sem degradar para TODAS as ~469 colunas.
                # Usado pelo fallback de 'coluna ausente' em execute().
                if extra_columns:
                    filtrados = {c for c in extra_columns if c in all_columns}
                    if filtrados:
                        projected_columns = set(projected_columns) | filtrados
                        projected_columns.add(ATEND_ANO)
                        projected_columns.add(ATEND_MES)
                        LOGGER.info(
                            "question cache reprojecao minima (+%d colunas faltantes) "
                            "request_id=%s colunas=%d de %d",
                            len(filtrados), request_id, len(projected_columns), len(all_columns),
                        )
                        LOGGER.info(
                            "question cache projeção mínima (VIEW) request_id=%s colunas=%d de %d",
                            request_id, len(projected_columns), len(all_columns),
                        )

                for safe_name, name in s2_groups:
                    cols = group_columns[safe_name]
                    pf = PERIOD_FIELDS.get(name)
                    if pf is None:
                        # >>> CORRECAO (grupo sem campo de periodo) <<<
                        # Antes, um grupo SEM campo nativo de periodo entrava no
                        # UNION com atend_ano/atend_mes = NULL e WHERE TRUE, isto
                        # e, SEM recorte temporal. Isso CONTAMINAVA o resultado:
                        # as linhas desse grupo apareciam em QUALQUER periodo,
                        # independentemente do ano/mes pedido na pergunta.
                        #
                        # Agora o grupo e EXCLUIDO do UNION (nao gera ATEND_ANO/
                        # ATEND_MES nulos nem entra sem filtro). Se nenhum grupo
                        # sobrar, group_selects fica vazio e o fluxo cai no
                        # comportamento ja existente ("Nenhum periodo do cache
                        # corresponde ao informado na pergunta."), sem excecao nova.
                        LOGGER.warning(
                            "grupo %s: sem campo de período mapeado em PERIOD_FIELDS; "
                            "EXCLUÍDO do UNION por não respeitar o recorte temporal "
                            "(evita contaminação por linhas fora do período).",
                            name,
                        )
                        continue
                    pcol = pf.columns[0]                         # campo de data nativo do grupo
                    # >>> FASE 3 (#3.5): usa as colunas FISICAS atend_ano/atend_mes <<<
                    # Se a tabela do grupo ja materializa atend_ano/atend_mes
                    # (FASE 3), referenciamos as colunas fisicas — o filtro vira
                    # push-down nativo. Em caches legados (sem as colunas), caimos
                    # no calculo derivado a partir do campo nativo, mantendo o
                    # comportamento anterior.
                    if ATEND_ANO in cols and ATEND_MES in cols:
                        ano_sql = f'"{ATEND_ANO}"'                # coluna fisica (push-down)
                        mes_sql = f'"{ATEND_MES}"'                # coluna fisica (push-down)
                    else:
                        # FASE 3B: atend_ano e INTEGER; o fallback derivado tambem
                        # precisa ser INTEGER para nao misturar tipos no UNION ALL.
                        ano_sql = ano_int_expr(pcol)              # ano (YYYY) INTEGER derivado
                        mes_sql = mes_expr(pcol)                  # mes (MM) derivado
                    conds = []
                    if period_years:
                        # FASE 3B: atend_ano e INTEGER na tabela/view/resumos.
                        # Monta literais INTEIROS (ex.: 2022) para comparacao
                        # numerica nativa e push-down. Fallback para string caso
                        # o ano nao seja numerico (defensivo).
                        def _ano_lit(y: str) -> str:
                            ys = str(y).strip()
                            return ys if ys.lstrip("-").isdigit() else "'" + ys.replace("'", "''") + "'"
                        anos_lit = ", ".join(_ano_lit(y) for y in period_years)
                        conds.append(f"{ano_sql} IN ({anos_lit})")
                    if period_months:
                        meses_lit = ", ".join(
                            "'" + str(mo).zfill(2) + "'" for mo in period_months
                        )
                        conds.append(f"{mes_sql} IN ({meses_lit})")
                    where_group = " AND ".join(conds) if conds else "TRUE"

                    projections = []
                    for col in projected_columns:
                        esc = col.replace('"', '""')
                        if col == ATEND_ANO:
                            projections.append(f"{ano_sql} AS {ATEND_ANO}")
                        elif col == ATEND_MES:
                            projections.append(f"{mes_sql} AS {ATEND_MES}")
                        elif col in cols:
                            projections.append(f'"{esc}"')
                        else:
                            projections.append(f'CAST(NULL AS VARCHAR) AS "{esc}"')
                    group_selects.append(
                        f"SELECT {', '.join(projections)} FROM {safe_name}.datasus_cache "
                        f"WHERE {where_group}"
                    )
            
            if group_selects:
                union_sql = " UNION ALL ".join(group_selects)
                # >>> LEITURA DIRETA DO DUCKDB COMO VIEW (LAZY) <<<
                # SEMPRE criamos uma VIEW (nunca materializamos TEMP TABLE).
                # A view le diretamente as tabelas fisicas <grupo>.datasus_cache
                # nos .duckdb anexados (dados do cache DuckDB, nao de Parquet).
                # Por que nao materializar:
                #   * elimina o OOM (o UNION de 16 grupos nao cabe em pouca RAM);
                #   * elimina a copia do subconjunto a cada pergunta;
                #   * permite push-down do filtro na leitura das tabelas fisicas.
                self.connection.execute("DROP VIEW IF EXISTS datasus_query_cache")
                self.connection.execute("DROP TABLE IF EXISTS datasus_query_cache")
                self.connection.execute(
                    "CREATE TEMP VIEW datasus_query_cache AS " + union_sql
                )
                LOGGER.info(
                    "question cache como VIEW (lazy, leitura direta do .duckdb) "
                    "request_id=%s grupos=%d duration_ms=%.2f",
                    request_id, len(group_selects), (time.perf_counter() - started) * 1000,
                )
                return {"files": 0, "names": [], "periods": periods}
        
        # Fallback: se nao ha grupos ou falhou, le os Parquets diretamente (modo legado)
        paths = ", ".join("'" + str(path).replace("'", "''") + "'" for path in files)
        self.connection.execute(
            "CREATE TEMP VIEW datasus_query_files AS SELECT * FROM read_parquet(["
            + paths
            + "], union_by_name = true, filename = true)"
        )
        # Modo legado (sem grupos): nao ha campo nativo por grupo disponivel
        # aqui. Expoe as colunas derivadas como NULL para manter o contrato.
        self.connection.execute(
            "CREATE OR REPLACE TEMP VIEW datasus_query_cache AS "
            "SELECT *, "
            f"CAST(NULL AS INTEGER) AS {ATEND_ANO}, "
            f"CAST(NULL AS VARCHAR) AS {ATEND_MES} "
            "FROM datasus_query_files"
        )
        LOGGER.info(
            "question cache complete request_id=%s files=%d first=%s last=%s duration_ms=%.2f",
            request_id, len(files), files[0].name, files[-1].name, (time.perf_counter() - started) * 1000,
        )
        return {"files": len(files), "names": [path.name for path in files], "periods": periods}

    def summary_tables(self) -> list[str]:
        """API PUBLICA dos nomes das tabelas agregadas resumo_* disponiveis.

        Delega para ``_summary_tables`` (mantido para compatibilidade e mock
        em testes). Prefira ESTE metodo em codigo de aplicacao.
        """
        return self._summary_tables()

    def _summary_tables(self) -> list[str]:
        names = {"resumo_diagnosticos", "resumo_cid_periodo", "resumo_anual_cid"}
        result = self.connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        present = [row[0] for row in result if row and row[0] in names]

        # >>> CORRECAO (schema obsoleto dos resumos persistidos) <<<
        # As tabelas resumo_* vivem no .duckdb PRINCIPAL e sao PERSISTIDAS entre
        # execucoes. Se foram criadas por uma versao ANTIGA do codigo (que ainda
        # derivava o periodo de parquet_year/parquet_month e nao normalizava as
        # colunas), elas continuam no banco com um schema INCOMPATIVEL:
        #   resumo_cid_periodo(casos, parquet_month, parquet_year)
        # Como _summary_tables() so checava o NOME, ela anunciava essas tabelas
        # ao LLM como se tivessem (atend_ano, atend_mes, sexo, cid, casos). O LLM
        # entao gerava SQL CORRETO contra o schema ESPERADO
        #   ... FROM resumo_cid_periodo WHERE atend_ano = 2025 AND cid LIKE 'A90%'
        # mas o DuckDB falhava na execucao (so existiam parquet_year/parquet_month):
        #   Binder Error: ... does not have a column named "atend_ano"
        # Aqui VALIDAMOS o schema real e DROPAMOS qualquer resumo INCOMPATIVEL.
        # Assim ele nao e anunciado/usado ate ser reconstruido por
        # build_summary_tables() (que ja cria o schema correto).
        # Valida o schema COMPLETO que anunciamos ao LLM: se faltar qualquer
        # coluna normalizada esperada, a tabela e considerada obsoleta.
        required = {
            "resumo_diagnosticos": {"atend_ano", "atend_mes", "sexo", "cid", "casos"},
            "resumo_cid_periodo": {"atend_ano", "atend_mes", "sexo", "cid", "casos"},
            "resumo_anual_cid": {"atend_ano", "cid", "casos"},
        }
        healthy: list[str] = []
        for table in present:
            try:
                cols = {
                    row[0].lower()
                    for row in self.connection.execute(
                        f"DESCRIBE {table}"
                    ).fetchall()
                }
            except Exception as exc:
                LOGGER.warning("summary: falha no DESCRIBE de %s: %s", table, exc)
                continue
            missing = required.get(table, set()) - cols
            if missing:
                LOGGER.warning(
                    "summary: tabela %s tem schema OBSOLETO (colunas=%s; faltam %s); "
                    "descartando para ser reconstruída pelo próximo build_summary_tables().",
                    table, sorted(cols), sorted(missing),
                )
                try:
                    self.connection.execute(f"DROP TABLE IF EXISTS {table}")
                except Exception as exc:
                    LOGGER.warning("summary: falha ao DROP de %s: %s", table, exc)
                continue
            healthy.append(table)
        return sorted(healthy)

    def schema_context(self, relation: str = "datasus", include_examples: bool = True) -> dict[str, Any]:
        try:
            conn = self.connection
            schema_rows = conn.execute(f"DESCRIBE {relation}").fetchall()
        except duckdb.Error:
            return {"columns": []}
        schema = [{"column": row[0], "type": row[1]} for row in schema_rows]
        context: dict[str, Any] = {"columns": schema}
        likely_columns = [
            item["column"] for item in schema
            if any(token in item["column"].upper() for token in ("CID", "SEXO", "COMP", "DT", "ANO"))
        ]
        if likely_columns and include_examples:
            selected = ", ".join('"' + column.replace('"', '""') + '"' for column in likely_columns)
            try:
                context["examples"] = conn.execute(
                    f"SELECT {selected} FROM {relation} USING SAMPLE reservoir(20 ROWS)"
                ).fetchall()
                context["example_columns"] = likely_columns
            except duckdb.Error:
                LOGGER.exception("could not collect schema examples")
        return context

    @log_calls
    def execute(self, sql: str, request_id: str, limite: int | None = None) -> list[dict[str, Any]]:
        started = time.perf_counter()
        # >>> PARTE 7 (#8): respeita o limite REAL da pergunta. Antes o
        # fetchmany era FIXO em 100. Fallback: DEFAULT_FETCH_ROWS.
        _fetch_n = int(limite) if (limite and int(limite) > 0) else DEFAULT_FETCH_ROWS
        LOGGER.info("duckdb query start request_id=%s sql_length=%d sql=%s", request_id, len(sql), sql)
        uses_query_cache = bool(re.search(r"\bdatasus_query_cache\b", sql, flags=re.IGNORECASE))
        # S3: datasus_query_cache pode ser VIEW (DATASUS_UNIFIED_VIEW) ou TEMP
        # TABLE (modo materializado). information_schema.tables NAO enxerga
        # views temporarias, entao checamos views tambem.
        query_cache_exists = self._fetch_scalar(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'datasus_query_cache'",
            default=0,
        )
        if not query_cache_exists:
            query_cache_exists = self._fetch_scalar(
                "SELECT COUNT(*) FROM information_schema.views WHERE table_name = 'datasus_query_cache'",
                default=0,
            )
        if uses_query_cache and not query_cache_exists:
            raise RuntimeError("O cache filtrado da pergunta não foi preparado. Informe um ano ou mês válido.")
        LOGGER.info("duckdb query execution start request_id=%s", request_id)
        try:
            cursor = self.connection.execute(sql)
        except Exception as e:
            # >>> CORRECAO MELHORADA: Trata SQL truncado e strings nao terminadas <<<
            err_msg = str(e)
            # >>> CORRECAO (coluna ausente na temp table) <<<
            # Se a temp table foi criada por uma projecao minima de uma SQL
            # anterior e a SQL atual (refeita / revisada / com CID corrigido)
            # referencia outra coluna, o DuckDB falha com:
            #   Binder Error: Referenced column "X" not found in FROM clause!
            #   Candidate bindings: ...
            # Neste caso, reconstruimos a temp table da MESMA pergunta SEM a
            # projecao minima (todas as colunas), garantindo que a execucao
            # prossiga em vez de derrubar a requisicao.
            if (
                "Referenced column" in err_msg
                and "not found in FROM clause" in err_msg
                and uses_query_cache
                and getattr(self, "_last_question", None)
            ):
                missing = None
                try:
                    missing = re.search(r'Referenced column "([^"]+)"', err_msg)
                    missing = missing.group(1) if missing else None
                except Exception:
                    missing = None
                LOGGER.warning(
                    "coluna %r ausente na temp table datasus_query_cache; "
                    "reconstruindo cache completo (sem projeção mínima). request_id=%s",
                    missing, request_id,
                )
                try:
                    # >>> PARTE 7 (#9): reprojecao MINIMA. Em vez de
                    # reconstruir com sql=None (TODAS as ~469 colunas),
                    # passamos a coluna FALTANTE como extra_columns, mantendo
                    # a projecao minima (colunas usadas + faltante + periodo).
                    _extra = {missing} if missing else None
                    self.prepare_question_cache(
                        self._last_question, request_id, sql=self._last_sql,
                        extra_columns=_extra,
                    )
                    cursor = self.connection.execute(sql)
                    columns = [d[0] for d in cursor.description or []]
                    LOGGER.info(
                        "duckdb query complete (após reconstrução) request_id=%s columns=%s",
                        request_id, columns,
                    )
                    rows = [dict(zip(columns, row)) for row in cursor.fetchmany(_fetch_n)]
                    LOGGER.info(
                        "duckdb query complete request_id=%s rows_returned=%d "
                        "result_empty=%s duration_ms=%.2f",
                        request_id, len(rows), not rows,
                        (time.perf_counter() - started) * 1000,
                    )
                    return rows
                except Exception as rebuild_err:
                    LOGGER.error(
                        "falha ao reconstruir temp table para coluna %r: %s. "
                        "Propagando erro original. request_id=%s",
                        missing, rebuild_err, request_id,
                    )
                    raise e
            if "unterminated quoted string" in err_msg.lower() or "syntax error" in err_msg.lower():
                # Tenta multiplas correcoes em sequencia
                fixed_sql = self._fix_unterminated_sql_strings(sql)
                
                # >>> NOVO: Tenta corrigir SQL truncado/incompleto (parenteses nao fechados, etc.) <<<
                if fixed_sql == sql:
                    fixed_sql = self._fix_incomplete_sql_static(sql)
                
                if fixed_sql != sql:
                    LOGGER.warning("SQL corrigido no execute fallback request_id=%s sql=%s", request_id, fixed_sql)
                    try:
                        cursor = self.connection.execute(fixed_sql)
                        sql = fixed_sql
                    except Exception as e2:
                        LOGGER.error("falha ao executar SQL corrigido request_id=%s erro=%s", request_id, e2)
                        # >>> NOVO: Se nao conseguiu corrigir, usa fallback COUNT(*) <<<
                        fallback_sql = "SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT 100"
                        LOGGER.warning("usando fallback COUNT(*) request_id=%s", request_id)
                        cursor = self.connection.execute(fallback_sql)
                        sql = fallback_sql
                else:
                    # >>> NOVO: Se nao conseguiu corrigir, tenta re-escrever o SQL com uma query simples <<<
                    LOGGER.warning("SQL não pôde ser corrigido, usando fallback COUNT(*) request_id=%s", request_id)
                    fallback_sql = "SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT 100"
                    try:
                        cursor = self.connection.execute(fallback_sql)
                        sql = fallback_sql
                    except Exception as e3:
                        LOGGER.error("falha até mesmo no fallback COUNT(*) request_id=%s erro=%s", request_id, e3)
                        raise e3
            else:
                raise
        columns = [description[0] for description in cursor.description or []]
        LOGGER.info("duckdb query fetch start request_id=%s columns=%s", request_id, columns)
        rows = [dict(zip(columns, row)) for row in cursor.fetchmany(_fetch_n)]
        LOGGER.info("duckdb query complete request_id=%s rows_returned=%d limite=%d result_empty=%s duration_ms=%.2f", request_id, len(rows), _fetch_n, not rows, (time.perf_counter() - started) * 1000)
        return rows

    @staticmethod
    def _fix_unterminated_sql_strings(sql: str) -> str:
        """
        Corrige strings SQL nao terminadas (como LIKE ' sem fechamento).
        Retorna o SQL corrigido, ou o original se nao conseguir corrigir.
        """
        # Detecta strings nao balanceadas em aspas simples
        in_string = False
        escaped = False
        last_open_quote = -1
        
        for i, char in enumerate(sql):
            if char == "'":
                if escaped:
                    escaped = False
                    continue
                if in_string:
                    in_string = False
                else:
                    in_string = True
                    last_open_quote = i
            elif char == "\\" and in_string:
                escaped = True
            else:
                escaped = False
        
        if not in_string:
            return sql  # Strings balanceadas, nada a corrigir
        
        LOGGER.warning("SQL contém string não terminada. Tentando corrigir no execute...")
        
        prefix = sql[:last_open_quote]  # Tudo antes da aspas aberta
        rest = sql[last_open_quote:]   # Comeca com a aspas aberta
        after_quote = rest[1:] if rest.startswith("'") else rest
        
        # Caso 1: Padrao LIKE 'xxx  (sem fechamento)
        like_before = re.search(r"(?:LIKE|ILIKE)\s*$", prefix, flags=re.IGNORECASE)
        if like_before:
            value = after_quote
            if value and re.match(r"^[%A-Za-z0-9._\-\s]+$", value):
                fixed_sql = sql + "'"
                LOGGER.warning("SQL: LIKE incompleto fechado. value=%r", value)
                return fixed_sql
            if value and not value.strip():
                fixed_sql = sql + "'"
                LOGGER.warning("SQL: LIKE com string vazia completado")
                return fixed_sql
        
        # Caso 2: Termina com operador de comparacao (ex: = ')
        before = prefix.rstrip()
        if re.search(r"(?:=|<>|!=|<=|>=)\s*$", before, flags=re.IGNORECASE):
            if after_quote and re.match(r"^[%A-Za-z0-9._\-\s]+$", after_quote):
                fixed_sql = sql + "'"
                LOGGER.warning("SQL: string após operador fechada. value=%r", after_quote)
                return fixed_sql
            fixed_sql = sql[:last_open_quote] + "''" + sql[last_open_quote+1:]
            LOGGER.warning("SQL: string vazia após operador completada")
            return fixed_sql
        
        # Caso 3: Fallback - apenas fecha a string com aspas simples
        if last_open_quote >= 0:
            if after_quote and re.match(r"^[%A-Za-z0-9._\-\s]+$", after_quote):
                fixed_sql = sql + "'"
                if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                    fixed_sql = fixed_sql + " LIMIT 100"
                LOGGER.warning("SQL: string não terminada fechada. value=%r", after_quote)
                return fixed_sql
            fixed_sql = sql[:last_open_quote] + "''" + sql[last_open_quote+1:]
            if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                fixed_sql = fixed_sql + " LIMIT 100"
            LOGGER.warning("SQL: string não terminada fechada com aspas vazias")
            return fixed_sql
        
        return sql

    @staticmethod
    def _fix_incomplete_sql_static(sql: str) -> str:
        """
        Corrige SQL truncado/incompleto (parenteses nao fechados, falta de clausulas).
        Versao estatica que tenta completar o SQL de forma segura.
        """
        result = sql
        # Balanceia parenteses
        open_parens = result.count("(") - result.count(")")
        if open_parens > 0:
            LOGGER.warning("SQL com %d parênteses não fechados no execute. Fechando...", open_parens)
            result = result + ")" * open_parens
        
        # Se terminar com LIKE ' sem fechamento, completa
        if re.search(r"LIKE\s*'?\s*$", result, flags=re.IGNORECASE) or \
           re.search(r"(?:=|<>|!=)\s*'?\s*$", result, flags=re.IGNORECASE):
            result = result + "'"
        
        # Se nao tiver LIMIT, adiciona
        if not re.search(r"\bLIMIT\s+\d+\b", result, flags=re.IGNORECASE):
            result = result.rstrip()
            if result.endswith(";"):
                result = result[:-1]
            result = result + " LIMIT 100"
        
        # Se nao tiver FROM, usa fallback
        if not re.search(r"\bFROM\b", result, flags=re.IGNORECASE) and \
           re.search(r"\bSELECT\b", result, flags=re.IGNORECASE):
            result = "SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT 100"
            LOGGER.warning("SQL sem FROM, usando fallback COUNT(*)")
        
        return result

    def _datasus_view_exists(self) -> bool:
        result = self._fetch_scalar(
            "SELECT COUNT(*) FROM information_schema.views WHERE table_schema = 'main' AND table_name = 'datasus'",
            default=0,
        )
        return bool(result)

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._startup_error is None

    def startup_status(self) -> str | None:
        return self._startup_error

    def mark_ready(self, error: str | None = None) -> None:
        self._startup_error = error
        self._ready.set()

    def status(self) -> dict[str, Any]:
        started = time.perf_counter()
        LOGGER.info("status start grupos=%s", list(self.groups))
        if not self.is_ready():
            return {
                "files": 0,
                "rows": None,
                "database": str(self.database_path),
                "summary_tables": [],
                "groups": {},
                "state": "error" if self.startup_status() else "loading",
                "error": self.startup_status(),
            }
        if self.groups:
            group_status: dict[str, Any] = {}
            total_files = 0
            total_rows = 0
            for name, group in sorted(self.groups.items()):
                gf = group._scalar("SELECT COUNT(*) FROM file_manifest", default=0)
                total_files += int(gf)
                # _cache_relation_exists cobre TABLE (legado) e VIEW (S1).
                # Com VIEW, SELECT COUNT(*) funciona normalmente (le os Parquet).
                rel_exists = group._cache_relation_exists()
                g_rows = int(group._scalar("SELECT COUNT(*) FROM datasus_cache", default=0)) if rel_exists else 0
                total_rows += g_rows
                group_status[name] = {
                    "files": int(gf),
                    "rows": g_rows,
                    "database": str(group.db_path),
                    "cache_table": rel_exists,
                }
            LOGGER.info("status complete files=%d rows=%d duration_ms=%.2f", total_files, total_rows, (time.perf_counter() - started) * 1000)
            return {
                "files": total_files,
                "rows": total_rows,
                "database": str(self.database_path),
                "groups": group_status,
                "summary_tables": self._summary_tables(),
            }
        files = self._fetch_scalar("SELECT COUNT(*) FROM file_manifest", default=0)
        rows = self._cached_row_count()
        if rows is None and self._cache_table_exists():
            LOGGER.info("status row count start source=persisted")
            rows = self._fetch_scalar("SELECT COUNT(*) FROM datasus_cache", default=0)
            self._store_row_count(rows)
        LOGGER.info("status complete files=%d rows_known=%s duration_ms=%.2f", files, rows is not None, (time.perf_counter() - started) * 1000)
        return {"files": files, "rows": rows, "database": str(self.database_path), "groups": {}, "summary_tables": self._summary_tables()}

    def _open_group_if_needed(self, name: str) -> None:
        """Abre a conexao DuckDB de um grupo se ela ainda nao estiver aberta."""
        group = self.groups.get(name)
        if group is None:
            return
        try:
            if group.connection is None:
                group.reopen()
                LOGGER.info("grupo %s: conexão aberta sob demanda", name)
        except Exception as e:
            LOGGER.error("grupo %s: falha ao abrir conexão: %s", name, e)
            # >>> CORRECAO: So cai para ':memory:' quando NAO ha conexao principal
            # (modo isolado). Com conexao principal, o problema e o ATTACH do schema
            # do grupo; mascarar como banco em memoria faz as consultas qualificadas
            # (ex.: pa.file_manifest) falharem depois, de forma confusa. Propaga o erro.
            if group.main_connection is None:
                try:
                    group.db_path = None
                    group._using_main_connection = False
                    group.connection = duckdb.connect(":memory:")
                    group._configure_memory()
                    LOGGER.warning("grupo %s: usando banco em memória como fallback", name)
                except Exception as e2:
                    LOGGER.error("grupo %s: falha ao abrir banco em memória: %s", name, e2)
                    raise
            else:
                raise

    def _cached_row_count(self) -> int | None:
        value = self._fetch_scalar("SELECT value FROM cache_metadata WHERE key = 'row_count'")
        return None if value is None else int(value)

    def _store_row_count(self, rows: int) -> None:
        self.connection.execute("INSERT OR REPLACE INTO cache_metadata VALUES ('row_count', ?)", [rows])

    def discover_files(self) -> list[Path]:
        files = _discover_files_in(self.settings.data_dir)
        LOGGER.info(
            "discovered parquet_files=%d data_dir=%s exists=%s",
            len(files), self.settings.data_dir, self.settings.data_dir.exists(),
        )
        return files

    def _save_checkpoint(self, group: str, batch_index: int, total: int) -> None:
        """Salva checkpoint de progresso da ingestao."""
        self.connection.execute(
            "INSERT OR REPLACE INTO cache_checkpoint (key, value) VALUES (?, ?)",
            [f"{group}:last_batch_index", str(batch_index)]
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO cache_checkpoint (key, value) VALUES (?, ?)",
            [f"{group}:total_files", str(total)]
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO cache_checkpoint (key, value) VALUES (?, ?)",
            [f"{group}:last_updated_at", str(time.time())]
        )

    def _load_checkpoint(self, group: str) -> int:
        """Carrega o ultimo checkpoint salvo para o grupo. Retorna o indice do ultimo lote."""
        row = self.connection.execute(
            "SELECT value FROM cache_checkpoint WHERE key = ?",
            [f"{group}:last_batch_index"]
        ).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)

    def _ingest_locked(self, started: float) -> dict[str, Any]:
        all_files = self.discover_files()
        if not all_files:
            LOGGER.warning("Nenhum arquivo parquet encontrado para ingestão.")
            return {"files": 0, "status": "no_files_found"}

        manifest: dict[str, tuple[int, float, str, int]] = {
            source: (size, modified, cached, in_db)
            for source, size, modified, cached, in_db in self.connection.execute(
                "SELECT source, size, modified, cached, in_db FROM file_manifest"
            ).fetchall()
        }

        already_cached = 0
        to_ingest: list[Path] = []
        for source in all_files:
            relative_str = str(source.relative_to(self.settings.data_dir))
            record = manifest.get(relative_str)
            scache = _retry_stat(source)
            if scache is None:
                LOGGER.warning("arquivo indisponível (USB?) %s; adiado para o próximo ingest", source)
                continue
            target = _grouped_cache_path(self.settings.cache_dir, "parquet", source)
            if (
                record is not None
                and record[0] == scache.st_size
                and record[1] == scache.st_mtime
                and target.exists()
                and record[3] == 1
            ):
                already_cached += 1
                continue
            to_ingest.append(source)

        total_to_ingest = len(to_ingest)
        LOGGER.info(
            "Ingestão incremental iniciada: %d novos/alterações pendentes de %d totais "
            "(já em cache: %d)", total_to_ingest, len(all_files), already_cached,
        )

        if total_to_ingest == 0:
            if not self._cache_table_exists():
                to_ingest = all_files
                total_to_ingest = len(to_ingest)
            else:
                LOGGER.info("Cache já está sincronizado com os arquivos locais.")
                if not self._datasus_view_exists():
                    self.connection.execute("CREATE VIEW datasus AS SELECT * FROM datasus_cache")
                return {
                    "files": len(all_files),
                    "pending": 0,
                    "already_cached": already_cached,
                    "changed": 0,
                    "cache_hit": True,
                }

        batch_size = 50
        changed = 0
        invalid = 0

        group_key = "legacy" if not self.groups else "_global"
        checkpoint_index = self._load_checkpoint(group_key)
        if checkpoint_index > 0:
            LOGGER.info("retomando de checkpoint no índice %d (de %d)", checkpoint_index, len(to_ingest))
            checkpoint_index = min(checkpoint_index, len(to_ingest))

        for i in range(checkpoint_index, len(to_ingest), batch_size):
            batch = to_ingest[i:i + batch_size]
            batch_cached_paths: list[str] = []
            batch_sources: list[str] = []

            for source in batch:
                relative = source.relative_to(self.settings.data_dir)
                relative_str = str(relative)
                target = _grouped_cache_path(self.settings.cache_dir, "parquet", source)
                stat = _retry_stat(source)
                if stat is None:
                    continue
                try:
                    if not _retry_copyfile(source, target):
                        raise OSError("cópia incompleta do arquivo-fonte")
                    self._validate_parquet(target)
                    self._validate_parquet(target)

                    self.connection.execute(
                        "INSERT OR REPLACE INTO file_manifest "
                        "(source, size, modified, cached, in_db) VALUES (?, ?, ?, ?, 0)",
                        [relative_str, stat.st_size, stat.st_mtime, str(target)],
                    )
                    batch_cached_paths.append(str(target))
                    batch_sources.append(relative_str)
                    changed += 1

                    year_month = self._extract_period_from_path(target)
                    processed_total = checkpoint_index + changed
                    percent = (processed_total / total_to_ingest * 100) if total_to_ingest > 0 else 0
                    LOGGER.info(
                        "CACHE GRAVADO | modo=legado | arquivo=%s | parquet=%s | partição=%s | progresso=%d/%d (%.1f%%)",
                        source.name,
                        target.name,
                        year_month or "N/A",
                        processed_total,
                        total_to_ingest,
                        percent,
                    )
                except Exception as error:
                    invalid += 1
                    self._remove_invalid_file(source, target, error)

            if batch_cached_paths:
                paths_sql = ", ".join("'" + p.replace("'", "''") + "'" for p in batch_cached_paths)

                if not self._cache_table_exists():
                    self.connection.execute(
                        "CREATE TABLE datasus_cache AS SELECT *, filename AS parquet_path, "
                        "2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])', 1), '') AS INTEGER) AS parquet_year, "
                        "NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])', 2), '') AS parquet_month "
                        "FROM read_parquet([" + paths_sql + "], union_by_name = true, filename = true)"
                    )
                    self.connection.execute(
                        "UPDATE datasus_cache SET parquet_year = 2000 + CAST(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])', 1) AS INTEGER), "
                        "parquet_month = regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])', 2) WHERE parquet_year IS NULL"
                    )
                else:
                    self.connection.execute("DROP VIEW IF EXISTS temp_batch")
                    self.connection.execute(
                        "CREATE VIEW temp_batch AS SELECT * FROM read_parquet([" + paths_sql + "], union_by_name = true)"
                    )
                    existing_cols = {
                        row[0].upper(): row[0]
                        for row in self.connection.execute("DESCRIBE datasus_cache").fetchall()
                    }
                    new_cols = self.connection.execute("DESCRIBE temp_batch").fetchall()
                    for col_name, col_type, *_ in new_cols:
                        if col_name.upper() not in existing_cols:
                            LOGGER.info("Evolução de schema: adicionando coluna %s (%s)", col_name, col_type)
                            self.connection.execute(
                                f'ALTER TABLE datasus_cache ADD COLUMN "{col_name}" {col_type}'
                            )
                    table_cols = [row[0] for row in _schema_rows(self.connection, "datasus_cache")]
                    batch_cols = _schema_rows(self.connection, "temp_batch")
                    batch_by_upper = {row[0].upper(): row[0] for row in batch_cols}
                    projection = []
                    for col in table_cols:
                        escaped = col.replace('"', '""')
                        if col.upper() in batch_by_upper:
                            projection.append(
                                '"' + batch_by_upper[col.upper()].replace('"', '""') + '"'
                            )
                        else:
                            projection.append('CAST(NULL AS VARCHAR) AS "' + escaped + '"')
                    col_list = ", ".join('"' + col.replace('"', '""') + '"' for col in table_cols)
                    proj_list = ", ".join(projection)
                    self.connection.execute(
                        f"INSERT INTO datasus_cache ({col_list}) SELECT {proj_list} FROM temp_batch"
                    )
                    self.connection.execute("DROP VIEW temp_batch")

                self._mark_in_db(batch_sources)

            self._save_checkpoint(group_key, i + len(batch), len(to_ingest))
            LOGGER.info("Lote processado: %d/%d arquivos", min(i + batch_size, len(to_ingest)), len(to_ingest))

        if self._cache_table_exists():
            if not self._datasus_view_exists():
                self.connection.execute("CREATE VIEW datasus AS SELECT * FROM datasus_cache")
            rows = self._fetch_scalar("SELECT COUNT(*) FROM datasus_cache", default=0)
            self._store_row_count(rows)
            self._refresh_period_index(self._cached_parquet_files())
        else:
            rows = 0

        duration = (time.perf_counter() - started) * 1000
        LOGGER.info(
            "Ingestão completa: %d novos, %d inválidos, %d linhas, %d já em cache, duração %.2fms",
            changed, invalid, rows, already_cached, duration,
        )

        self._invalidate_canonical_schema()

        return {
            "files": changed,
            "pending": total_to_ingest,
            "already_cached": already_cached,
            "invalid": invalid,
            "rows": rows,
            "cache_hit": False,
        }

    def _mark_in_db(self, sources: list[str]) -> None:
        """Marca no file_manifest que os arquivos foram efetivamente gravados no banco."""
        if not sources:
            return
        escaped = ", ".join("'" + src.replace("'", "''") + "'" for src in sources)
        self.connection.execute(
            "UPDATE file_manifest SET in_db = 1 WHERE source IN (" + escaped + ")"
        )

    def _extract_period_from_path(self, path: Path) -> str | None:
        """Extrai o periodo (ano/mes) do caminho do cache agrupado.

        Estrutura: {cache_dir}/{grupo}/{ano}/{mes}/{arquivo}.parquet
        """
        try:
            parts = path.parts
            if len(parts) >= 3:
                year = parts[-3]
                month = parts[-2]
                if year.isdigit() and len(year) == 4 and month.isdigit() and len(month) == 2:
                    return f"{year}/{month}"
        except (IndexError, ValueError):
            return None
        return None

    def _remove_invalid_file(self, source: Path, target: Path, error: Exception) -> None:
        LOGGER.error("parquet read failed source=%s cache=%s error=%s", source, target, error)
        try:
            target.unlink(missing_ok=True)
            LOGGER.warning("removed invalid cached parquet path=%s", target)
        except OSError:
            LOGGER.exception("could not remove invalid cached parquet path=%s", target)
        self.connection.execute(
            "DELETE FROM file_manifest WHERE source = ?",
            [str(source.relative_to(self.settings.data_dir))],
        )

    def _validate_parquet(self, path: Path) -> None:
        """Valida que o Parquet pode ser lido pelo DuckDB (best-effort)."""
        self.connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)])

    def ingest(self, build_summary: bool = False) -> dict[str, Any]:
        """Ingere os arquivos no cache DuckDB.

        Em modo GRUPOS (self.groups nao vazio) delega a ingestao a cada grupo
        (``pending_batch``/``import_batch``/``finalize``), abrindo e fechando a
        conexao de cada um para manter a memoria sob controle. Sem grupos, usa
        o caminho legado (``_ingest_locked``).
        """
        started = time.perf_counter()
        LOGGER.info("cache ingest start grupos=%s", list(self.groups))
        groups_result: dict[str, Any] = {}
        with self._ingest_lock:
            if self.groups:
                PER_ITERATION = 50
                stats: dict[str, dict[str, Any]] = {
                    name: {
                        "files": 0,
                        "invalid": 0,
                        "already_cached": 0,
                        "pending": 0,
                        "initial_already": None,
                        "initial_pending": None,
                        "cache_hit": True,
                    }
                    for name in self.groups
                }
                for name in sorted(self.groups.keys()):
                    group = self.groups[name]
                    LOGGER.info("processando grupo %s (abre conexão, processa, fecha)", name)
                    self._open_group_if_needed(name)
                    try:
                        while True:
                            batch, pend_total, already = group.pending_batch(limit=PER_ITERATION)
                            if stats[name]["initial_pending"] is None:
                                stats[name]["initial_pending"] = pend_total
                                stats[name]["initial_already"] = already
                            if not batch:
                                break
                            res = group.import_batch(batch)
                            stats[name]["files"] += res["files"]
                            stats[name]["invalid"] += res["invalid"]
                            stats[name]["cache_hit"] = False
                            self._save_checkpoint(name, stats[name]["files"], pend_total)
                            LOGGER.info("Grupo %s: lote de %d arquivos importado", name, len(batch))
                        group.finalize()
                    except Exception as e:
                        LOGGER.exception("erro ao processar grupo %s: %s", name, e)
                    finally:
                        group.close()
                        LOGGER.info("Grupo %s processado e conexão fechada", name)

                groups_result = {
                    name: {
                        "group": name,
                        "files": stat["files"],
                        "invalid": stat["invalid"],
                        "already_cached": stat["initial_already"] or 0,
                        "cache_hit": stat["cache_hit"],
                    }
                    for name, stat in stats.items()
                }
                result: dict[str, Any] = {
                    "files": sum(stat["files"] for stat in stats.values()),
                    "pending": sum((stat["initial_pending"] or 0) for stat in stats.values()),
                    "already_cached": sum((stat["initial_already"] or 0) for stat in stats.values()),
                    "cache_hit": all(stat["cache_hit"] for stat in stats.values()),
                    "groups": groups_result,
                }
            else:
                result = self._ingest_locked(started)

            if build_summary and not result.get("cache_hit", False):
                result["indexes"] = self.build_summary_tables()

            if not result.get("cache_hit", False):
                try:
                    self.refresh_column_dictionary(persist=True)
                except Exception as e:
                    LOGGER.warning("falha ao atualizar dicionário de colunas após ingest: %s", e)

            synced = _sync_to_official_cache(
                self.settings.cache_dir, self.settings.official_cache_dir
            )
            result["synced_to_official"] = synced
            # >>> PARTE 6 (#7): pre-computa os prefixos de CID apos a ingestao <<<
            try:
                result["cid_prefixes"] = self._populate_cid_prefixes()
            except Exception as e:
                LOGGER.warning("falha ao pre-computar prefixos de CID: %s", e)
        return result

    def build_summary_tables(self) -> dict[str, Any]:
        """Constroi as tabelas agregadas resumo_* a partir dos Parquet cacheados."""
        started = time.perf_counter()
        with self._build_lock:
            return self._build_summary_tables_locked(started)

    def _build_summary_tables_locked(self, started: float) -> dict[str, Any]:
        files = self._cached_parquet_files()
        if not files:
            raise RuntimeError("Não existem arquivos Parquet no cache para materializar os resumos.")
        LOGGER.info("summary build start files=%d", len(files))
        if self.connection is None:
            raise RuntimeError("A conexão DuckDB não está disponível para construir os resumos.")

        self.connection.execute("DROP TABLE IF EXISTS resumo_diagnosticos")
        self.connection.execute("DROP TABLE IF EXISTS resumo_cid_periodo")
        self.connection.execute("DROP TABLE IF EXISTS resumo_anual_cid")
        self.connection.execute(
            f"CREATE TABLE resumo_diagnosticos ("
            f"{ATEND_ANO} INTEGER, {ATEND_MES} VARCHAR, sexo VARCHAR, cid VARCHAR, casos BIGINT)"
        )

        periods_found = 0

        # Candidatos a coluna nativa de periodo: todas as colunas declaradas em
        # db/period_fields.py (SP_DTINTER, DT_INTER, DT_ATEND, PA_CMP, AP_DTINIC).
        _period_cols_candidates: list[str] = []
        for _pf in PERIOD_FIELDS.values():
            for _c in _pf.columns:
                if _c not in _period_cols_candidates:
                    _period_cols_candidates.append(_c)

        for path in files:
            periods_found += 1
            try:
                self.connection.execute("DROP VIEW IF EXISTS summary_source")
                quoted_path = "'" + str(path).replace("'", "''") + "'"
                self.connection.execute(
                    "CREATE TEMP VIEW summary_source AS SELECT * FROM read_parquet(["
                    + quoted_path
                    + "], union_by_name = true, filename = true)"
                )

                available = {
                    row[0]
                    for row in self.connection.execute("DESCRIBE summary_source").fetchall()
                }
                available_upper = {col.upper(): col for col in available}

                # Descobre a coluna nativa de periodo presente neste arquivo.
                period_col = None
                for cand in _period_cols_candidates:
                    if cand.upper() in available_upper:
                        period_col = available_upper[cand.upper()]
                        break

                if period_col is None:
                    self.connection.execute("DROP VIEW IF EXISTS summary_source")
                    continue

                ano_sql = ano_expr(period_col)
                mes_sql = mes_expr(period_col)
                diagnosis_columns = [
                    available_upper[col_name.upper()]
                    for col_name in DIAGNOSIS_COLUMN_NAMES
                    if col_name.upper() in available_upper
                ]

                if not diagnosis_columns:
                    self.connection.execute("DROP VIEW IF EXISTS summary_source")
                    continue

                sex_expression = (
                    'CAST("AP_SEXO" AS VARCHAR)' if "AP_SEXO" in available
                    else "CAST(NULL AS VARCHAR)"
                )
                diagnosis_selects = []
                for column in diagnosis_columns:
                    escaped = column.replace('"', '""')
                    diagnosis_selects.append(
                        "SELECT " + ano_sql + f" AS {ATEND_ANO}, "
                        + mes_sql + f" AS {ATEND_MES}, "
                        + sex_expression + " AS sexo, "
                        + 'UPPER(TRIM(CAST("' + escaped + '" AS VARCHAR))) AS cid FROM summary_source '
                        + 'WHERE "' + escaped + '" IS NOT NULL AND TRIM(CAST("' + escaped + '" AS VARCHAR)) <> '
                        + "''"
                    )

                batch_sql = (
                    "INSERT INTO resumo_diagnosticos SELECT "
                    + f"{ATEND_ANO}, {ATEND_MES}, sexo, cid, COUNT(*) AS casos FROM ("
                    + " UNION ALL ".join(diagnosis_selects)
                    + ") AS diagnoses GROUP BY 1, 2, 3, 4"
                )
                self.connection.execute(batch_sql)
                self.connection.execute("DROP VIEW IF EXISTS summary_source")
            except Exception as e:
                LOGGER.error("Erro ao resumir arquivo %s: %s. Pulando...", path, e)
                self._drop_summary_source()

        if periods_found == 0:
            self._drop_summary_source()
            raise RuntimeError("Nenhum Parquet com coluna nativa de período foi encontrado.")

        self.connection.execute(
            f"CREATE OR REPLACE TABLE resumo_cid_periodo AS SELECT {ATEND_ANO}, {ATEND_MES}, sexo, cid, casos FROM resumo_diagnosticos"
        )
        self.connection.execute(
            f"CREATE OR REPLACE TABLE resumo_anual_cid AS SELECT {ATEND_ANO}, cid, SUM(casos) AS casos FROM resumo_diagnosticos GROUP BY 1, 2"
        )
        self.connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_resumo_diagnosticos_periodo ON resumo_diagnosticos ({ATEND_ANO}, {ATEND_MES}, cid)"
        )
        self.connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_resumo_anual_cid ON resumo_anual_cid ({ATEND_ANO}, cid)"
        )
        self._drop_summary_source()
        LOGGER.info(
            "summary build complete files=%d tables=%s duration_ms=%.2f",
            len(files), self._summary_tables(), (time.perf_counter() - started) * 1000,
        )
        return {
            "files": len(files),
            "tables": self._summary_tables(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def _drop_summary_source(self) -> None:
        try:
            self.connection.execute("DROP VIEW IF EXISTS summary_source")
        except Exception:
            LOGGER.debug("summary_source já não existe", exc_info=True)
