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

from config.settings import DATASUS_DATA_DIR
from config.constants import DEFAULT_FETCH_ROWS, FILE_METADATA_COLUMNS
from core.io_utils import _discover_files_in, _sync_to_official_cache
from core.logging_config import log_calls
from core.path_utils import _migrate_legacy_cache_paths
from db import cid_prefixes as cid_prefixes_mod
from db import group_registry
from db import unified_view
from db import summary as summary_mod
from db.period_parser import parse_periods, wants_monthly_grouping
from db.sql_repair import fix_incomplete_sql, fix_unterminated_strings
from db.group_cache import _GroupCache
from db.column_dictionary import ColumnDictionaryStore, persist_to_duckdb
from db.period_fields import (
    PERIOD_FIELDS,
    ATEND_ANO,
    ATEND_MES,
    ano_expr,
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



class DatasusCache:
    """Cache DuckDB da aplicacao: banco principal + um banco ANEXADO por grupo.

    O banco principal (`datasus.duckdb`) guarda `file_manifest`,
    `cache_metadata`, os resumos `resumo_*` e a tabela `cid10`. Cada grupo do
    DATASUS (``er``, ``sp``, ``pa``...) vive em ``<grupo>.duckdb``, ANEXADO como
    schema (`<grupo>.datasus_cache`), e a view unificada
    `datasus_query_cache` e um ``UNION ALL`` dos grupos selecionados para a
    pergunta (ver :meth:`prepare_question_cache`).

    A ingestao e incremental (``pending_batch``/``import_batch``) e a coerencia
    e controlada por `<grupo>.file_manifest` (chave ``source``).
    """

    def __init__(self, settings: Any) -> None:
        """Abre o banco principal, anexa os grupos e prepara a tabela de CID.

        Args:
            settings: objeto de configuracoes (``config.settings.setting`` ou um
                equivalente em testes) com ``data_dir``/``cache_dir`` etc.

        Efeitos colaterais: cria/abre ``<cache_dir>/datasus.duckdb``, registra os
        ``_GroupCache`` detectados, carrega a tabela ``cid10`` (best-effort) e
        inicializa os locks/estado de prontidao usados por
        :meth:`ensure_ready_sync`.
        """
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
            self.sync_groups()
        except Exception as e:
            LOGGER.error("erro ao descobrir grupos: %s", e)
            self.groups = {}

        # Anexa os bancos dos grupos como schemas da conexao principal.
        # attach() e o UNICO ponto de ATTACH (idempotente, com fallback RO).
        for name, group in self.groups.items():
            try:
                group.attach()
            except Exception as e:
                LOGGER.error("falha ao anexar grupo %s: %s", name, e, exc_info=True)
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
            cid_prefixes_mod.load_cid10(self.connection)
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
        """Define o caminho do banco principal (usado por testes e reabertura).

        Args:
            value: novo caminho de ``datasus.duckdb``.
        """
        self._db_path = value

    def _configure_memory(self) -> None:
        """Configura a memoria/threads/spill/checkpoint do DuckDB (centralizada em Settings).

        Alem de `memory_limit`/`threads`/`temp_directory`, desativa
        `preserve_insertion_order` (menos estado em memoria) e define
        `checkpoint_threshold='4GB'` (WAL limitado) - PLANO_PERFORMANCE_CACHE.md, Etapas 1-4.
        """
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
            # 3) Etapa 4 (PLANO_PERFORMANCE_CACHE.md): limita o WAL e evita o
            #    checkpoint gigante single-thread no close().
            try:
                self.connection.execute("SET checkpoint_threshold='4GB'")
            except Exception:
                pass
        except Exception as e:
            LOGGER.warning("erro ao configurar memória do DuckDB: %s", e)


    def canonical_schema(self) -> tuple[set[str], dict[str, set[str]]]:
        """Retorna a uniao das colunas e as colunas por grupo.

        Usa UMA consulta a ``duckdb_columns()`` (antes eram 2 consultas de
        catalogo + 1 DESCRIBE por grupo). O resultado e memoizado em
        ``self._canonical_schema_cache`` e invalidado por
        :meth:`invalidate_schema` em mudancas estruturais.

        Returns:
            tuple[set[str], dict[str, set[str]]]: ``(all_columns,
            group_columns)``. ``all_columns`` inclui atend_ano/atend_mes;
            ``group_columns`` e ``{safe_name: set(colunas)}``.
        """
        cached = self._canonical_schema_cache
        if cached is not None:
            return cached["all_columns"], cached["group_columns"]

        colunas_por_db: dict[str, set[str]] = {}
        try:
            linhas = self.connection.execute(
                "SELECT database_name, column_name FROM duckdb_columns() "
                "WHERE table_name = 'datasus_cache'"
            ).fetchall()
        except Exception as e:
            LOGGER.warning("canonical_schema: falha ao ler duckdb_columns: %s", e)
            linhas = []
        for database_name, column_name in linhas:
            if not database_name or not column_name:
                continue
            colunas_por_db.setdefault(str(database_name), set()).add(str(column_name))

        metadados = {"filename", "parquet_path", "parquet_year", "parquet_month"}
        all_columns: set[str] = set()
        group_columns: dict[str, set[str]] = {}
        safe_names: list[str] = []
        safe_to_name: dict[str, str] = {}
        for name in self.groups:
            safe_name = name.replace('"', '""')
            cols = colunas_por_db.get(name) or colunas_por_db.get(safe_name)
            if not cols:
                continue
            cols = set(cols) - metadados
            group_columns[safe_name] = cols
            safe_names.append(safe_name)
            safe_to_name[safe_name] = name
            all_columns.update(cols)

        all_columns.add(ATEND_ANO)
        all_columns.add(ATEND_MES)
        self._canonical_schema_cache = {
            "all_columns": all_columns,
            "group_columns": group_columns,
            "safe_names": safe_names,
            "safe_to_name": safe_to_name,
        }
        LOGGER.info(
            "canonical_schema computado: grupos=%d colunas=%d",
            len(safe_names), len(all_columns),
        )
        return all_columns, group_columns

    def invalidate_schema(self) -> None:
        """Descarta o cache do schema canonico.

        Chamar apos mudanca estrutural (nova ingestao, attach/detach de grupo,
        rebuild de tabelas) para que a proxima consulta recompute.
        """
        if self._canonical_schema_cache is not None:
            LOGGER.info("canonical_schema invalidado")
        self._canonical_schema_cache = None



    # >>> PARTE 6 (#7): PREFIXOS DE CID PRE-COMPUTADOS <<<


    def get_cid_prefixes(self, min_len: int = 2) -> list[str]:
        """Le os prefixos de CID pre-computados (compatibilidade).

        Args:
            min_len: tamanho minimo do prefixo (>= 2).

        Returns:
            list[str]: prefixos conhecidos, ordenados.
        """
        return cid_prefixes_mod.read_prefixes(self.connection, min_len)


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

        except Exception as e:
            LOGGER.error("grupo %s: erro ao ler colunas: %s", grupo_name, e)
            cols = []

        # >>> CORRECAO (parquet_year/parquet_month): REMOVE metadados de arquivo <<<
        # O dicionario de colunas (usado no prompt do LLM e na validacao de
        # colunas) era construido a partir de `DESCRIBE <grupo>.datasus_cache`,
        # que inclui as colunas de METADADOS DE ARQUIVO (filename, parquet_path,
        # parquet_year, parquet_month). Esses metadados vem do NOME do arquivo
        # Parquet (competencia de remessa), NAO sao datas reais de atendimento e
        # NUNCA devem ser usados para filtrar/agrupar consultas - o recorte
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

    def sync_groups(self) -> list[str]:
        """Reconcilia o catalogo de grupos com o disco e registra os caches.

        Usa ``db.group_registry.sync`` para descobrir grupos em
        ``cache_dir/*.duckdb`` e nas subpastas de ``cache_dir``/``data_dir``
        (cobre o caso de grupo com Parquet mas sem banco .duckdb, ex.: ab/abo).
        Cria um ``_GroupCache`` por grupo e garante o ATTACH de todos.

        Returns:
            list[str]: nomes dos grupos registrados, ordenados.
        """
        cache_dir = self.settings.cache_dir
        data_dir = getattr(self.settings, "data_dir", None)
        nomes = group_registry.sync(self.connection, cache_dir, data_dir)
        registros = group_registry.registrados(self.connection)

        self.groups = {}
        for nome in nomes:
            registro = registros.get(nome, {})
            origem = registro.get("source_dir")
            source_dir = Path(origem) if origem else cache_dir / nome
            # Migra caches existentes para a estrutura ano/mes, se necessario.
            _migrate_legacy_cache_paths(cache_dir, nome)
            self.groups[nome] = _GroupCache(
                nome, source_dir, cache_dir, self.connection
            )

        for name, group in self.groups.items():
            try:
                group.attach()
            except Exception as exc:
                LOGGER.error(
                    "grupo %s: falha ao anexar banco: %s", name, exc, exc_info=True
                )

        # O conjunto de grupos (e portanto o schema canonico) mudou.
        self.invalidate_schema()
        LOGGER.info("grupos sincronizados: %s", sorted(self.groups))
        return sorted(self.groups)

    @log_calls
    def group(self, name: str) -> _GroupCache:
        """Retorna o cache do grupo, garantindo a conexao aberta.

        Args:
            name: nome do grupo (chave de ``self.groups``).

        Returns:
            _GroupCache: grupo pronto para consultas (schema anexado).

        Raises:
            KeyError: quando o grupo nao esta registrado no cache.
        """
        grupo = self.groups[name]
        grupo.ensure()
        return grupo

    def groups_needing_ingest(self) -> list[str]:
        """Retorna os grupos que precisam de ingestao na inicializacao.

        Le o catalogo sem abrir conexao de grupo: um grupo precisa de ingestao
        quando nao esta com status 'ok' no catalogo ou quando ha mais Parquet
        na origem do que arquivos registrados no manifesto.

        Returns:
            list[str]: nomes dos grupos pendentes.
        """
        registros = group_registry.registrados(self.connection)
        needing: list[str] = []
        for name, grupo in self.groups.items():
            registro = registros.get(name)
            if not registro or registro.get("status") != "ok":
                needing.append(name)
                continue
            try:
                no_disco = len(_discover_files_in(grupo.source_dir))
            except OSError as exc:
                LOGGER.warning("grupo %s: falha ao listar origem: %s", name, exc)
                needing.append(name)
                continue
            if no_disco > int(registro.get("files") or 0):
                needing.append(name)
        LOGGER.info("[groups_needing_ingest] pendentes: %s", needing or "nenhum")
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
        existirem grupos pendentes, levanta ``RuntimeError`` - assim o lifespan
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
    columns = get_grupo_columns

    def get_column_dictionary(self):
        """Retorna o dicionario consolidado de colunas (em memoria, cacheado).

        O dicionario e reconstruido apenas quando o cache de colunas por grupo
        muda (comparacao por assinatura), evitando reprocessamento a cada
        requisicao do LLMQueryService.
        """
        # Garante que o cache por grupo esta carregado.
        if not hasattr(self, "_columns_cache") or self._columns_cache is None:
            self._columns_cache = self._build_all_grupo_columns_caches()
        cache_dir = str(self.settings.cache_dir)
        try:
            dictionary = self._column_dict_store.get(
                self._columns_cache, cache_dir=cache_dir
            )
        except Exception as e:
            LOGGER.warning("get_column_dictionary: falha ao obter do store (%s); reconstruindo.", e)
            dictionary = ColumnDictionaryStore().get(
                self._columns_cache, cache_dir=cache_dir
            )
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






    def periods_for_question(self, question: str) -> list[tuple[str, str, str]]:
        """Resolve ``(grupo, ano, mes)`` do recorte a partir do catalogo.

        Le ``group_periods`` (uma consulta) e aplica o recorte temporal da
        pergunta. Quando o catalogo ainda nao tem periodos (primeira execucao
        antes do finalize), cai no DISTINCT por grupo.

        Args:
            question: pergunta do usuario.

        Returns:
            list[tuple[str, str, str]]: ``(grupo, ano, mes)`` selecionados.
        """
        disponiveis = group_registry.periodos(self.connection)
        if not disponiveis:
            disponiveis = self._periodos_dos_grupos()
        if not disponiveis:
            return []
        periods_available = {(str(ano), str(mes)) for _, ano, mes in disponiveis}
        try:
            years, months, recent, agrupar = parse_periods(
                question, periods_available
            )
        except ValueError as exc:
            LOGGER.warning(
                "pergunta sem periodo interpretavel (%s); usando todos os %d "
                "periodos do catalogo.", exc, len(periods_available),
            )
            years, months, recent, agrupar = set(), set(), None, False
        _ = agrupar  # reservado para telemetria de agrupamento mensal
        selecionados: list[tuple[str, str, str]] = []
        for grupo, ano, mes in disponiveis:
            ano_str = str(ano)
            mes_str = str(mes).zfill(2)
            periodo = (ano_str, mes_str)
            if recent is not None and periodo not in recent:
                continue
            if years and ano_str[-2:] not in years:
                continue
            if months and mes_str not in months:
                continue
            selecionados.append((grupo, ano_str, mes_str))
        selecionados.sort()
        LOGGER.info(
            "periods_for_question: %d de %d grupos=%d years=%s months=%s",
            len(selecionados), len(disponiveis), len(self.groups), years, months,
        )
        return selecionados

    def _periodos_dos_grupos(self) -> list[tuple[str, str, str]]:
        """Le ``(grupo, ano, mes)`` direto das tabelas (fallback sem catalogo).

        Returns:
            list[tuple[str, str, str]]: periodos encontrados, ordenados.
        """
        achados: set[tuple[str, str, str]] = set()
        for name in self.groups:
            pf = PERIOD_FIELDS.get(name)
            if pf is None:
                continue
            pcol = pf.columns[0]
            try:
                grupo = self.group(name)
                tabela = (
                    "datasus_cache" if not grupo._using_main_connection
                    else f"{name}.datasus_cache"
                )
                rows = grupo.connection.execute(
                    f"SELECT DISTINCT {ano_expr(pcol)} AS a, {mes_expr(pcol)} AS m "
                    f"FROM {tabela} WHERE \"{pcol}\" IS NOT NULL"
                ).fetchall()
            except Exception as exc:
                LOGGER.debug(
                    "grupo %s: fallback de periodos indisponivel: %s", name, exc
                )
                continue
            for ano, mes in rows:
                if ano and mes:
                    achados.add((name, str(ano), str(mes).zfill(2)))
        return sorted(achados)

    @staticmethod
    def question_wants_monthly_grouping(question: str) -> bool:
        """Delega para period_parser.wants_monthly_grouping (compatibilidade)."""
        return wants_monthly_grouping(question)


    @log_calls
    def prepare_question_cache(
        self,
        question: str,
        request_id: str,
        sql: str | None = None,
        extra_columns: set[str] | None = None,
    ) -> dict[str, Any]:
        """Prepara o cache da pergunta criando a view unificada ``datasus_query_cache``.

        Resolve o recorte temporal (pelos indices nativos dos grupos ou, no modo
        legado, pelos Parquet), monta o ``UNION ALL`` de ``<grupo>.datasus_cache``
        com filtro por ``atend_ano``/``atend_mes`` (push-down) e o publica como
        TEMP VIEW (lazy, sem materializar). Grupos sem campo de periodo mapeado
        sao EXCLUIDOS do UNION.

        Args:
            question: pergunta do usuario.
            request_id: identificador da requisicao (telemetria).
            sql: SQL final, usada para a projecao minima de colunas (opcional).
            extra_columns: colunas adicionais que devem ser projetadas (opcional).

        Returns:
            dict[str, Any]: ``{"files", "names", "periods"}`` do recorte aplicado.

        Raises:
            RuntimeError: quando nenhum periodo/grupo corresponde a pergunta.
        """
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
        periods: set[tuple[str, str]] = set()
        if self.groups:
            selected_groups = self.periods_for_question(question)
            if not selected_groups:
                raise RuntimeError("Nenhum período do cache corresponde ao informado na pergunta.")
            periods = {(year, month) for _, year, month in selected_groups}
        
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
            # comportamento anterior para o caso (raro) de grupo recem-criado.
            # ATTACH sob demanda: garante os schemas antes de (re)computar o
            # schema canonico S2. attach() e idempotente.
            for group in self.groups.values():
                try:
                    group.attach()
                except Exception as attach_exc:
                    LOGGER.warning(
                        "grupo %s: falha ao anexar banco: %s", group.name, attach_exc
                    )
            all_columns, group_columns = self.canonical_schema()
            # >>> GUARD: S2 vazio mas ha grupos <<<
            # Se o cache S2 nao conhece nenhum grupo, mas ``self.groups`` nao
            # esta vazio (ex.: grupos anexados/ingeridos apos a ultima
            # invalidacao), descartamos e recomputamos UMA vez antes de desistir.
            if not group_columns and self.groups:
                self.invalidate_schema()
                all_columns, group_columns = self.canonical_schema()
            # Fonte unica de verdade dos grupos efetivamente disponiveis
            # (TABELA ou VIEW), a partir do cache S2.
            _s2_safe_to_name = (self._canonical_schema_cache or {}).get("safe_to_name", {})
            s2_groups = [
                (safe, _s2_safe_to_name.get(safe, safe))
                for safe in (self._canonical_schema_cache or {}).get("safe_names", [])
                if safe in group_columns
            ]

            union_sql, n_grupos = unified_view.build_union_sql(
                s2_groups=s2_groups,
                group_columns=group_columns,
                all_columns=all_columns,
                period_years=period_years,
                period_months=period_months,
                sql=sql,
                extra_columns=extra_columns,
                request_id=request_id,
            )
            if union_sql:
                self.connection.execute("DROP VIEW IF EXISTS datasus_query_cache")
                self.connection.execute("DROP TABLE IF EXISTS datasus_query_cache")
                self.connection.execute(
                    "CREATE TEMP VIEW datasus_query_cache AS " + union_sql
                )
                LOGGER.info(
                    "question cache como VIEW (lazy) request_id=%s grupos=%d duration_ms=%.2f",
                    request_id, n_grupos, (time.perf_counter() - started) * 1000,
                )
                return {"files": 0, "names": [], "periods": periods}

        raise RuntimeError(
            "Nenhum periodo/grupo do cache corresponde ao informado na pergunta."
        )

    def summary_tables(self) -> list[str]:
        """Lista as tabelas agregadas resumo_* disponiveis.

        Returns:
            list[str]: nomes das tabelas resumo_* saudaveis.
        """
        return summary_mod.summary_tables(self.connection)
    _summary_tables = summary_tables


    def schema_context(self, relation: str = "datasus", include_examples: bool = True) -> dict[str, Any]:
        """Descreve as colunas de uma relacao (nome + tipo) para o prompt do LLM.

        Args:
            relation: relacao consultada (ex.: ``datasus``, ``resumo_cid_periodo``).
            include_examples: reservado para exemplos de valores por coluna.

        Returns:
            dict[str, Any]: ``{"columns": [{"column", "type"}, ...]}`` (vazio com
            log em caso de erro).
        """
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
        """Executa a SQL final (com reparos seguros) e devolve as linhas.

        Aplica limites (``LIMIT``) e correcoes estaticas de SQL antes de executar,
        registra telemetria e devolve a lista de linhas como dicionarios.

        Args:
            sql: SQL gerada/validada a executar.
            request_id: identificador da requisicao (logs/telemetria).
            limite: teto de linhas pedido na pergunta (opcional).

        Returns:
            list[dict[str, Any]]: linhas do resultado (vazio em caso de erro).
        """
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
                fixed_sql = fix_unterminated_strings(sql)
                
                # >>> NOVO: Tenta corrigir SQL truncado/incompleto (parenteses nao fechados, etc.) <<<
                if fixed_sql == sql:
                    fixed_sql = fix_incomplete_sql(sql)
                
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




    def is_ready(self) -> bool:
        """Informa se o cache terminou a inicializacao SEM erro.

        Returns:
            bool: True quando ``ensure_ready_sync`` marcou pronto e nao houve
            erro de startup (senao ``/api/ask`` responde 503).
        """
        return self._ready.is_set() and self._startup_error is None

    def startup_status(self) -> str | None:
        """Devolve a mensagem de erro da inicializacao do cache, se houver.

        Returns:
            str | None: mensagem de erro (``None`` quando tudo correu bem).
        """
        return self._startup_error

    def mark_ready(self, error: str | None = None) -> None:
        """Marca o cache como pronto (liberando ``/api/ask``) ou registra o erro.

        Args:
            error: mensagem de erro de startup; ``None`` indica sucesso.
        """
        self._startup_error = error
        self._ready.set()

    def status(self) -> dict[str, Any]:
        """Resume o estado do cache: arquivos, linhas, grupos e resumos.

        Returns:
            dict[str, Any]: ``{"files", "rows", "database", "groups",
            "summary_tables"}`` - usado pelo endpoint de status.
        """
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
            registros = group_registry.registrados(self.connection)
            group_status: dict[str, Any] = {}
            total_files = 0
            total_rows = 0
            for name, group in sorted(self.groups.items()):
                registro = registros.get(name, {})
                arquivos = int(registro.get("files") or 0)
                linhas = int(registro.get("rows_est") or 0)
                total_files += arquivos
                total_rows += linhas
                group_status[name] = {
                    "files": arquivos,
                    "rows": linhas,
                    "database": str(group.db_path),
                    "cache_table": registro.get("status") == "ok",
                }
            LOGGER.info(
                "status complete files=%d rows_est=%d duration_ms=%.2f",
                total_files, total_rows, (time.perf_counter() - started) * 1000,
            )
            return {
                "files": total_files,
                "rows": total_rows,
                "database": str(self.database_path),
                "groups": group_status,
                "summary_tables": self.summary_tables(),
            }
        return {
            "files": 0,
            "rows": None,
            "database": str(self.database_path),
            "groups": {},
            "summary_tables": self.summary_tables(),
        }

    def ingest(self, build_summary: bool = False) -> dict[str, Any]:
        """Ingere os arquivos no cache DuckDB.

        Delega a ingestao a cada grupo (pending_batch/import_batch/finalize),
        abrindo e fechando a conexao de cada um para manter a memoria sob
        controle. O progresso fica no group_checkpoint de cada grupo.
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
                    self.group(name)
                    try:
                        # Rede de seguranca contra nao-convergencia: cada lote
                        # consome no maximo PER_ITERATION arquivos, portanto um
                        # grupo com N arquivos nao deveria exigir mais do que
                        # ~N/PER_ITERATION iteracoes. O limite evita loop
                        # infinito caso pending_batch continue devolvendo o
                        # mesmo lote (ex.: arquivos invalidos).
                        total_group_files = len(group.discover_files())
                        max_iters = (total_group_files // PER_ITERATION) + 3
                        iterations = 0
                        while True:
                            iterations += 1
                            if iterations > max_iters:
                                LOGGER.error(
                                    "grupo %s: ingestão não convergiu após %d lotes "
                                    "(%d arquivos); interrompendo para evitar loop.",
                                    name, iterations, total_group_files,
                                )
                                break
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
                            LOGGER.info("Grupo %s: lote de %d arquivos importado", name, len(batch))
                        # So valida integridade de periodo se o grupo recebeu
                        # escrita nesta rodada (evita varredura completa em
                        # grupos grandes sem novidades).
                        group.finalize(verify_period=stats[name]["files"] > 0)
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
                result = {
                    "files": 0,
                    "pending": 0,
                    "already_cached": 0,
                    "cache_hit": True,
                    "groups": {},
                }

            # Estrutura dos grupos pode ter mudado: descarta o schema canonico
            # memoizado para que a proxima pergunta recompute a uniao de colunas.
            self.invalidate_schema()

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
                relacoes = [
                    '"' + n.replace('"', '""') + '".datasus_cache'
                    for n in self.groups
                ] or ["datasus_cache"]
                result["cid_prefixes"] = cid_prefixes_mod.populate_prefixes(
                    self.connection, relacoes
                )
            except Exception as e:
                LOGGER.warning("falha ao pre-computar prefixos de CID: %s", e)
        return result

    def build_summary_tables(self) -> dict[str, Any]:
        """Constroi as tabelas agregadas resumo_* a partir das tabelas de grupo.

        Returns:
            dict[str, Any]: {"files", "tables", "duration_ms"}.
        """
        relacoes = [
            '"' + nome.replace('"', '""') + '".datasus_cache'
            for nome in self.groups
        ]
        with self._build_lock:
            return summary_mod.build_summary_tables(self.connection, relacoes)
