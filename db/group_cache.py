from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import duckdb

from config.settings import PROJECT_DIR
from core.io_utils import _discover_files_in, _retry_copyfile, _retry_stat
from core.logging_config import log_calls
from core.path_utils import _grouped_cache_path, _schema_rows, _extract_period_from_path
from db import group_registry
from db.column_comments import apply_comments as _apply_column_comments_to
from db.period_fields import (
    PERIOD_FIELDS,
    ATEND_ANO,
    ATEND_MES,
    ano_expr,
    ano_int_expr,
    mes_expr,
)

LOGGER = logging.getLogger("datasus_llm")

# Limite (linhas) para migrar atend_ano/atend_mes IN PLACE. A migracao adiciona
# as colunas e reescreve a tabela inteira no backfill; acima deste limite isso e
# proibitivo dentro do boot/finalize. Nesses casos o grupo precisa ser reconstruido
# com `dev_tools/rebuild_group_cache.py <grupo>` (que ja cria as colunas).
# Configuravel por `DATASUS_MIGRATE_MAX_ROWS`.
MIGRATE_MAX_ROWS = int(os.getenv("DATASUS_MIGRATE_MAX_ROWS", "5000000"))


def _sql_literal(value: Any) -> str:
    """Converte um valor Python em literal SQL seguro (string escapada/numero).

    Usado para evitar `execute(sql, [params])` por linha nos caminhos de escrita
    da ingestao: o binding de parametros mantem objetos Python vivos durante a
    execucao nativa, e o destrutor do resultado pode rodar fora do GIL em certas
    versoes do duckdb-python (ver duckdb-python #456/#462).

    Args:
        value: valor Python (str, int, float, bool ou None).

    Returns:
        str: literal SQL correspondente.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"

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


class _GroupCache:
    """Cache DuckDB dedicado a uma sub-pasta dos dados (ex.: sp, rj, am, ...).

    Cada sub-pasta dentro de DATASUS_DATA_DIR recebe o seu proprio arquivo
    '<subpasta>.duckdb' em DATASUS_CACHE_DIR, permitindo leituras rapidas e

    IMPORTANTE: a conexao DuckDB NO e aberta no construtor. Ela e aberta
    sob demanda pelo metodo `reopen()`, permitindo que apenas um arquivo
    .duckdb fique aberto em memoria por vez durante a ingestao. Isso
    reduz drasticamente o uso de RAM.
    """

    def __init__(self, name: str, source_dir: Path, cache_dir: Path, main_connection: Any = None) -> None:
        """Registra o cache de UM grupo (sem abrir conexao DuckDB aqui).

        A conexao e criada sob demanda por :meth:`reopen` - assim so um
        `<grupo>.duckdb` fica aberto por vez durante a ingestao.

        Args:
            name: nome do grupo (ex.: ``"er"``, ``"sp"``).
            source_dir: pasta de origem dos Parquet **deste** grupo.
            cache_dir: diretorio de cache onde vive ``<name>.duckdb``.
            main_connection: conexao principal (quando o grupo e ANEXADO como
                schema nela); ``None`` forca banco proprio.
        """
        self.name = name
        self.source_dir = source_dir
        self.cache_dir = cache_dir
        self.db_path = cache_dir / f"{name}.duckdb"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.main_connection = main_connection
        # A conexao sera aberta sob demanda quando o grupo for processado
        self.connection = None
        self._using_main_connection = False
        LOGGER.info("grupo cache registrado name=%s db_path=%s (abertura sob demanda)", name, self.db_path)

    def close(self) -> None:
        """Fecha a conexao DuckDB deste grupo, liberando memoria.

        Deve ser chamado apos o processamento do grupo para que o proximo
        grupo possa usar a memoria liberada.
        """
        try:
            if self.connection is not None and not self._using_main_connection:
                self.connection.close()
                LOGGER.info("grupo %s: conexÃ£o DuckDB fechada, memÃ³ria liberada", self.name)
            elif self._using_main_connection:
                LOGGER.info("grupo %s: usando conexÃ£o principal; nÃ£o serÃ¡ fechada aqui", self.name)
        except Exception:
            LOGGER.exception("grupo %s: erro ao fechar conexÃ£o DuckDB", self.name)
        finally:
            self.connection = None
            self._using_main_connection = False

    def reopen(self) -> None:
        """Reabre a conexao DuckDB do grupo caso tenha sido fechada.

        Sempre usa a conexao principal com o schema do grupo, pois os bancos
        .duckdb dos grupos sao anexados como schemas na conexao principal.
        """
        if self.connection is not None:
            return
        try:
            if self.main_connection is not None:
                # Marca o flag antes de chamar attach(): o restante de reopen()
                # usa _using_main_connection para qualificar as tabelas.
                # attach() depende apenas de main_connection, portanto a ordem
                # entre o flag e a chamada nao altera o resultado.
                self.connection = self.main_connection
                self._using_main_connection = True
                self.attach()
                LOGGER.info("grupo %s: usando conexÃ£o principal (schema %s)", self.name, self.name)
            else:
                self.connection = duckdb.connect(str(self.db_path))
            self._configure_memory()
            # Garante as tabelas basicas e migracoes necessarias
            manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
            metadata_table = f"metadata" if not self._using_main_connection else f"{self.name}.metadata"
            period_table = f"period_index" if not self._using_main_connection else f"{self.name}.period_index"
            ckpt_table = f"group_checkpoint" if not self._using_main_connection else f"{self.name}.group_checkpoint"
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {manifest_table} "
                "(source VARCHAR PRIMARY KEY, size BIGINT, modified DOUBLE, cached VARCHAR, "
                "in_db INTEGER DEFAULT 0, invalid INTEGER DEFAULT 0)"
            )
            # Migracao: adiciona a coluna 'invalid' caso a tabela ja exista sem ela.
            manifest_cols = {row[0] for row in self.connection.execute(f"DESCRIBE {manifest_table}").fetchall()}
            if "invalid" not in manifest_cols:
                LOGGER.info("grupo %s: migrando file_manifest para incluir coluna 'invalid'", self.name)
                self.connection.execute(f"ALTER TABLE {manifest_table} ADD COLUMN invalid INTEGER DEFAULT 0")
            # >>> FASE 3 (#3): MIGRACAO IDEMPOTENTE das colunas de periodo FISICAS <<<
            # Materializa atend_ano/atend_mes na tabela datasus_cache (quando ela
            # for TABELA fisica), derivadas do CAMPO NATIVO de periodo do grupo
            # (SP_DTINTER, DT_INTER, DT_ATEND, PA_CMP, AP_DTINIC a" ver
            # db/period_fields.py). Isso permite que o UNION de prepare_question_cache
            # use as colunas fisicas (push-down) em vez de SUBSTR(CAST(...)) a cada
            # consulta. Idempotente: so adiciona colunas que faltam; nunca apaga dados.
            self._migrate_period_columns()
            self.connection.execute(f"CREATE TABLE IF NOT EXISTS {metadata_table} (key VARCHAR PRIMARY KEY, value BIGINT)")
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {period_table} "
                "(path VARCHAR PRIMARY KEY, parquet_year VARCHAR, parquet_month VARCHAR)"
            )
            # Tabela de checkpoint para retomar ingestao interrompida
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {ckpt_table} ("
                "key VARCHAR PRIMARY KEY, value VARCHAR)"
            )
            # Metadados: aplica descricao/exemplos do layout CSV como COMENTARIO
            # das colunas da TABELA datasus_cache (so para campos que coincidem
            # EXATAMENTE). Idempotente e best-effort; nunca toca na VIEW datasus.
            self._apply_column_comments()
        except Exception:
            # >>> CORREAAO: NAO descarta o grupo para ':memory:' quando hA conexAo
            # principal. O fallback em memoria isola o grupo, fazendo as consultas
            # qualificadas (schema.<tabela>) continuarem falhando e, pior, ocultando
            # a falha real de ATTACH. So cai para memoria no modo sem conexao principal.
            if self.main_connection is not None:
                LOGGER.error(
                    "grupo %s: falha ao preparar schema do grupo em %s; propagando erro "
                    "(conexÃ£o principal presente)",
                    self.name, self.db_path, exc_info=True,
                )
                raise
            LOGGER.error("falha ao reabrir DuckDB do grupo %s em %s; usando banco em memÃ³ria",
                         self.name, self.db_path, exc_info=True)
            self.db_path = None
            self._using_main_connection = False
            self.connection = duckdb.connect(":memory:")
            self._configure_memory()
        LOGGER.info("grupo %s: conexÃ£o DuckDB reaberta com tabelas garantidas", self.name)
    ensure = reopen

    def attach(self) -> None:
        """Garante que o banco .duckdb do grupo esteja anexado como schema.

        Cria o arquivo do banco caso ainda nao exista e executa o ATTACH
        (READ_WRITE, com fallback READ_ONLY). Idempotente: se o schema ja
        estiver anexado, nao faz nada. Nunca apaga arquivos .duckdb.

        Raises:
            duckdb.IOException: quando o arquivo esta bloqueado por outro
                processo (conflicting lock). O erro e propagado apos o log.
        """
        # Depende apenas de haver conexao principal, nao do flag
        # _using_main_connection: o ATTACH e garantido em qualquer ordem
        # relativa a marcacao do flag (ver reopen()), evitando
        # "Catalog Error: Schema with name <grupo> does not exist!".
        if self.main_connection is None:
            return
        safe_name = self.name.replace('"', '""')
        try:
            exists = self.main_connection.execute(
                "SELECT COUNT(*) FROM duckdb_databases() WHERE database_name = ?",
                [self.name],
            ).fetchone()[0]
            if exists:
                return

            # Nao abre conexao temporaria so para materializar o arquivo:
            # isso adquire lock momentaneo no <grupo>.duckdb e pode colidir
            # com a conexao principal. O ATTACH abaixo cria o arquivo.
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.main_connection.execute(
                    f"ATTACH IF NOT EXISTS '{self.db_path}' AS {safe_name} (READ_WRITE)"
                )
            except Exception:
                self.main_connection.execute(
                    f"ATTACH IF NOT EXISTS '{self.db_path}' AS {safe_name} (READ_ONLY)"
                )
            LOGGER.info(
                "grupo %s: banco %s anexado como schema %s sob demanda",
                self.name, self.db_path, safe_name,
            )
        except duckdb.IOException as exc:
            # Lock de escrita: diagnostico explicito, sem retry infinito.
            if "Could not set lock" in str(exc):
                LOGGER.error(
                    "grupo %s: arquivo %s esta BLOQUEADO por OUTRO processo "
                    "(conflicting lock). Verifique com `lsof %s` / `fuser -v %s` se ha "
                    "execucao anterior ativa (app, notebook ou rebuild_group_cache.py) "
                    "e encerre-a antes de reexecutar.",
                    self.name, self.db_path, self.db_path, self.db_path,
                )
            else:
                LOGGER.exception(
                    "grupo %s: falha ao garantir ATTACH do banco %s", self.name, self.db_path
                )
            raise
        except Exception:
            LOGGER.exception(
                "grupo %s: falha ao garantir ATTACH do banco %s", self.name, self.db_path
            )
            raise

    def _period_col_projection(self, col: str) -> str | None:
        """FASE 3 (#3.3): expressao de projecao de uma coluna de periodo fisica.

        Quando ``col`` for ``atend_ano`` ou ``atend_mes`` e o grupo tiver campo
        nativo de periodo, retorna a expressao SQL que deriva o valor do CAMPO
        NATIVO (ex.: ``SUBSTR(CAST("DT_INTER" AS VARCHAR), 1, 4) AS atend_ano``).
        Caso contrario retorna ``None`` (o chamador usa o caminho padrao).

        Isso garante que os LOTES INCREMENTAIS gravem atend_ano/atend_mes
        preenchidos (nunca CAST(NULL)) a" o mesmo bug que ocorria com
        parquet_year/parquet_month em lotes incrementais.
        """
        pf = PERIOD_FIELDS.get(self.name)
        if pf is None:
            return None
        pcol = pf.columns[0]
        if col == ATEND_ANO:
            # FASE 3B: atend_ano e INTEGER (caminho canonico). TRY_CAST garante
            # NULL em vez de erro para valores ausentes/nao numericos.
            return f"{ano_int_expr(pcol)} AS {ATEND_ANO}"
        if col == ATEND_MES:
            return f"{mes_expr(pcol)} AS {ATEND_MES}"
        return None

    def _period_projection_exprs(self) -> str:
        """FASE 3 (#3.2): expressoes SQL de atend_ano/atend_mes para o SELECT inicial.

        Retorna um fragmento SQL (precedido de virgula) que deriva as colunas
        FISICAS atend_ano/atend_mes do CAMPO NATIVO de periodo do grupo. Retorna
        string vazia quando o grupo nao tem campo mapeado (a tabela sera criada
        sem essas colunas, e a migracao/backfill nao se aplica).

        Usado na criacao inicial de datasus_cache (``CREATE OR REPLACE TABLE``).
        """
        pf = PERIOD_FIELDS.get(self.name)
        if pf is None:
            return ""
        pcol = pf.columns[0]
        # FASE 3B: atend_ano e INTEGER (caminho canonico); atend_mes continua
        # VARCHAR ('01'..'12'). Mantem paridade com a view S1 e os resumos.
        ano_sql = ano_int_expr(pcol)
        mes_sql = mes_expr(pcol)
        return f", {ano_sql} AS {ATEND_ANO}, {mes_sql} AS {ATEND_MES}"

    def _estimated_rows(self, cache_table: str) -> int:
        """Retorna a cardinalidade ESTIMADA de ``datasus_cache`` (sem ``COUNT(*)``).

        Usa ``duckdb_tables().estimated_size`` para decidir, em O(1), se uma
        migracao in-place e viavel. Nunca levanta: em caso de erro devolve 0.

        Args:
            cache_table: relacao qualificada ou nao (apenas para doc/telemetria).

        Returns:
            int: numero estimado de linhas (0 quando desconhecido).
        """
        try:
            row = self.connection.execute(
                "SELECT estimated_size FROM duckdb_tables() "
                "WHERE database_name = ? AND table_name = 'datasus_cache'",
                [self.name],
            ).fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            LOGGER.debug("grupo %s: estimated_size indisponivel para %s", self.name, cache_table)
            return 0

    def _migrate_period_columns(self) -> None:
        """FASE 3 (#3): adiciona atend_ano/atend_mes FISICOS a datasus_cache (TABELA).

        Materializa as colunas derivadas de periodo na tabela fisica do grupo,
        usando o CAMPO NATIVO de periodo (ver db/period_fields.py). Idempotente:
        so adiciona colunas que ainda faltam. Nao faz nada quando:

          * o grupo nao tem campo nativo de periodo mapeado; ou
          * datasus_cache e uma VIEW (modo S1); ou
          * datasus_cache (TABELA) ainda nao existe (sera criada com as colunas
            pelo primeiro lote em import_batch/ingest); ou
          * as duas colunas ja existem no tipo canonico (populacao garantida na
            criacao/rebuild); ou
          * a tabela tem mais de ``MIGRATE_MAX_ROWS`` linhas e falta migrar
            (o backfill reescreveria a tabela inteira dentro do boot).

        Depois de adicionar a coluna, dispara um BACKFILL idempotente (3.4) para
        preencher as linhas ja existentes a partir do campo nativo.
        """
        pf = PERIOD_FIELDS.get(self.name)
        if pf is None:
            return
        try:
            # So se aplica a TABELA fisica (nao a VIEW do modo S1).
            if not self.exists(kind="table"):
                return
            cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"
            # FASE 3B: guardamos o TIPO de cada coluna para detectar e
            # converter uma coluna atend_ano legada em VARCHAR -> INTEGER.
            desc = {
                row[0].lower(): str(row[1]).upper()
                for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()
            }
            cols = set(desc)
            precisa_adicionar = ATEND_ANO not in cols or ATEND_MES not in cols
            # FASE 3B: atend_ano canonico e INTEGER. Se existir como VARCHAR
            # (cache da PARTE 3), ainda precisa ser convertido.
            precisa_converter = (
                ATEND_ANO in cols and "INT" not in desc.get(ATEND_ANO, "")
            )
            if not precisa_adicionar and not precisa_converter:
                # Ja materializadas no tipo canonico (import_batch/rebuild).
                return
            # Reescrita IN PLACE e O(n): em tabelas grandes levaria horas dentro
            # do boot/finalize. Acima do limite, exige rebuild do grupo.
            estimado = self._estimated_rows(cache_table)
            if estimado > MIGRATE_MAX_ROWS:
                LOGGER.warning(
                    "grupo %s: cache com ~%d linhas pendente de migracao de "
                    "periodo (%s/%s); migracao in-place desativada "
                    "(DATASUS_MIGRATE_MAX_ROWS=%d). Reconstrua com "
                    "`python dev_tools/rebuild_group_cache.py %s`.",
                    self.name, estimado, ATEND_ANO, ATEND_MES,
                    MIGRATE_MAX_ROWS, self.name,
                )
                return
            added = False
            # FASE 3B: atend_ano e INTEGER (tipo canonico).
            if ATEND_ANO not in cols:
                LOGGER.info("grupo %s: migrando %s -> +%s INTEGER", self.name, cache_table, ATEND_ANO)
                self._exec(f"ALTER TABLE {cache_table} ADD COLUMN {ATEND_ANO} INTEGER")
                added = True
            elif "INT" not in desc.get(ATEND_ANO, ""):
                # Coluna existente com tipo INCOMPATIVEL (ex.: VARCHAR migrado na
                # PARTE 3). Converte para INTEGER sem apagar dados (TRY_CAST ->
                # NULL em valores nao numericos). Idempotente: so quando o tipo
                # nao for INTEGER.
                LOGGER.info(
                    "grupo %s: migrando %s.%s tipo %s -> INTEGER",
                    self.name, cache_table, ATEND_ANO, desc.get(ATEND_ANO),
                )
                self._exec(
                    f"ALTER TABLE {cache_table} ALTER COLUMN {ATEND_ANO} "
                    f"TYPE INTEGER USING TRY_CAST({ATEND_ANO} AS INTEGER)"
                )
            if ATEND_MES not in cols:
                LOGGER.info("grupo %s: migrando %s -> +%s VARCHAR", self.name, cache_table, ATEND_MES)
                self._exec(f"ALTER TABLE {cache_table} ADD COLUMN {ATEND_MES} VARCHAR")
                added = True
            if added:
                # Preenche as linhas ja existentes a partir do campo nativo.
                self._backfill_period_columns()
        except Exception:
            LOGGER.exception(
                "grupo %s: falha ao migrar colunas de periodo fisicas (%s/%s)",
                self.name, ATEND_ANO, ATEND_MES,
            )

    def _backfill_period_columns(self) -> None:
        """FASE 3 (#3.4): preenche atend_ano/atend_mes nulos a partir do campo nativo.

        Idempotente: so toca linhas com atend_ano NULL. Usa o CAMPO NATIVO de
        periodo do grupo (SP_DTINTER, DT_INTER, DT_ATEND, PA_CMP, AP_DTINIC).
        Nao faz nada em modo VIEW (S1) nem quando o grupo nao tem campo mapeado.
        """
        pf = PERIOD_FIELDS.get(self.name)
        if pf is None:
            return
        try:
            if not self.exists(kind="table"):
                return
            cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"
            cols = {
                row[0].lower()
                for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()
            }
            if ATEND_ANO not in cols or ATEND_MES not in cols:
                return
            pcol = pf.columns[0]
            if pcol.lower() not in cols:
                # Campo nativo ausente neste grupo (ex.: cache antigo): nada a fazer.
                LOGGER.warning(
                    "grupo %s: campo nativo de periodo %s ausente em %s; "
                    "backfill de %s/%s ignorado",
                    self.name, pcol, cache_table, ATEND_ANO, ATEND_MES,
                )
                return
            ano_sql = ano_int_expr(pcol)
            mes_sql = mes_expr(pcol)
            self.connection.execute(
                f"UPDATE {cache_table} SET "
                f"{ATEND_ANO} = {ano_sql}, "
                f"{ATEND_MES} = {mes_sql} "
                f"WHERE {ATEND_ANO} IS NULL AND \"{pcol}\" IS NOT NULL"
            )
        except Exception:
            LOGGER.exception(
                "grupo %s: falha no backfill de %s/%s", self.name, ATEND_ANO, ATEND_MES,
            )

    def _configure_memory(self) -> None:
        """Aplica os PRAGMAs de memoria/threads/spill/checkpoint do grupo (best-effort).

        Usa os MESMOS defaults de ``config/settings.py`` (16 threads, 20 GB) e mantem
        o spill num DISCO SEPARADO do banco (``DATASUS_TEMP_DIR``, padrao
        ``C:\\datasus_spill``). Tambem desativa ``preserve_insertion_order`` (evita
        bufferizar/ordenar lotes gigantes -> menos spill) e limita o WAL com
        ``checkpoint_threshold='4GB'`` - PLANO_PERFORMANCE_CACHE.md, Etapas 1-4.

        Nao faz nada (com log) quando o grupo esta em modo
        "conexao principal", pois os PRAGMAs valem para a conexao inteira.
        """
        # Configuracao centralizada em config/settings.py (sugestao 5)
        # Usa os MESMOS defaults de Settings para manter consistencia entre o
        # cache principal (DatasusCache) e os bancos dos grupos (_GroupCache).
        # PLANO_PERFORMANCE_CACHE.md, Etapa 1: 16 threads / 20 GB (~63% da RAM).
        memory_limit = os.getenv("DATASUS_MEMORY_LIMIT", "20GB")
        threads = int(os.getenv("DATASUS_THREADS", "16"))
        # Spill dir resiliente: se DATASUS_TEMP_DIR for definido, usa-o;
        # senao fallback para o cache do grupo (ou ./cache em caso de USB removivel).
        temp_dir_raw = os.getenv("DATASUS_TEMP_DIR", r"C:\datasus_spill")
        try:
            temp_dir = Path(temp_dir_raw) if temp_dir_raw else self.cache_dir / "duckdb_spill"
            temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            (PROJECT_DIR / "cache").mkdir(parents=True, exist_ok=True)
            temp_dir = PROJECT_DIR / "cache" / "duckdb_spill"
            temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Se estiver usando a conexao principal, nao aplica PRAGMAs de memoria
            # (isso seria aplicado a conexao principal e pode causar conflitos).
            if self._using_main_connection:
                LOGGER.info("grupo %s: usando conexÃ£o principal; PRAGMAs de memÃ³ria jÃ¡ configurados", self.name)
                return
            self.connection.execute(f"PRAGMA temp_directory='{temp_dir}'")
            self.connection.execute(f"PRAGMA memory_limit='{memory_limit}'")
            self.connection.execute(f"PRAGMA threads={threads}")
            # Etapa 3: nao bufferizar/ordenar o lote inteiro -> muito menos spill.
            self.connection.execute("PRAGMA preserve_insertion_order=false")
            # Etapa 4: limita o WAL (evita o checkpoint gigante single-thread no close).
            self.connection.execute("PRAGMA checkpoint_threshold='4GB'")
        except Exception:
            LOGGER.exception("grupo %s: nÃ£o foi possÃ­vel aplicar limites de memÃ³ria", self.name)

    def _scalar(self, sql: str, params: list[Any] | None = None, default: Any = None) -> Any:
        """Executa ``sql`` e devolve o primeiro valor da primeira linha.

        Qualifica automaticamente as tabelas internas (``file_manifest``,
        ``datasus_cache``, ...) quando o grupo usa a conexao principal. Nunca
        levanta: erros sao logados e retorna ``default``.

        Args:
            sql: consulta escalar.
            params: parametros posicionais (opcional).
            default: valor devolvido em caso de erro/linha vazia.

        Returns:
            Any: primeiro valor da primeira linha (ou ``default``).
        """
        if self.connection is None:
            LOGGER.warning("grupo %s: conexÃ£o nÃ£o aberta ao chamar _scalar; abrindo", self.name)
            self.reopen()
            if self.connection is None:
                LOGGER.error("grupo %s: falha ao abrir conexÃ£o", self.name)
                return default            
        # Qualifica tabelas nao-qualificadas quando usando a conexao principal
        if self._using_main_connection:
            sql = self._qualify_sql(sql)
        LOGGER.debug("grupo %s: executando consulta: %s", self.name, sql)
        try:
            row = self.connection.execute(sql, params or []).fetchone()
            return row[0] if row is not None else default
        except Exception as e:
            LOGGER.error("grupo %s: erro ao executar consulta: %s", self.name, e, exc_info=True)
            return default
    scalar = _scalar

    def _qualify_sql(self, sql: str) -> str:
        """>>> PARTE 5 (#1): qualificacao ANCORADA em FROM/JOIN <<<

        A versao anterior reescrevia QUALQUER token datasus_cache/datasus
        (etc.) no SQL INTEIRO via regex com lookbehind/lookahead de aspas.
        Isso era ARRISCADO: tokens cercados por outros caracteres (ex.: dentro
        de um literal LIKE '%datasus%') NAO estavam adjacentes a aspas e eram
        reescritos, CORROMPENDO o literal ('%datasus%' -> '%pa.datasus%').

        Agora so qualificamos a RELACAO que aparece imediatamente APOS uma
        clausula FROM/JOIN. Um matcher reconhece FROM/JOIN + a relacao (com
        delimitador opcional) e:
          * SO qualifica se a relacao for uma das tabelas internas do grupo;
          * NUNCA toca em relacoes JA qualificadas (algo.tabela);
          * NUNCA toca em literais/comentarios (nao casam o padrao FROM/JOIN).
        """
        if not sql:
            return sql
        tabelas = (
            "file_manifest", "metadata", "period_index",
            "group_checkpoint", "datasus_cache", "datasus",
        )
        tabelas_cf = {nome.casefold() for nome in tabelas}
        schema = self.name

        # FROM/JOIN seguido da relacao, com delimitador opcional.
        rel_re = re.compile(
            r'\b(FROM|JOIN)\s+'
            r'(["`\[]?)([A-Za-z_][A-Za-z0-9_]*)(["`\]]?)'
            r'(?=\s|$|[,;)])',
            flags=re.IGNORECASE,
        )

        def _sub(match: re.Match) -> str:
            """Qualifica ``nome`` com ``schema`` quando for tabela interna."""
            keyword = match.group(1)
            pre_q = match.group(2)
            nome = match.group(3)
            if nome.casefold() not in tabelas_cf:
                return match.group(0)
            if pre_q:
                return f"{keyword} {pre_q}{schema}.{nome}{pre_q}"
            return f"{keyword} {schema}.{nome}"

        return rel_re.sub(_sub, sql)

    def exists(self, relation: str = "datasus_cache", kind: str = "any") -> bool:
        """Informa se uma relacao existe no banco do grupo.

        Args:
            relation: nome da relacao (ex.: ``"datasus_cache"``, ``"datasus"``).
            kind: ``"table"``, ``"view"`` ou ``"any"`` (qualquer uma).

        Returns:
            bool: True quando a relacao existe no banco do grupo.

        Raises:
            ValueError: quando ``kind`` nao e um dos valores aceitos.
        """
        if kind not in {"table", "view", "any"}:
            raise ValueError(f"kind invalido: {kind!r}")
        if self.connection is None:
            self.reopen()
        catalogos = []
        if kind in {"table", "any"}:
            catalogos.append(("duckdb_tables", "table_name"))
        if kind in {"view", "any"}:
            catalogos.append(("duckdb_views", "view_name"))
        for catalogo, coluna in catalogos:
            try:
                count = self.connection.execute(
                    f"SELECT COUNT(*) FROM {catalogo}() "
                    f"WHERE LOWER(database_name) = LOWER(?) AND {coluna} = ?",
                    [self.name, relation],
                ).fetchone()[0]
                if count:
                    return True
            except Exception:
                continue
        # Conexao isolada: o catalogo principal nao tem database_name do grupo.
        if self.main_connection is None or not self._using_main_connection:
            try:
                row = self.connection.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_name = ?",
                    [relation],
                ).fetchone()
                return bool(row and row[0])
            except Exception:
                return False
        return False

    def discover_files(self) -> list[Path]:
        """Lista os Parquet do grupo na pasta de origem (recursivo).

        Returns:
            list[Path]: arquivos ``*.parquet`` encontrados em ``source_dir``.
        """
        return _discover_files_in(self.source_dir)

    def _refresh_period_index(self, files: list[Path]) -> None:
        """Reconstroi o indice ``period_index`` (arquivo -> ano, mes) do grupo.

        O periodo e extraido do NOME do arquivo (padrao ``AAMM`` do DATASUS).
        A tabela e limpa e repovoada a cada chamada (idempotente).

        Args:
            files: Parquet cacheados do grupo.
        """
        records = []
        for path in files:
            match = re.search(r"(\d{2})(0[1-9]|1[0-2])[a-z]*\.parquet$", path.name.casefold())
            if match:
                records.append((str(path), "20" + match.group(1), match.group(2)))
        period_table = f"period_index" if not self._using_main_connection else f"{self.name}.period_index"
        self._exec(f"DELETE FROM {period_table}")
        if records:
            # Uma unica instrucao (VALUES) em vez de executemany: menos resultados
            # temporarios e nenhum binding de parametros por linha.
            valores = ", ".join(
                "(" + ", ".join(_sql_literal(v) for v in row) + ")"
                for row in records
            )
            self._exec(f"INSERT INTO {period_table} VALUES {valores}")
    period_index = _refresh_period_index

    def _exec(self, sql: str, params: list[Any] | None = None) -> None:
        """Executa ``sql`` e CONSOME o resultado imediatamente.

        DDL/DML no DuckDB deixam um resultado vazio/contagem "pendente" na
        conexao. Consumir com ``fetchall`` encerra a execucao no ponto em que o
        GIL esta garantido, em vez de deixar o destrutor do resultado rodar
        depois (GC/thread nativa) - ver duckdb-python #456/#462. Tambem mantem o
        uso de memoria previsivel durante a ingestao.

        Args:
            sql: comando SQL a executar.
            params: parametros posicionais (opcional).
        """
        self.connection.execute(sql, params or []).fetchall()

    def _save_checkpoint(self, batch_index: int, total: int) -> None:
        """Salva checkpoint de progresso da ingestao do grupo (1 comando)."""
        table = (
            "group_checkpoint" if not self._using_main_connection
            else f"{self.name}.group_checkpoint"
        )
        self._exec(
            f"INSERT OR REPLACE INTO {table} (key, value) VALUES "
            "('last_batch_index', ?), ('total_files', ?), ('last_updated_at', ?)",
            [str(batch_index), str(total), str(time.time())],
        )

    def _load_checkpoint(self) -> int:
        """Carrega o ultimo checkpoint salvo. Retorna o indice do ultimo lote processado."""
        table = f"group_checkpoint" if not self._using_main_connection else f"{self.name}.group_checkpoint"
        row = self.connection.execute(
            f"SELECT value FROM {table} WHERE key = 'last_batch_index'"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)

    def ingest(self) -> dict[str, Any]:
        """Ingestao COMPLETA do grupo, a partir dos Parquet da pasta de origem.

        Percorre o manifesto, copia para o cache agrupado (ano/mes) apenas os
        arquivos novos/alterados e materializa a TABELA datasus_cache. Verifica
        cada Parquet antes de registra-lo e marca os invalidos no manifesto
        (invalid=1) para nao reprocessa-los.

        Returns:
            dict[str, Any]: resumo com files (ingeridos), invalid,
            already_cached e cache_hit.
        """
        started = time.perf_counter()
        all_files = self.discover_files()
        if not all_files:
            return {"group": self.name, "files": 0, "status": "no_files_found"}

        manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
        cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"
        # Carrega o manifesto uma unica vez: source -> (size, modified, cached, in_db, invalid)
        manifest: dict[str, tuple[int, float, str, int, int]] = {
            source: (size, modified, cached, in_db, invalid)
            for source, size, modified, cached, in_db, invalid in self.connection.execute(
                f"SELECT source, size, modified, cached, in_db, invalid FROM {manifest_table}"
            ).fetchall()
        }

        # Apenas arquivos NO presentes no cache (novos ou alterados) sao processados.
        to_ingest: list[Path] = []
        already_cached = 0
        for source in all_files:
            rel_str = str(source.relative_to(self.source_dir))
            rec = manifest.get(rel_str)
            st = _retry_stat(source)
            if st is None:
                # Arquivo sumiu / USB desconectado no meio da varredura: salta agora
                # (pode voltar no proximo ingest quando o disco re-montar).
                LOGGER.warning("grupo %s: pulando %s (indisponÃ­vel/desconectado)", self.name, source)
                continue

            target = _grouped_cache_path(self.cache_dir, self.name, source)
            if rec is not None and rec[0] == st.st_size and rec[1] == st.st_mtime:
                if rec[3] == 1 and target.exists():
                    already_cached += 1
                    continue
            to_ingest.append(source)

        LOGGER.info("grupo %s: %d novos de %d totais (jÃ¡ em cache: %d)",
                    self.name, len(to_ingest), len(all_files), already_cached)

        if not to_ingest:
            if not self.exists():
                to_ingest = all_files
            else:
                if not self.exists("datasus"):
                    self._ensure_view_and_index(all_files)
                return {"group": self.name, "files": len(all_files), "changed": 0,
                        "already_cached": already_cached, "cache_hit": True}

        batch_size = 50
        changed = 0
        invalid = 0

        # Checkpoint: retoma do ultimo lote processado (reinicio nao reprocessa tudo)
        checkpoint_index = self._load_checkpoint()
        if checkpoint_index > 0:
            LOGGER.info("grupo %s: retomando de checkpoint no Ã­ndice %d (de %d)",
                        self.name, checkpoint_index, len(to_ingest))
            # Arquivos ja processados anteriormente podem ja estar no DuckDB.
            # O manifest detecta os ja inseridos; apenas continuamos do checkpoint.
            checkpoint_index = min(checkpoint_index, len(to_ingest))

        for i in range(checkpoint_index, len(to_ingest), batch_size):
            batch = to_ingest[i:i + batch_size]
            batch_cached: list[str] = []
            batch_sources: list[str] = []
            for source in batch:
                rel_str = str(source.relative_to(self.source_dir))
                target = _grouped_cache_path(self.cache_dir, self.name, source)
                st = _retry_stat(source)
                if st is None:
                    invalid += 1
                    LOGGER.error("grupo %s: arquivo indisponÃ­vel (USB?) %s", self.name, source)
                    continue
                try:
                    if not _retry_copyfile(source, target):
                        raise OSError("cÃ³pia incompleta do arquivo-fonte")
                    self.connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(target)])
                    self.connection.execute(
                        f"INSERT OR REPLACE INTO {manifest_table} (source, size, modified, cached, in_db) "
                        "VALUES (?, ?, ?, ?, 0)",
                        [rel_str, st.st_size, st.st_mtime, str(target)],
                    )
                    batch_cached.append(str(target))
                    batch_sources.append(rel_str)
                    changed += 1
                except Exception as error:
                    invalid += 1
                    LOGGER.error("grupo %s: falha ao ler %s: %s", self.name, source, error)
                    # Preserva o arquivo-fonte (dados originais) e remove apenas a copia invalida
                    # do cache, registrando o arquivo como invalido no manifesto para que nao seja
                    # reprocessado indefinidamente numa proxima ingestao.
                    target.unlink(missing_ok=True)
                    self.connection.execute(
                        f"INSERT OR REPLACE INTO {manifest_table} "
                        "(source, size, modified, cached, in_db, invalid) VALUES (?, ?, ?, '', 0, 1)",
                        [rel_str, st.st_size, st.st_mtime],
                    )

            if batch_cached:
                paths_sql = ", ".join(f"'{p.replace(chr(39), chr(39)*2)}'" for p in batch_cached)
                fonte = f"read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                lote = "(" + "SELECT *, filename AS parquet_path FROM " + fonte + ")"
                if not self.exists(kind="table"):
                    # Usa CREATE OR REPLACE para evitar conflito com nomes que existem
                    # no catalogo main (o DuckDB interpreta `CREATE TABLE ps.datasus_cache`
                    # de forma ambigua quando `datasus_cache` ja existe no main).
                    #
                    # >>> CORREAAO: perAodo derivado do CAMINHO COMPLETO do parquet
                    # (filename de read_parquet(filename=true)), ancorado no fim do nome
                    # (.parquet) e tolerando sufixo de letra (ex.: SPMG2205a.parquet);
                    # fallback para AAAA/MM das pastas do caminho. Garante
                    # parquet_year/parquet_month preenchidos ja na criacao.
                    self.connection.execute(
                        f"CREATE OR REPLACE TABLE {cache_table} AS SELECT *, filename AS parquet_path{self._period_projection_exprs()}, "
                        "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                        "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year, "
                        "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                        "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month "
                        f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                    )
                    # Rede de seguranca: recalcula o periodo de quem ainda ficou NULL
                    # a partir de parquet_path (idempotente).
                    self.connection.execute(
                        f"UPDATE {cache_table} SET "
                        "parquet_year = COALESCE(2000 + CAST(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                        "TRY_CAST(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)), "
                        "parquet_month = COALESCE(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                        "NULLIF(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) "
                        "WHERE parquet_year IS NULL AND parquet_path IS NOT NULL"
                    )
                else:
                    # >>> CORREAAO (ver import_batch): inclui 'filename = true' e o alias
                    # parquet_path no lote incremental; sem isso parquet_year/parquet_month
                    # ficavam NULL e o filtro de periodo descartava o lote inteiro.
                    existing_upper = {row[0].upper() for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()}
                    for row in self.connection.execute(f"DESCRIBE {lote}").fetchall():
                        col_name, col_type = row[0], row[1]
                        if col_name.upper() not in existing_upper:
                            LOGGER.info("grupo %s: nova coluna %s (%s)", self.name, col_name, col_type)
                            self.connection.execute(f'ALTER TABLE {cache_table} ADD COLUMN "{col_name}" {col_type}')
                    table_cols = [r[0] for r in _schema_rows(self.connection, cache_table)]
                    batch_by_upper = {r[0].upper(): r[0] for r in self.connection.execute(f"DESCRIBE {lote}").fetchall()}
                    projection = []
                    for col in table_cols:
                        esc = col.replace('"', '""')
                        # Os campos de metadados de periodo sao SEMPRE derivados
                        # explicitamente por coluna, para nunca cair no CAST(NULL).
                        if col == "parquet_path":
                            projection.append("filename AS parquet_path")
                        elif col == "parquet_year":
                            projection.append(
                                "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                                "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year"
                            )
                        elif col == "parquet_month":
                            projection.append(
                                "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                                "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month"
                            )
                        elif col.upper() in batch_by_upper:
                            projection.append('"' + batch_by_upper[col.upper()].replace('"', '""') + '"')
                        elif self._period_col_projection(col) is not None:
                            projection.append(self._period_col_projection(col))
                        else:
                            projection.append('CAST(NULL AS VARCHAR) AS "' + esc + '"')
                    col_list = ", ".join('"' + col.replace('"', '""') + '"' for col in table_cols)
                    # >>> GUARDA DE IDEMPOTENCIA (reparo 2026-10-12) <<<
                    # Se o MESMO parquet ja estava na tabela (ex.: a chave do
                    # manifesto mudou numa migracao Linux->Windows), remove a
                    # contribuicao anterior ANTES de inserir de novo. Sem isso o
                    # arquivo entra 2x e as agregacoes saem dobradas.
                    self.connection.execute(
                        f"DELETE FROM {cache_table} WHERE parquet_path IN "
                        f"(SELECT DISTINCT parquet_path FROM {lote})"
                    )
                    self.connection.execute(
                        f"INSERT INTO {cache_table} ({col_list}) SELECT {', '.join(projection)} FROM {lote}"
                    )
                    # >>> CORREAAO: recomputa o perAodo do lote a partir de parquet_path.
                    self.connection.execute(
                        f"UPDATE {cache_table} SET "
                        "parquet_year = COALESCE(2000 + CAST(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                        "TRY_CAST(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)), "
                        "parquet_month = COALESCE(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                        "NULLIF(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) "
                        "WHERE parquet_year IS NULL AND parquet_path IS NOT NULL"
                    )
                escaped = ", ".join("'" + s.replace("'", "''") + "'" for s in batch_sources)
                self.connection.execute(f"UPDATE {manifest_table} SET in_db = 1 WHERE source IN ({escaped})")

            # Salva checkpoint apos cada lote processado
            self._save_checkpoint(i + len(batch), len(to_ingest))

        self._ensure_view_and_index([_grouped_cache_path(self.cache_dir, self.name, f) for f in to_ingest])
        duration = (time.perf_counter() - started) * 1000
        LOGGER.info("grupo %s: ingestÃ£o completa: %d novos, %d invÃ¡lidos, %.2fms", self.name, changed, invalid, duration)
        return {"group": self.name, "files": changed, "invalid": invalid,
                "already_cached": already_cached, "cache_hit": False}

    @log_calls
    def pending_batch(self, limit: int = 50) -> tuple[list[Path], int, int]:
        """Identifica o proximo lote de arquivos NO cacheados deste grupo.

        Returns:
            (lote, total_pendentes, total_ja_cacheados)
        O lote contem no maximo `limit` arquivos, respeitando a segmentacao global.
        """
        all_files = self.discover_files()
        if not all_files:
            return [], 0, 0

        manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
        manifest: dict[str, tuple[int, float, str, int, int]] = {
            source: (size, modified, cached, in_db, invalid)
            for source, size, modified, cached, in_db, invalid in self.connection.execute(
                f"SELECT source, size, modified, cached, in_db, invalid FROM {manifest_table}"
            ).fetchall()
        }

        pending: list[Path] = []
        already_cached = 0
        for source in all_files:
            rel_str = str(source.relative_to(self.source_dir))
            rec = manifest.get(rel_str)
            st = _retry_stat(source)
            if st is None:
                # USB desconectado/suspenso durante a varredura: deixa para o
                # proximo ingest, nao derruba o processo.
                continue
            target = _grouped_cache_path(self.cache_dir, self.name, source)
            if rec is not None and rec[0] == st.st_size and rec[1] == st.st_mtime:
                if rec[3] == 1 and target.exists():
                    already_cached += 1
                    continue
                if rec[4] == 1:
                    # Arquivo marcado como invalido: preservado no fonte, nao reprocessado.
                    continue
            pending.append(source)

        # Sem relacao de cache ainda: reconstrucao a partir dos Parquets.
        # ATENCAO: NAO re-incluir arquivos ja marcados como invalidos no
        # manifesto (invalid=1). Caso contrario, quando TODOS os Parquets do
        # grupo sao invalidos o lote nunca produz `batch_cached`, a relacao de
        # cache nunca e criada, `already_cached` continua 0 e este guard
        # re-adiciona tudo indefinidamente -> loop infinito na inicializacao.
        if not pending and already_cached == 0 and not self.exists():
            pending = [
                f
                for f in all_files
                if (manifest.get(str(f.relative_to(self.source_dir))) or (0, 0, "", 0, 0))[4] != 1
            ]
            already_cached = 0

        batch = pending[:limit]
        LOGGER.info("grupo %s: lote de %d pendentes de %d totais (jÃ¡ em cache: %d)",
                    self.name, len(batch), len(all_files), already_cached)
        return batch, len(pending), already_cached

    @log_calls
    def import_batch(self, batch: list[Path]) -> dict[str, Any]:
        """Importa um lote (ate 50 arquivos) dos Parquet no DuckDB deste grupo."""
        changed = 0
        invalid = 0
        batch_cached: list[str] = []
        batch_sources: list[str] = []
        # Linhas do manifesto acumuladas no lote. Sao gravadas em UMA unica
        # instrucao ao final (antes: 1 INSERT parametrizado por arquivo).
        manifest_rows: list[tuple[Any, ...]] = []

        # Total de arquivos para calculo do percentual de progresso
        all_files = self.discover_files()
        total_files = len(all_files)
        # Arquivos ja processados anteriormente (checkpoint)
        processed_before = self._load_checkpoint()

        manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
        cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"
        group_ckpt_table = f"group_checkpoint" if not self._using_main_connection else f"{self.name}.group_checkpoint"

        for source in batch:
            rel_str = str(source.relative_to(self.source_dir))
            target = _grouped_cache_path(self.cache_dir, self.name, source)
            st = _retry_stat(source)
            if st is None:
                invalid += 1
                LOGGER.error("grupo %s: arquivo indisponÃ­vel (USB?) %s", self.name, source)
                continue
            try:
                if not _retry_copyfile(source, target):
                    raise OSError("cÃ³pia incompleta do arquivo-fonte")
                # Literal escapado (sem binding de parametros): menos objetos
                # Python vivos durante a execucao nativa do DuckDB.
                target_sql = str(target).replace("'", "''")
                self._exec(f"SELECT * FROM read_parquet('{target_sql}') LIMIT 0")
                manifest_rows.append((rel_str, st.st_size, st.st_mtime, str(target), 0, 0))
                batch_cached.append(str(target))
                batch_sources.append(rel_str)
                changed += 1

                # LOG DO ARQUIVO GRAVADO COM NOME, PARQUET, PARTIAAO E PERCENTUAL
                year_month = _extract_period_from_path(target)
                processed_total = processed_before + changed
                percent = (processed_total / total_files * 100) if total_files > 0 else 0
                LOGGER.info(
                    "CACHE GRAVADO | grupo=%s | arquivo=%s | parquet=%s | partiÃ§Ã£o=%s | progresso=%d/%d (%.1f%%)",
                    self.name,
                    source.name,
                    target.name,
                    year_month or "N/A",
                    processed_total,
                    total_files,
                    percent,
                )
            except Exception as error:
                invalid += 1
                LOGGER.error("grupo %s: falha ao ler %s: %s", self.name, source, error)
                # Preserva o arquivo-fonte (dados originais) e remove apenas a copia invalida
                # do cache, registrando o arquivo como invalido no manifesto para que nao seja
                # reprocessado indefinidamente numa proxima ingestao.
                target.unlink(missing_ok=True)
                manifest_rows.append((rel_str, st.st_size, st.st_mtime, "", 0, 1))

        # Manifesto: UMA escrita por lote (INSERT ... SELECT FROM VALUES).
        if manifest_rows:
            valores = ", ".join(
                "(" + ", ".join(_sql_literal(v) for v in row) + ")"
                for row in manifest_rows
            )
            self._exec(
                f"INSERT OR REPLACE INTO {manifest_table} "
                "(source, size, modified, cached, in_db, invalid) "
                f"SELECT * FROM (VALUES {valores}) AS v"
                "(source, size, modified, cached, in_db, invalid)"
            )

        if batch_cached:
            paths_sql = ", ".join(f"'{p.replace(chr(39), chr(39)*2)}'" for p in batch_cached)
            fonte = f"read_parquet([{paths_sql}], union_by_name = true, filename = true)"
            lote = "(" + "SELECT *, filename AS parquet_path FROM " + fonte + ")"
            if not self.exists(kind="table"):
                # Usa CREATE OR REPLACE para evitar conflito com nomes que existem
                # no catalogo main (o DuckDB interpreta `CREATE TABLE ps.datasus_cache`
                # de forma ambigua quando `datasus_cache` ja existe no main).
                #
                # >>> CORREAAO: o perAodo A derivado do CAMINHO COMPLETO do parquet
                # (filename retornado por read_parquet(filename=true)), ancorando o
                # regex no fim do nome (.parquet) e tolerando o sufixo de letra
                # (ex.: SPMG2205a.parquet). Como fallback, extrai AAAA/MM das pastas
                # do caminho (.../<grupo>/<ano>/<mes>/arquivo.parquet). Assim
                # parquet_year/parquet_month sao SEMPRE preenchidos na criacao.
                self._exec(
                    f"CREATE OR REPLACE TABLE {cache_table} AS SELECT *, filename AS parquet_path{self._period_projection_exprs()}, "
                    "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                    "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year, "
                    "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                    "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month "
                    f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                )
                # Rede de seguranca: qualquer linha que ainda fique com periodo NULL
                # e recalculada a partir de parquet_path (idempotente).
                self._exec(
                    f"UPDATE {cache_table} SET "
                    "parquet_year = COALESCE(2000 + CAST(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                    "TRY_CAST(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)), "
                    "parquet_month = COALESCE(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                    "NULLIF(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) "
                    "WHERE parquet_year IS NULL AND parquet_path IS NOT NULL"
                )
            else:
                # >>> CORREAAO (bug sarampo/B05 -> 0 linhas com dados no cache):
                # inclui 'filename = true' e o alias parquet_path no lote incremental.
                # Sem isso, o lote nao tinha 'filename'; como a tabela ja possui
                # as colunas 'parquet_path'/'parquet_year'/'parquet_month' (criadas no
                # 1o lote), o INSERT preenchia-as com CAST(NULL AS VARCHAR). Com
                # parquet_year NULL, o filtro de periodo de prepare_question_cache
                # (parquet_year IN (...)) descartava TODO o lote e a consulta retornava
                # 0 mesmo havendo milhares de registros do CID no cache.
                existing_upper = {row[0].upper() for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()}
                for row in self.connection.execute(f"DESCRIBE {lote}").fetchall():
                    col_name, col_type = row[0], row[1]
                    if col_name.upper() not in existing_upper:
                        LOGGER.info("grupo %s: nova coluna %s (%s)", self.name, col_name, col_type)
                        self._exec(f'ALTER TABLE {cache_table} ADD COLUMN "{col_name}" {col_type}')
                table_cols = [r[0] for r in _schema_rows(self.connection, cache_table)]
                batch_by_upper = {r[0].upper(): r[0] for r in self.connection.execute(f"DESCRIBE {lote}").fetchall()}
                projection = []
                for col in table_cols:
                    esc = col.replace('"', '""')
                    # Campos de metadados de periodo sao SEMPRE derivados
                    # explicitamente (nunca CAST(NULL)), garantindo que todo lote
                    # incremental grave parquet_path/parquet_year/parquet_month.
                    if col == "parquet_path":
                        projection.append("filename AS parquet_path")
                    elif col == "parquet_year":
                        projection.append(
                            "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                            "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year"
                        )
                    elif col == "parquet_month":
                        projection.append(
                            "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                            "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month"
                        )
                    elif col.upper() in batch_by_upper:
                        projection.append('"' + batch_by_upper[col.upper()].replace('"', '""') + '"')
                    elif self._period_col_projection(col) is not None:
                        projection.append(self._period_col_projection(col))
                    else:
                        projection.append('CAST(NULL AS VARCHAR) AS "' + esc + '"')
                col_list = ", ".join('"' + col.replace('"', '""') + '"' for col in table_cols)
                # Idempotencia (correcao do loop de ingestao): remove as linhas ja
                # gravadas para ESTES Parquets antes de reinserir. Sem isso,
                # reprocessar um lote (ex.: manifesto com mtime desatualizado)
                # DUPLICA as linhas de datasus_cache.
                self._exec(
                    f"DELETE FROM {cache_table} WHERE parquet_path IN ({paths_sql})"
                )
                # >>> GUARDA DE IDEMPOTENCIA (reparo 2026-10-12) <<<
                # Remove a contribuicao anterior do(s) mesmo(s) parquet(s) antes
                # de inserir o lote: torna a ingestao idempotente e impede a
                # duplicacao (2x) que ocorria quando a chave do manifesto mudava.
                self._exec(
                    f"DELETE FROM {cache_table} WHERE parquet_path IN "
                    f"(SELECT DISTINCT parquet_path FROM {lote})"
                )
                self._exec(
                    f"INSERT INTO {cache_table} ({col_list}) SELECT {', '.join(projection)} FROM {lote}"
                )
                # >>> CORREAAO: recomputa parquet_year/parquet_month a partir de
                # parquet_path para o lote recem-inserido. Antes esse UPDATE so existia
                # na criacao inicial da tabela, deixando os lotes incrementais com
                # parquet_year NULL.
                self._exec(
                    f"UPDATE {cache_table} SET "
                    "parquet_year = COALESCE(2000 + CAST(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                    "TRY_CAST(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)), "
                    "parquet_month = COALESCE(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                    "NULLIF(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) "
                    "WHERE parquet_year IS NULL AND parquet_path IS NOT NULL"
                )
            escaped = ", ".join("'" + s.replace("'", "''") + "'" for s in batch_sources)
            self._exec(f"UPDATE {manifest_table} SET in_db = 1 WHERE source IN ({escaped})")

        # Salva checkpoint para o percentual correto no proximo lote
        if changed > 0 or invalid > 0:
            processed_total = self._load_checkpoint() + changed
            self._save_checkpoint(processed_total, total_files)

        return {"group": self.name, "files": changed, "invalid": invalid}

    def _verify_period_integrity(self) -> None:
        """Valida que TODAS as linhas do cache do grupo tem parquet_year preenchido.

        Apos qualquer escrita, confere a contagem de linhas sem periodo. Como o
        filtro de periodo de prepare_question_cache (parquet_year IN (...)) DESCARTA
        linhas com parquet_year NULL, qualquer linha nessa condicao se tornaria
        invisivel nas consultas (o bug do sarampo/B05 retornando 0 com dados no
        cache). Aqui, uma eventual violacao e registrada em ERROR e corrigida via
        backfill a partir de parquet_path a" nunca deixando o cache em estado
        silenciosamente incorreto.
        """
        try:
            if not self.exists():
                return
            cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"

            # Caches legados podem NO ter as colunas de metadados (filename,
            # parquet_path, parquet_year, parquet_month). Nesse caso nao ha o que
            # backfillar: o grupo precisa ser reconstruido (ver
            # dev_tools/rebuild_group_cache.py). Detectamos isso explicitamente para
            # nao gerar 'Binder Error: Referenced column parquet_year not found' em
            # consultas de periodo.
            cols = {
                row[0]
                for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()
            }
            faltando = [
                c for c in ("filename", "parquet_path", "parquet_year", "parquet_month")
                if c not in cols
            ]
            if faltando:
                LOGGER.error(
                    "grupo %s: cache LEGADO sem as colunas de metadados %s; "
                    "consultas com filtro de perÃ­odo falharÃ£o para este grupo. "
                    "Reconstrua com dev_tools/rebuild_group_cache.py %s",
                    self.name, faltando, self.name,
                )
                return

            missing = self.connection.execute(
                f"SELECT COUNT(*) FROM {cache_table} WHERE parquet_year IS NULL"
            ).fetchone()[0]
            if missing == 0:
                return
            LOGGER.error(
                "grupo %s: INTEGRIDADE DE PERÃODO: %d linhas com parquet_year NULL; "
                "aplicando backfill a partir de parquet_path", self.name, missing,
            )
            self.connection.execute(
                f"UPDATE {cache_table} SET "
                "parquet_year = COALESCE(2000 + CAST(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                "TRY_CAST(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)), "
                "parquet_month = COALESCE(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                "NULLIF(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) "
                "WHERE parquet_year IS NULL AND parquet_path IS NOT NULL"
            )
            missing_after = self.connection.execute(
                f"SELECT COUNT(*) FROM {cache_table} WHERE parquet_year IS NULL"
            ).fetchone()[0]
            if missing_after > 0:
                LOGGER.error(
                    "grupo %s: ainda restam %d linhas com parquet_year NULL apÃ³s backfill "
                    "(sem parquet_path utilizÃ¡vel) â€” requer reconstruÃ§Ã£o do grupo.",
                    self.name, missing_after,
                )
            else:
                LOGGER.info("grupo %s: integridade de perÃ­odo restaurada via backfill", self.name)
        except Exception:
            LOGGER.exception("grupo %s: falha ao verificar integridade de perÃ­odo", self.name)

    @log_calls
    def finalize(self, verify_period: bool = True) -> None:
        """Garante a view 'datasus', o indice de periodo e publica no catalogo.

        Args:
            verify_period: quando True, valida que nao restam linhas sem
                ``parquet_year`` (varredura completa da tabela). Use False quando
                o grupo NAO recebeu escrita nesta rodada - nao ha como ter surgido
                linha nova com periodo nulo.
        """
        cached = self.cached_files()
        if self.exists() and not self.exists("datasus"):
            if self._using_main_connection:
                self._exec(
                    f"CREATE VIEW {self.name}.datasus AS SELECT * FROM {self.name}.datasus_cache"
                )
            else:
                self._exec("CREATE VIEW datasus AS SELECT * FROM datasus_cache")
        # Garante que nenhuma linha ficou sem periodo (evita resultado vazio).
        if verify_period:
            self._verify_period_integrity()
        # Reaplica os comentarios de coluna na TABELA datasus_cache.
        self._apply_column_comments()
        if cached:
            self._refresh_period_index(cached)
        self._publicar_no_catalogo()

    def _publicar_no_catalogo(self) -> None:
        """Publica files/rows/periodos do grupo no catalogo (best-effort).

        Roda apenas com conexao principal (o catalogo vive nela) e nunca
        levanta excecao. Usa atend_ano/atend_mes fisicos quando existem; caso
        contrario deriva os periodos do campo nativo do grupo (PERIOD_FIELDS).
        """
        if self.main_connection is None:
            return
        try:
            # `files` e o numero de Parquet na ORIGEM - a mesma metrica usada por
            # `groups_needing_ingest`. NAO usar COUNT(*) do manifesto: entradas
            # antigas (de outro source_dir) inflam o valor e fariam o grupo
            # parecer "completo" quando um arquivo novo chega.
            files = len(_discover_files_in(self.source_dir))
            rows = int(self._scalar(
                "SELECT estimated_size FROM duckdb_tables() "
                "WHERE database_name = ? AND table_name = 'datasus_cache'",
                [self.name], default=0) or 0)
            tabela = (
                "datasus_cache" if not self._using_main_connection
                else f"{self.name}.datasus_cache"
            )
            try:
                colunas = {
                    str(row[0]).lower()
                    for row in self.connection.execute(
                        f"DESCRIBE {tabela}"
                    ).fetchall()
                }
                if "atend_ano" in colunas and "atend_mes" in colunas:
                    ano_sql, mes_sql = '"atend_ano"', '"atend_mes"'
                else:
                    periodo = PERIOD_FIELDS.get(self.name)
                    if periodo is None:
                        raise RuntimeError(
                            f"grupo {self.name} sem campo nativo de periodo"
                        )
                    ano_sql = ano_int_expr(periodo.columns[0])
                    mes_sql = mes_expr(periodo.columns[0])
                periodos = [
                    (int(ano), str(mes))
                    for ano, mes in self.connection.execute(
                        f"SELECT DISTINCT {ano_sql}, {mes_sql} FROM {tabela} "
                        f"WHERE {ano_sql} IS NOT NULL"
                    ).fetchall()
                ]
            except Exception:
                periodos = []
            group_registry.atualizar_apos_ingestao(
                self.main_connection, self.name, files, rows, periodos
            )
        except Exception:
            LOGGER.warning(
                "grupo %s: falha ao publicar no catalogo", self.name, exc_info=True
            )

    def _apply_column_comments(self) -> int:
        """Aplica descricao/exemplos do layout CSV como comentario nas colunas.

        Le ``<cache_dir>/layout_csv/<grupo>.csv`` e grava a descricao (e os
        exemplos, quando existirem) como COMENTARIO das colunas da TABELA
        ``datasus_cache``. So comenta quando o campo do CSV coincide EXATAMENTE
        com uma coluna existente. A VIEW ``datasus`` NO e comentada.

        Idempotente e best-effort: nunca levanta excecao (falhas sao logadas).
        Nao faz nada em modo VIEW (S1), pois nao ha tabela fisica para comentar.
        """
        try:
            if not self.exists(kind="table"):
                return 0
            return _apply_column_comments_to(
                self.connection,
                self.cache_dir,
                self.name,
                table="datasus_cache",
                qualified=self._using_main_connection,
            )
        except Exception:
            LOGGER.exception("grupo %s: falha ao aplicar comentÃ¡rios de colunas", self.name)
            return 0

    def cached_files(self) -> list[Path]:
        """Lista os Parquet JA CACHEADOS do grupo (``<cache_dir>/<grupo>/**.parquet``).

        Returns:
            list[Path]: arquivos do cache agrupado por ano/mes.
        """
        return _discover_files_in(self.cache_dir / self.name)

    def _ensure_view_and_index(self, files: list[Path]) -> None:
        """Garante a view ``datasus`` e o indice de periodo apos escrita incremental.

        Cria a view de compatibilidade quando a relacao de cache ja existe e ela
        ainda nao foi criada, e reindexa o periodo quando ha arquivos novos (ou
        quando o indice esta ausente).

        Args:
            files: Parquet recem-processados (usados para o indice).
        """
        if self.exists() and not self.exists("datasus"):
            if self._using_main_connection:
                self.connection.execute(
                    f"CREATE VIEW {self.name}.datasus AS SELECT * FROM {self.name}.datasus_cache"
                )
            else:
                self.connection.execute("CREATE VIEW datasus AS SELECT * FROM datasus_cache")
        if self._using_main_connection:
            indexed = self.connection.execute(
                f"SELECT COUNT(*) FROM {self.name}.period_index"
            ).fetchone()[0]
        else:
            indexed = self._scalar("SELECT COUNT(*) FROM period_index", default=0)
        if files or indexed is None:
            self._refresh_period_index(files)
