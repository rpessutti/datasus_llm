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
from core.path_utils import _grouped_cache_path, _schema_rows, _file_period, _extract_period_from_path

LOGGER = logging.getLogger("datasus_llm")


class _GroupCache:
    """Cache DuckDB dedicado a uma sub-pasta dos dados (ex.: sp, rj, am, ...).

    Cada sub-pasta dentro de DATASUS_DATA_DIR recebe o seu próprio arquivo
    '<subpasta>.duckdb' em DATASUS_CACHE_DIR, permitindo leituras rápidas e

    IMPORTANTE: a conexão DuckDB NÃO é aberta no construtor. Ela é aberta
    sob demanda pelo método `reopen()`, permitindo que apenas um arquivo
    .duckdb fique aberto em memória por vez durante a ingestão. Isso
    reduz drasticamente o uso de RAM.
    """

    def __init__(self, name: str, source_dir: Path, cache_dir: Path, main_connection: Any = None) -> None:
        self.name = name
        self.source_dir = source_dir
        self.cache_dir = cache_dir
        self.db_path = cache_dir / f"{name}.duckdb"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.main_connection = main_connection
        # A conexão será aberta sob demanda quando o grupo for processado
        self.connection = None
        self._using_main_connection = False
        LOGGER.info("grupo cache registrado name=%s db_path=%s (abertura sob demanda)", name, self.db_path)

    def close(self) -> None:
        """Fecha a conexão DuckDB deste grupo, liberando memória.

        Deve ser chamado após o processamento do grupo para que o próximo
        grupo possa usar a memória liberada.
        """
        try:
            if self.connection is not None and not self._using_main_connection:
                self.connection.close()
                LOGGER.info("grupo %s: conexão DuckDB fechada, memória liberada", self.name)
            elif self._using_main_connection:
                LOGGER.info("grupo %s: usando conexão principal; não será fechada aqui", self.name)
        except Exception:
            LOGGER.exception("grupo %s: erro ao fechar conexão DuckDB", self.name)
        finally:
            self.connection = None
            self._using_main_connection = False

    def reopen(self) -> None:
        """Reabre a conexão DuckDB do grupo caso tenha sido fechada anteriormente.

        SEMPRE usa a conexão principal com o schema do grupo, pois os bancos
        .duckdb dos grupos são anexados como schemas na conexão principal.
        """
        if self.connection is not None:
            return
        try:
            if self.main_connection is not None:
                # >>> CORREÇÃO: ANTES de usar a conexão principal, GARANTE que o banco
                # do grupo esteja ANEXADO como schema (ex.: 'pa'). Sem isso, quando o
                # <grupo>.duckdb ainda não existia em disco, o ATTACH feito no
                # DatasusCache.__init__ era pulado e toda query qualificada
                # (ex.: SELECT ... FROM pa.file_manifest) falhava com
                # "Catalog Error: Table with name file_manifest does not exist!".
                #
                # >>> CORREÇÃO (Catalog Error: Schema with name sp does not exist!):
                # O flag DEVE ser marcado como True ANTES de chamar _ensure_attached(),
                # pois _ensure_attached() tem a guarda
                #   `if not self._using_main_connection ...: return`
                # Na ordem anterior (ensure_attached -> set flag) o método retornava
                # cedo, o ATTACH do <grupo>.duckdb nunca acontecia, e a linha seguinte
                # (CREATE TABLE IF NOT EXISTS sp.file_manifest) falhava com
                # "Catalog Error: Schema with name sp does not exist!".
                self.connection = self.main_connection
                self._using_main_connection = True
                self._ensure_attached()
                LOGGER.info("grupo %s: usando conexão principal (schema %s)", self.name, self.name)
            else:
                self.connection = duckdb.connect(str(self.db_path))
            self._configure_memory()
            # Garante as tabelas básicas e migrações necessárias
            manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
            metadata_table = f"metadata" if not self._using_main_connection else f"{self.name}.metadata"
            period_table = f"period_index" if not self._using_main_connection else f"{self.name}.period_index"
            ckpt_table = f"group_checkpoint" if not self._using_main_connection else f"{self.name}.group_checkpoint"
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {manifest_table} "
                "(source VARCHAR PRIMARY KEY, size BIGINT, modified DOUBLE, cached VARCHAR, "
                "in_db INTEGER DEFAULT 0, invalid INTEGER DEFAULT 0)"
            )
            # Migração: adiciona a coluna 'invalid' caso a tabela já exista sem ela.
            manifest_cols = {row[0] for row in self.connection.execute(f"DESCRIBE {manifest_table}").fetchall()}
            if "invalid" not in manifest_cols:
                LOGGER.info("grupo %s: migrando file_manifest para incluir coluna 'invalid'", self.name)
                self.connection.execute(f"ALTER TABLE {manifest_table} ADD COLUMN invalid INTEGER DEFAULT 0")
            self.connection.execute(f"CREATE TABLE IF NOT EXISTS {metadata_table} (key VARCHAR PRIMARY KEY, value BIGINT)")
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {period_table} "
                "(path VARCHAR PRIMARY KEY, parquet_year VARCHAR, parquet_month VARCHAR)"
            )
            # Tabela de checkpoint para retomar ingestão interrompida
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {ckpt_table} ("
                "key VARCHAR PRIMARY KEY, value VARCHAR)"
            )
        except Exception:
            # >>> CORREÇÃO: NÃO descarta o grupo para ':memory:' quando há conexão
            # principal. O fallback em memória isola o grupo, fazendo as consultas
            # qualificadas (schema.<tabela>) continuarem falhando e, pior, ocultando
            # a falha real de ATTACH. Só cai para memória no modo sem conexão principal.
            if self.main_connection is not None:
                LOGGER.error(
                    "grupo %s: falha ao preparar schema do grupo em %s; propagando erro "
                    "(conexão principal presente)",
                    self.name, self.db_path, exc_info=True,
                )
                raise
            LOGGER.error("falha ao reabrir DuckDB do grupo %s em %s; usando banco em memória",
                         self.name, self.db_path, exc_info=True)
            self.db_path = None
            self._using_main_connection = False
            self.connection = duckdb.connect(":memory:")
            self._configure_memory()
        LOGGER.info("grupo %s: conexão DuckDB reaberta com tabelas garantidas", self.name)

    def _ensure_attached(self) -> None:
        """Garante que o banco .duckdb do grupo esteja ANEXADO como schema na conexão principal.

        Cria o arquivo do banco caso ele ainda não exista e realiza o ATTACH
        (READ_WRITE, com fallback para READ_ONLY). Operação idempotente: se o
        schema já estiver anexado, não faz nada.

        IMPORTANTE: este método NUNCA apaga arquivos .duckdb — apenas cria e
        anexa. É seguro chamá-lo repetidamente.
        """
        # >>> CORREÇÃO: depende apenas de haver conexão principal, não do flag
        # _using_main_connection. Assim o ATTACH é garantido mesmo quando este
        # método é chamado em qualquer ordem relativa à marcação do flag (ver
        # reopen()), evitando o erro
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

            # Garante que o diretório exista. NÃO abrimos uma conexão temporária
            # só para materializar o arquivo: isso adquire um lock momentâneo
            # sobre <grupo>.duckdb que pode colidir com a conexão principal
            # (IOException: Could not set lock on file ...). O ATTACH abaixo cria
            # o arquivo se ele ainda não existir.
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
            # Fix 4: erro de lock é diagnosticado explicitamente.
            if "Could not set lock" in str(exc):
                LOGGER.error(
                    "grupo %s: arquivo %s está BLOQUEADO por OUTRO processo "
                    "(conflicting lock). Verifique com `lsof %s` / `fuser -v %s` se há "
                    "execução anterior ativa (app, notebook ou rebuild_group_cache.py) "
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

    def _configure_memory(self) -> None:
        # Configuração centralizada em config/settings.py (sugestão 5)
        # Usa os MESMOS defaults de Settings para manter consistência entre o
        # cache principal (DatasusCache) e os bancos dos grupos (_GroupCache).
        memory_limit = os.getenv("DATASUS_MEMORY_LIMIT", "1GB")
        threads = int(os.getenv("DATASUS_THREADS", "4"))
        # Spill dir resiliente: se DATASUS_TEMP_DIR for definido, usa-o;
        # senão fallback para o cache do grupo (ou ./cache em caso de USB removível).
        temp_dir_raw = os.getenv("DATASUS_TEMP_DIR", "")
        if temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            temp_dir.mkdir(parents=True, exist_ok=True)
        else:
            temp_dir = self.cache_dir / "duckdb_spill"
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                (PROJECT_DIR / "cache").mkdir(parents=True, exist_ok=True)
                temp_dir = PROJECT_DIR / "cache" / "duckdb_spill"
                temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Se estiver usando a conexão principal, não aplica PRAGMAs de memória
            # (isso seria aplicado à conexão principal e pode causar conflitos).
            if self._using_main_connection:
                LOGGER.info("grupo %s: usando conexão principal; PRAGMAs de memória já configurados", self.name)
                return
            self.connection.execute(f"PRAGMA temp_directory='{temp_dir}'")
            self.connection.execute(f"PRAGMA memory_limit='{memory_limit}'")
            self.connection.execute(f"PRAGMA threads={threads}")
        except Exception:
            LOGGER.exception("grupo %s: não foi possível aplicar limites de memória", self.name)

    def _scalar(self, sql: str, params: list[Any] | None = None, default: Any = None) -> Any:
        if self.connection is None:
            LOGGER.warning("grupo %s: conexão não aberta ao chamar _scalar; abrindo", self.name)
            self.reopen()
            if self.connection is None:
                LOGGER.error("grupo %s: falha ao abrir conexão", self.name)
                return default            
        # Qualifica tabelas não-qualificadas quando usando a conexão principal
        if self._using_main_connection:
            sql = self._qualify_sql(sql)
        LOGGER.debug("grupo %s: executando consulta: %s", self.name, sql)
        try:
            row = self.connection.execute(sql, params or []).fetchone()
            return row[0] if row is not None else default
        except Exception as e:
            LOGGER.error("grupo %s: erro ao executar consulta: %s", self.name, e, exc_info=True)
            return default

    def _qualify_sql(self, sql: str) -> str:
        """Qualifica tabelas comuns com o schema do grupo quando usando a conexão principal."""
        for table in ("file_manifest", "metadata", "period_index", "group_checkpoint", "datasus_cache", "datasus"):
            sql = re.sub(
                rf"\b(?<!['\"`])({table})\b(?!['\"`])",
                f"{self.name}.{table}",
                sql,
            )
        return sql

    def _cache_table_exists(self) -> bool:
        if self.connection is None and self.main_connection is not None:
            try:
                count = self.main_connection.execute(
                    "SELECT COUNT(*) FROM duckdb_tables() "
                    "WHERE database_name = ? AND table_name = 'datasus_cache'",
                    [self.name],
                ).fetchone()[0]
                return bool(count)
            except Exception:
                pass  # cai no fallback abaixo

        if self.connection is None:
            LOGGER.warning("grupo %s: conexão não aberta ao chamar _cache_table_exists; abrindo", self.name)
            self.reopen()
        if self._using_main_connection:
            return bool(self.connection.execute(
                "SELECT COUNT(*) FROM duckdb_tables() "
                "WHERE database_name = ? AND table_name = 'datasus_cache'",
                [self.name]
            ).fetchone()[0])
        return bool(self._scalar(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'datasus_cache'", default=0,
        ))

    def _cache_view_exists(self) -> bool:
        """True quando a relação datasus_cache existe como VIEW (modo S1)."""
        if self.connection is None and self.main_connection is not None:
            try:
                count = self.main_connection.execute(
                    "SELECT COUNT(*) FROM duckdb_views() "
                    "WHERE database_name = ? AND view_name = 'datasus_cache'",
                    [self.name],
                ).fetchone()[0]
                return bool(count)
            except Exception:
                pass
        if self.connection is None:
            LOGGER.warning("grupo %s: conexão não aberta ao chamar _cache_view_exists; abrindo", self.name)
            self.reopen()
        if self._using_main_connection:
            return bool(self.connection.execute(
                "SELECT COUNT(*) FROM duckdb_views() "
                "WHERE database_name = ? AND view_name = 'datasus_cache'",
                [self.name]
            ).fetchone()[0])
        return bool(self._scalar(
            "SELECT COUNT(*) FROM information_schema.views "
            "WHERE table_schema = 'main' AND table_name = 'datasus_cache'", default=0,
        ))

    def _cache_relation_exists(self) -> bool:
        """True quando datasus_cache existe como TABELA (legado) OU VIEW (S1).

        No modo S1 (DATASUS_CACHE_AS_VIEW=true) o cache do grupo é uma VIEW sobre
        os Parquet, portanto _cache_table_exists() retorna False. Este helper cobre
        os dois modos e evita que o pipeline reingira tudo a cada inicialização.
        """
        return self._cache_table_exists() or self._cache_view_exists()

    def _datasus_view_exists(self) -> bool:
        if self.connection is None:
            LOGGER.warning("grupo %s: conexão não aberta ao chamar _datasus_view_exists; abrindo", self.name)
            self.reopen()
        if self._using_main_connection:
            return bool(self.connection.execute(
                "SELECT COUNT(*) FROM duckdb_views() "
                "WHERE database_name = ? AND view_name = 'datasus'",
                [self.name]
            ).fetchone()[0])
        return bool(self._scalar(
            "SELECT COUNT(*) FROM information_schema.views "
            "WHERE table_schema = 'main' AND table_name = 'datasus'", default=0,
        ))

    def discover_files(self) -> list[Path]:
        return _discover_files_in(self.source_dir)

    def _grouped_cache_path(self, source: Path) -> Path:
        """Retorna o caminho de cache agrupado por ano/mês para um arquivo-fonte deste grupo."""
        return _grouped_cache_path(self.cache_dir, self.name, source)

    def _extract_period_from_path(self, path: Path) -> str | None:
        """Extrai o período (ano/mês) do caminho do cache agrupado.

        Estrutura: {cache_dir}/{grupo}/{ano}/{mes}/{arquivo}.parquet
        """
        return _extract_period_from_path(path)

    def _refresh_period_index(self, files: list[Path]) -> None:
        records = []
        for path in files:
            match = re.search(r"(\d{2})(0[1-9]|1[0-2])[a-z]*\.parquet$", path.name.casefold())
            if match:
                records.append((str(path), "20" + match.group(1), match.group(2)))
        period_table = f"period_index" if not self._using_main_connection else f"{self.name}.period_index"
        self.connection.execute(f"DELETE FROM {period_table}")
        if records:
            self.connection.executemany(f"INSERT INTO {period_table} VALUES (?, ?, ?)", records)

    def _save_checkpoint(self, batch_index: int, total: int) -> None:
        """Salva checkpoint de progresso da ingestão do grupo."""
        sql = (
            f"INSERT OR REPLACE INTO {'group_checkpoint' if not self._using_main_connection else self.name + '.group_checkpoint'} "
            "(key, value) VALUES ('last_batch_index', ?)"
        )
        self.connection.execute(sql, [str(batch_index)])
        sql = (
            f"INSERT OR REPLACE INTO {'group_checkpoint' if not self._using_main_connection else self.name + '.group_checkpoint'} "
            "(key, value) VALUES ('total_files', ?)"
        )
        self.connection.execute(sql, [str(total)])
        sql = (
            f"INSERT OR REPLACE INTO {'group_checkpoint' if not self._using_main_connection else self.name + '.group_checkpoint'} "
            "(key, value) VALUES ('last_updated_at', ?)"
        )
        self.connection.execute(sql, [str(time.time())])

    def _load_checkpoint(self) -> int:
        """Carrega o último checkpoint salvo. Retorna o índice do último lote processado."""
        table = f"group_checkpoint" if not self._using_main_connection else f"{self.name}.group_checkpoint"
        row = self.connection.execute(
            f"SELECT value FROM {table} WHERE key = 'last_batch_index'"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)

    @staticmethod
    @log_calls
    def _cache_as_view_enabled() -> bool:
        """FASE 2 (S1): quando True, o cache do grupo vira uma VIEW sobre os Parquet.

        Isso ELIMINA a duplicacao de dados (a tabela fisica datasus_cache
        guardava uma copia dos Parquet). A view le diretamente os Parquet do
        grupo via read_parquet([...], union_by_name=true, filename=true) e
        deriva parquet_path/parquet_year/parquet_month.

        Reversivel via DATASUS_CACHE_AS_VIEW=false.
        """
        return os.getenv("DATASUS_CACHE_AS_VIEW", "false").casefold() in {"1", "true", "yes", "on"}

    def _all_cached_parquets(self) -> list[Path]:
        """Todos os Parquet ja cacheados do grupo (usados pela VIEW da S1)."""
        try:
            return sorted(self._cached_group_files())
        except Exception:
            return []

    _VIEW_PERIOD_EXPR = (
        "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
        "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year, "
        "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
        "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month"
    )

    def _materialize_as_view(self) -> bool:
        """FASE 2 (S1): (re)cria datasus_cache como VIEW sobre os Parquet do grupo.

        Retorna True se a view foi criada. Substitui a tabela fisica,
        eliminando a copia duplicada. Mantem EXATAMENTE as colunas derivadas
        (parquet_path, parquet_year, parquet_month) que a tabela tinha, para
        que o restante do pipeline (prepare_question_cache) continue igual.
        """
        parquets = self._all_cached_parquets()
        if not parquets:
            LOGGER.warning("grupo %s: S1 sem Parquet para materializar view", self.name)
            return False
        paths_sql = ", ".join(f"'{str(p).replace(chr(39), chr(39) * 2)}'" for p in parquets)
        try:
            # Remove relacao anterior (tabela fisica OU view) antes de recriar.
            # O DROP TABLE IF EXISTS falha com CatalogException quando o objeto ja
            # e uma VIEW (idempotencia da S1), por isso tentamos ambos.
            for drop in (
                f"DROP VIEW IF EXISTS {self.name}.datasus_cache",
                f"DROP TABLE IF EXISTS {self.name}.datasus_cache",
            ):
                try:
                    self.connection.execute(drop)
                except duckdb.CatalogException:
                    pass
            self.connection.execute(
                f"CREATE VIEW {self.name}.datasus_cache AS "
                f"SELECT *, filename AS parquet_path, {self._VIEW_PERIOD_EXPR} "
                f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
            )
            LOGGER.info(
                "grupo %s: S1 datasus_cache criado como VIEW sobre %d Parquet",
                self.name, len(parquets),
            )
            return True
        except Exception as e:
            LOGGER.error("grupo %s: falha ao materializar VIEW (S1): %s", self.name, e, exc_info=True)
            return False

    def _ingest_as_view(self, all_files: list[Path]) -> dict[str, Any]:
        """FASE 2 (S1): copia os Parquet que faltam e (re)cria a VIEW.

        Nao ha INSERT de linhas no DuckDB: garantimos que cada Parquet esta no
        cache local (copiando os que faltam/alterados) e recriamos a VIEW lendo
        TODOS os Parquet do grupo. Idempotente e barato em execucoes seguintes
        (a copia e pulada quando tamanho/mtime batem).
        """
        manifest_table = "file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
        try:
            manifest: dict[str, tuple[int, float]] = {
                row[0]: (row[1], row[2])
                for row in self.connection.execute(
                    f"SELECT source, size, modified FROM {manifest_table}"
                ).fetchall()
            }
        except Exception:
            manifest = {}

        copied = 0
        skipped = 0
        for source in all_files:
            rel_str = str(source.relative_to(self.source_dir))
            target = self._grouped_cache_path(source)
            st = _retry_stat(source)
            if st is None:
                LOGGER.warning("grupo %s: S1 pulando %s (indisponivel)", self.name, source)
                continue
            rec = manifest.get(rel_str)
            if rec and rec[0] == st.st_size and rec[1] == st.st_mtime and target.exists():
                skipped += 1
                continue
            try:
                if not _retry_copyfile(source, target):
                    raise OSError("copia incompleta do arquivo-fonte")
                copied += 1
                self.connection.execute(
                    f"INSERT OR REPLACE INTO {manifest_table} (source, size, modified, cached, in_db) "
                    "VALUES (?, ?, ?, ?, 1)",
                    [rel_str, st.st_size, st.st_mtime, str(target)],
                )
            except Exception as e:
                LOGGER.error("grupo %s: S1 falha ao copiar %s: %s", self.name, source, e)

        ok = self._materialize_as_view()
        # Garante a view 'datasus' (compatibilidade) e o indice de periodo.
        if ok:
            if self._using_main_connection:
                self.connection.execute(
                    f"CREATE OR REPLACE VIEW {self.name}.datasus AS SELECT * FROM {self.name}.datasus_cache"
                )
            else:
                self.connection.execute("CREATE OR REPLACE VIEW datasus AS SELECT * FROM datasus_cache")
            self._refresh_period_index(self._all_cached_parquets())
        return {
            "group": self.name,
            "files": len(all_files),
            "copied": copied,
            "already_cached": skipped,
            "status": "view_ok" if ok else "view_failed",
        }

    def ingest(self) -> dict[str, Any]:
        started = time.perf_counter()
        all_files = self.discover_files()
        if not all_files:
            return {"group": self.name, "files": 0, "status": "no_files_found"}

        # >>> FASE 2 (S1): modo VIEW sobre Parquet <<<
        # Quando habilitado, o grupo NAO materializa tabela fisica: a ingestao
        # garante apenas que os Parquet estao copiados para o cache e cria uma
        # VIEW datasus_cache lendo todos eles. Elimina a copia duplicada.
        # Deve vir ANTES do manifesto/lotes porque nao ha linhas a inserir.
        if self._cache_as_view_enabled():
            result = self._ingest_as_view(all_files)
            LOGGER.info(
                "grupo %s: S1 ingest concluido em %.2fs (view sobre parquet)",
                self.name, time.perf_counter() - started,
            )
            return result

        manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
        cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"
        # Carrega o manifesto uma única vez: source -> (size, modified, cached, in_db, invalid)
        manifest: dict[str, tuple[int, float, str, int, int]] = {
            source: (size, modified, cached, in_db, invalid)
            for source, size, modified, cached, in_db, invalid in self.connection.execute(
                f"SELECT source, size, modified, cached, in_db, invalid FROM {manifest_table}"
            ).fetchall()
        }

        # Apenas arquivos NÃO presentes no cache (novos ou alterados) são processados.
        to_ingest: list[Path] = []
        already_cached = 0
        for source in all_files:
            rel_str = str(source.relative_to(self.source_dir))
            rec = manifest.get(rel_str)
            st = _retry_stat(source)
            if st is None:
                # Arquivo sumiu / USB desconectado no meio da varredura: salta agora
                # (pode voltar no próximo ingest quando o disco re-montar).
                LOGGER.warning("grupo %s: pulando %s (indisponível/desconectado)", self.name, source)
                continue

            target = self._grouped_cache_path(source)
            if rec is not None and rec[0] == st.st_size and rec[1] == st.st_mtime:
                if rec[3] == 1 and target.exists():
                    already_cached += 1
                    continue
            to_ingest.append(source)

        LOGGER.info("grupo %s: %d novos de %d totais (já em cache: %d)",
                    self.name, len(to_ingest), len(all_files), already_cached)

        if not to_ingest:
            if not self._cache_relation_exists():
                to_ingest = all_files
            else:
                if not self._datasus_view_exists():
                    self._ensure_view_and_index(all_files)
                return {"group": self.name, "files": len(all_files), "changed": 0,
                        "already_cached": already_cached, "cache_hit": True}

        batch_size = 50
        changed = 0
        invalid = 0

        # Checkpoint: retoma do último lote processado (reinício não reprocessa tudo)
        checkpoint_index = self._load_checkpoint()
        if checkpoint_index > 0:
            LOGGER.info("grupo %s: retomando de checkpoint no índice %d (de %d)",
                        self.name, checkpoint_index, len(to_ingest))
            # Arquivos já processados anteriormente podem já estar no DuckDB.
            # O manifest detecta os já inseridos; apenas continuamos do checkpoint.
            checkpoint_index = min(checkpoint_index, len(to_ingest))

        for i in range(checkpoint_index, len(to_ingest), batch_size):
            batch = to_ingest[i:i + batch_size]
            batch_cached: list[str] = []
            batch_sources: list[str] = []
            for source in batch:
                rel_str = str(source.relative_to(self.source_dir))
                target = self._grouped_cache_path(source)
                st = _retry_stat(source)
                if st is None:
                    invalid += 1
                    LOGGER.error("grupo %s: arquivo indisponível (USB?) %s", self.name, source)
                    continue
                try:
                    if not _retry_copyfile(source, target):
                        raise OSError("cópia incompleta do arquivo-fonte")
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
                    # Preserva o arquivo-fonte (dados originais) e remove apenas a cópia inválida
                    # do cache, registrando o arquivo como inválido no manifesto para que não seja
                    # reprocessado indefinidamente numa próxima ingestão.
                    target.unlink(missing_ok=True)
                    self.connection.execute(
                        f"INSERT OR REPLACE INTO {manifest_table} "
                        "(source, size, modified, cached, in_db, invalid) VALUES (?, ?, ?, '', 0, 1)",
                        [rel_str, st.st_size, st.st_mtime],
                    )

            if batch_cached:
                paths_sql = ", ".join(f"'{p.replace(chr(39), chr(39)*2)}'" for p in batch_cached)
                if not self._cache_table_exists():
                    # Usa CREATE OR REPLACE para evitar conflito com nomes que existem
                    # no catálogo main (o DuckDB interpreta `CREATE TABLE ps.datasus_cache`
                    # de forma ambígua quando `datasus_cache` já existe no main).
                    #
                    # >>> CORREÇÃO: período derivado do CAMINHO COMPLETO do parquet
                    # (filename de read_parquet(filename=true)), ancorado no fim do nome
                    # (.parquet) e tolerando sufixo de letra (ex.: SPMG2205a.parquet);
                    # fallback para AAAA/MM das pastas do caminho. Garante
                    # parquet_year/parquet_month preenchidos já na criação.
                    self.connection.execute(
                        f"CREATE OR REPLACE TABLE {cache_table} AS SELECT *, filename AS parquet_path, "
                        "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                        "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year, "
                        "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                        "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month "
                        f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                    )
                    # Rede de segurança: recalcula o período de quem ainda ficou NULL
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
                    self.connection.execute("DROP VIEW IF EXISTS temp_batch")
                    # >>> CORREÇÃO (ver import_batch): inclui 'filename = true' e o alias
                    # parquet_path no lote incremental; sem isso parquet_year/parquet_month
                    # ficavam NULL e o filtro de período descartava o lote inteiro.
                    self.connection.execute(
                        f"CREATE VIEW temp_batch AS SELECT *, filename AS parquet_path "
                        f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                    )
                    existing_upper = {row[0].upper() for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()}
                    for row in self.connection.execute("DESCRIBE temp_batch").fetchall():
                        col_name, col_type = row[0], row[1]
                        if col_name.upper() not in existing_upper:
                            LOGGER.info("grupo %s: nova coluna %s (%s)", self.name, col_name, col_type)
                            self.connection.execute(f'ALTER TABLE {cache_table} ADD COLUMN "{col_name}" {col_type}')
                    table_cols = [r[0] for r in _schema_rows(self.connection, cache_table)]
                    batch_by_upper = {r[0].upper(): r[0] for r in _schema_rows(self.connection, "temp_batch")}
                    projection = []
                    for col in table_cols:
                        esc = col.replace('"', '""')
                        # Os campos de metadados de período são SEMPRE derivados
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
                        else:
                            projection.append('CAST(NULL AS VARCHAR) AS "' + esc + '"')
                    col_list = ", ".join('"' + col.replace('"', '""') + '"' for col in table_cols)
                    self.connection.execute(
                        f"INSERT INTO {cache_table} ({col_list}) SELECT {', '.join(projection)} FROM temp_batch"
                    )
                    self.connection.execute("DROP VIEW temp_batch")
                    # >>> CORREÇÃO: recomputa o período do lote a partir de parquet_path.
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

            # Salva checkpoint após cada lote processado
            self._save_checkpoint(i + len(batch), len(to_ingest))

        self._ensure_view_and_index([self._grouped_cache_path(f) for f in to_ingest])
        duration = (time.perf_counter() - started) * 1000
        LOGGER.info("grupo %s: ingestão completa: %d novos, %d inválidos, %.2fms", self.name, changed, invalid, duration)
        return {"group": self.name, "files": changed, "invalid": invalid,
                "already_cached": already_cached, "cache_hit": False}

    @log_calls
    def pending_batch(self, limit: int = 50) -> tuple[list[Path], int, int]:
        """Identifica o próximo lote de arquivos NÃO cacheados deste grupo.

        Returns:
            (lote, total_pendentes, total_ja_cacheados)
        O lote contém no máximo `limit` arquivos, respeitando a segmentação global.
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
                # próximo ingest, não derruba o processo.
                continue
            target = self._grouped_cache_path(source)
            if rec is not None and rec[0] == st.st_size and rec[1] == st.st_mtime:
                if rec[3] == 1 and target.exists():
                    already_cached += 1
                    continue
                if rec[4] == 1:
                    # Arquivo marcado como inválido: preservado no fonte, não reprocessado.
                    continue
            pending.append(source)

        # Grupo ainda sem tabela/view de cache: trata tudo como pendente (reconstrução).
        if not pending and not self._cache_relation_exists():
            pending = list(all_files)
            already_cached = 0

        batch = pending[:limit]
        LOGGER.info("grupo %s: lote de %d pendentes de %d totais (já em cache: %d)",
                    self.name, len(batch), len(all_files), already_cached)
        return batch, len(pending), already_cached

    @log_calls
    def import_batch(self, batch: list[Path]) -> dict[str, Any]:
        """Importa um lote (até 50 arquivos) dos Parquet no DuckDB deste grupo."""
        changed = 0
        invalid = 0
        batch_cached: list[str] = []
        batch_sources: list[str] = []

        # Total de arquivos para cálculo do percentual de progresso
        all_files = self.discover_files()
        total_files = len(all_files)
        # Arquivos já processados anteriormente (checkpoint)
        processed_before = self._load_checkpoint()

        manifest_table = f"file_manifest" if not self._using_main_connection else f"{self.name}.file_manifest"
        cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"
        group_ckpt_table = f"group_checkpoint" if not self._using_main_connection else f"{self.name}.group_checkpoint"

        for source in batch:
            rel_str = str(source.relative_to(self.source_dir))
            target = self._grouped_cache_path(source)
            st = _retry_stat(source)
            if st is None:
                invalid += 1
                LOGGER.error("grupo %s: arquivo indisponível (USB?) %s", self.name, source)
                continue
            try:
                if not _retry_copyfile(source, target):
                    raise OSError("cópia incompleta do arquivo-fonte")
                self.connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(target)])
                self.connection.execute(
                    f"INSERT OR REPLACE INTO {manifest_table} (source, size, modified, cached, in_db) "
                    "VALUES (?, ?, ?, ?, 0)",
                    [rel_str, st.st_size, st.st_mtime, str(target)],
                )
                batch_cached.append(str(target))
                batch_sources.append(rel_str)
                changed += 1

                # LOG DO ARQUIVO GRAVADO COM NOME, PARQUET, PARTIÇÃO E PERCENTUAL
                year_month = self._extract_period_from_path(target)
                processed_total = processed_before + changed
                percent = (processed_total / total_files * 100) if total_files > 0 else 0
                LOGGER.info(
                    "CACHE GRAVADO | grupo=%s | arquivo=%s | parquet=%s | partição=%s | progresso=%d/%d (%.1f%%)",
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
                # Preserva o arquivo-fonte (dados originais) e remove apenas a cópia inválida
                # do cache, registrando o arquivo como inválido no manifesto para que não seja
                # reprocessado indefinidamente numa próxima ingestão.
                target.unlink(missing_ok=True)
                self.connection.execute(
                    f"INSERT OR REPLACE INTO {manifest_table} "
                    "(source, size, modified, cached, in_db, invalid) VALUES (?, ?, ?, '', 0, 1)",
                    [rel_str, st.st_size, st.st_mtime],
                )

        if batch_cached:
            paths_sql = ", ".join(f"'{p.replace(chr(39), chr(39)*2)}'" for p in batch_cached)
            if not self._cache_table_exists():
                # Usa CREATE OR REPLACE para evitar conflito com nomes que existem
                # no catálogo main (o DuckDB interpreta `CREATE TABLE ps.datasus_cache`
                # de forma ambígua quando `datasus_cache` já existe no main).
                #
                # >>> CORREÇÃO: o período é derivado do CAMINHO COMPLETO do parquet
                # (filename retornado por read_parquet(filename=true)), ancorando o
                # regex no fim do nome (.parquet) e tolerando o sufixo de letra
                # (ex.: SPMG2205a.parquet). Como fallback, extrai AAAA/MM das pastas
                # do caminho (.../<grupo>/<ano>/<mes>/arquivo.parquet). Assim
                # parquet_year/parquet_month são SEMPRE preenchidos na criação.
                self.connection.execute(
                    f"CREATE OR REPLACE TABLE {cache_table} AS SELECT *, filename AS parquet_path, "
                    "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                    "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year, "
                    "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                    "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month "
                    f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                )
                # Rede de segurança: qualquer linha que ainda fique com período NULL
                # é recalculada a partir de parquet_path (idempotente).
                self.connection.execute(
                    f"UPDATE {cache_table} SET "
                    "parquet_year = COALESCE(2000 + CAST(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                    "TRY_CAST(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)), "
                    "parquet_month = COALESCE(NULLIF(regexp_extract(parquet_path, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                    "NULLIF(regexp_extract(parquet_path, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) "
                    "WHERE parquet_year IS NULL AND parquet_path IS NOT NULL"
                )
            else:
                self.connection.execute("DROP VIEW IF EXISTS temp_batch")
                # >>> CORREÇÃO (bug sarampo/B05 -> 0 linhas com dados no cache):
                # inclui 'filename = true' e o alias parquet_path no lote incremental.
                # Sem isso, 'temp_batch' não tinha 'filename'; como a tabela já possui
                # as colunas 'parquet_path'/'parquet_year'/'parquet_month' (criadas no
                # 1º lote), o INSERT preenchia-as com CAST(NULL AS VARCHAR). Com
                # parquet_year NULL, o filtro de período de prepare_question_cache
                # (parquet_year IN (...)) descartava TODO o lote e a consulta retornava
                # 0 mesmo havendo milhares de registros do CID no cache.
                self.connection.execute(
                    f"CREATE VIEW temp_batch AS SELECT *, filename AS parquet_path "
                    f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
                )
                existing_upper = {row[0].upper() for row in self.connection.execute(f"DESCRIBE {cache_table}").fetchall()}
                for row in self.connection.execute("DESCRIBE temp_batch").fetchall():
                    col_name, col_type = row[0], row[1]
                    if col_name.upper() not in existing_upper:
                        LOGGER.info("grupo %s: nova coluna %s (%s)", self.name, col_name, col_type)
                        self.connection.execute(f'ALTER TABLE {cache_table} ADD COLUMN "{col_name}" {col_type}')
                table_cols = [r[0] for r in _schema_rows(self.connection, cache_table)]
                batch_by_upper = {r[0].upper(): r[0] for r in _schema_rows(self.connection, "temp_batch")}
                projection = []
                for col in table_cols:
                    esc = col.replace('"', '""')
                    # Campos de metadados de período são SEMPRE derivados
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
                    else:
                        projection.append('CAST(NULL AS VARCHAR) AS "' + esc + '"')
                col_list = ", ".join('"' + col.replace('"', '""') + '"' for col in table_cols)
                self.connection.execute(
                    f"INSERT INTO {cache_table} ({col_list}) SELECT {', '.join(projection)} FROM temp_batch"
                )
                self.connection.execute("DROP VIEW temp_batch")
                # >>> CORREÇÃO: recomputa parquet_year/parquet_month a partir de
                # parquet_path para o lote recém-inserido. Antes esse UPDATE só existia
                # na criação inicial da tabela, deixando os lotes incrementais com
                # parquet_year NULL.
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

        # Salva checkpoint para o percentual correto no próximo lote
        if changed > 0 or invalid > 0:
            processed_total = self._load_checkpoint() + changed
            self._save_checkpoint(processed_total, total_files)

        return {"group": self.name, "files": changed, "invalid": invalid}

    def _verify_period_integrity(self) -> None:
        """Valida que TODAS as linhas do cache do grupo têm parquet_year preenchido.

        Após qualquer escrita, confere a contagem de linhas sem período. Como o
        filtro de período de prepare_question_cache (parquet_year IN (...)) DESCARTA
        linhas com parquet_year NULL, qualquer linha nessa condição se tornaria
        invisível nas consultas (o bug do sarampo/B05 retornando 0 com dados no
        cache). Aqui, uma eventual violação é registrada em ERROR e corrigida via
        backfill a partir de parquet_path — nunca deixando o cache em estado
        silenciosamente incorreto.
        """
        try:
            # Modo S1: datasus_cache é uma VIEW e parquet_year/parquet_month são
            # derivados na própria definição da view (não há linhas a backfillar).
            # O UPDATE abaixo só é aplicável a tabelas físicas.
            if self._cache_view_exists() and not self._cache_table_exists():
                return
            if not self._cache_relation_exists():
                return
            cache_table = f"datasus_cache" if not self._using_main_connection else f"{self.name}.datasus_cache"

            # Caches legados podem NÃO ter as colunas de metadados (filename,
            # parquet_path, parquet_year, parquet_month). Nesse caso não há o que
            # backfillar: o grupo precisa ser reconstruído (ver
            # dev_tools/rebuild_group_cache.py). Detectamos isso explicitamente para
            # não gerar 'Binder Error: Referenced column parquet_year not found' em
            # consultas de período.
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
                    "consultas com filtro de período falharão para este grupo. "
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
                "grupo %s: INTEGRIDADE DE PERÍODO: %d linhas com parquet_year NULL; "
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
                    "grupo %s: ainda restam %d linhas com parquet_year NULL após backfill "
                    "(sem parquet_path utilizável) — requer reconstrução do grupo.",
                    self.name, missing_after,
                )
            else:
                LOGGER.info("grupo %s: integridade de período restaurada via backfill", self.name)
        except Exception:
            LOGGER.exception("grupo %s: falha ao verificar integridade de período", self.name)

    @log_calls
    def finalize(self) -> None:
        """Garante a view 'datasus' e o índice de período após o cache do grupo."""
        cached = self._cached_group_files()
        if self._cache_relation_exists() and not self._datasus_view_exists():
            if self._using_main_connection:
                self.connection.execute(
                    f"CREATE VIEW {self.name}.datasus AS SELECT * FROM {self.name}.datasus_cache"
                )
            else:
                self.connection.execute("CREATE VIEW datasus AS SELECT * FROM datasus_cache")
        # Garante que nenhuma linha ficou sem período (evita o sintoma de
        # "resultado vazio" em consultas com filtro temporal).
        self._verify_period_integrity()
        if cached:
            self._refresh_period_index(cached)

    def _cached_group_files(self) -> list[Path]:
        return _discover_files_in(self.cache_dir / self.name)

    def _ensure_view_and_index(self, files: list[Path]) -> None:
        if self._cache_relation_exists() and not self._datasus_view_exists():
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