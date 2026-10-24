# -*- coding: utf-8 -*-
"""Testes da FASE 3 (#3): materialização de atend_ano/atend_mes na ingestão.

Objetivo: contar com colunas FÍSICAS `atend_ano`/`atend_mes` em
``<grupo>.datasus_cache`` (TABELA), derivadas do CAMPO NATIVO de período de
cada grupo (SP_DTINTER, DT_INTER, DT_ATEND, PA_CMP, AP_DTINIC — ver
db/period_fields.py). Isso permite que o UNION use colunas físicas (push-down)
em vez de SUBSTR(CAST(...)) a cada consulta.

Cobre os checkpoints internos da PARTE 3:
  * CHECKPOINT A: migração idempotente (reopen 2x, colunas não duplicam).
  * CHECKPOINT A2 (FASE 3B): migração de tipo VARCHAR -> INTEGER idempotente.
  * CHECKPOINT B: DESCRIBE mostra as colunas; contagem de linhas inalterada.
  * CHECKPOINT B2 (FASE 3B): atend_ano é INTEGER; atend_mes é VARCHAR.
  * CHECKPOINT C: lote incremental preenche atend_ano/atend_mes (não NULL).
  * CHECKPOINT D: backfill deixa COUNT(atend_ano IS NULL) = 0.
  * CHECKPOINT E: UNION de prepare_question_cache usa as colunas físicas e o
    recorte temporal continua correto.
  * Grupo SEM campo de período mapeado permanece sem as colunas.

IMPORTANTE: este modulo roda em modo TABELA (o antigo modo VIEW/S1 foi
removido). A ingestao sempre materializa a tabela ``<grupo>.datasus_cache``.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from db.cache import DatasusCache  # noqa: E402
from db.group_cache import _GroupCache  # noqa: E402


class _FakeSettings:
    """Settings mínimo para instanciar DatasusCache em diretórios temporários."""

    def __init__(self, data_dir: Path, cache_dir: Path) -> None:
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.official_cache_dir = cache_dir / "official"
        self.database_path = cache_dir / "datasus.duckdb"


def _make_parquet(path: Path, dtype_col: str, dt: int, cid: str) -> None:
    """Cria um Parquet com uma coluna de data nativa (DT_INTER ou PA_CMP) + CID."""
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        f"COPY (SELECT {dt} AS {dtype_col}, '{cid}' AS CID) TO '{path}' (FORMAT PARQUET)"
    )


class AtendColumnsF3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="f3_atend_"))
        self.data = self.tmp / "data"
        self.cache = self.tmp / "cache"
        self.cache.mkdir(parents=True)
        # Grupo 'er' usa DT_INTER (date8: AAAAMMDD).
        _make_parquet(self.data / "er" / "2022" / "05" / "ER2205.parquet", "DT_INTER", 20220510, "B05")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache(self) -> DatasusCache:
        c = DatasusCache(_FakeSettings(self.data, self.cache))
        c.sync_groups()
        return c

    def _ingest_all(self, c: DatasusCache) -> None:
        for group in c.groups.values():
            group.reopen()
            group.ingest()

    # ------------------------------------------------------------------ #
    # CHECKPOINT A/B: colunas físicas criadas na ingestão + reopen 2x
    # ------------------------------------------------------------------ #
    def test_checkpoint_ab_cria_colunas_e_reopen_idempotente(self) -> None:
        c = self._cache()
        try:
            self._ingest_all(c)

            er = c.groups["er"]
            er.reopen()

            cols = [r[0].lower() for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()]
            self.assertIn("atend_ano", cols)
            self.assertIn("atend_mes", cols)

            # FASE 3B (CHECKPOINT B2): atend_ano é INTEGER; atend_mes é VARCHAR.
            types = {
                r[0].lower(): str(r[1]).upper()
                for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()
            }
            self.assertIn("INT", types["atend_ano"])
            self.assertIn("VARCHAR", types["atend_mes"])

            # Valores derivados do CAMPO NATIVO DT_INTER (20220510 -> 2022 / 05).
            # atend_ano é INTEGER (FASE 3B); atend_mes continua VARCHAR '01'..'12'.
            ano, mes = er.connection.execute(
                "SELECT atend_ano, atend_mes FROM er.datasus_cache LIMIT 1"
            ).fetchone()
            self.assertEqual(ano, 2022)
            self.assertIsInstance(ano, int)
            self.assertEqual(mes, "05")

            # CHECKPOINT A: reopen 2x NÃO duplica colunas nem falha (idempotência).
            er.close()
            er.reopen()
            er.reopen()
            cols2 = [r[0].lower() for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()]
            self.assertEqual(cols2.count("atend_ano"), 1)
            self.assertEqual(cols2.count("atend_mes"), 1)

            # CHECKPOINT B: contagem de linhas inalterada.
            n = er.connection.execute("SELECT COUNT(*) FROM er.datasus_cache").fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # CHECKPOINT A (migração real via ALTER TABLE sobre cache SEM as colunas)
    # ------------------------------------------------------------------ #
    def test_checkpoint_a_migra_cache_sem_colunas(self) -> None:
        """Simula um cache LEGADO (sem atend_ano/atend_mes) e valida a migração.

        Diferente do CHECKPOINT A/B (que testa a criação já com as colunas), aqui
        removemos as colunas da tabela (DROP COLUMN) e confirmamos que reopen()
        (via _migrate_period_columns) as re-adiciona, faz o backfill e PRESERVA a
        contagem de linhas — sem duplicar nada.
        """
        c = self._cache()
        try:
            self._ingest_all(c)
            er = c.groups["er"]
            er.reopen()

            n_antes = er.connection.execute("SELECT COUNT(*) FROM er.datasus_cache").fetchone()[0]
            self.assertEqual(n_antes, 1)

            # Remove as colunas para simular um cache anterior à FASE 3.
            er.connection.execute("ALTER TABLE er.datasus_cache DROP COLUMN atend_ano")
            er.connection.execute("ALTER TABLE er.datasus_cache DROP COLUMN atend_mes")
            cols = [r[0].lower() for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()]
            self.assertNotIn("atend_ano", cols)
            self.assertNotIn("atend_mes", cols)

            # reabre -> migração deve re-adicionar e backfillar.
            er.close()
            er.reopen()
            cols2 = [r[0].lower() for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()]
            self.assertIn("atend_ano", cols2)
            self.assertIn("atend_mes", cols2)

            # Contagem de linhas preservada e backfill aplicado.
            n_depois = er.connection.execute("SELECT COUNT(*) FROM er.datasus_cache").fetchone()[0]
            self.assertEqual(n_depois, n_antes)
            missing = er.connection.execute(
                "SELECT COUNT(*) FROM er.datasus_cache WHERE atend_ano IS NULL"
            ).fetchone()[0]
            self.assertEqual(missing, 0)
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # CHECKPOINT A2 (FASE 3B): migração de tipo VARCHAR -> INTEGER
    # ------------------------------------------------------------------ #
    def test_checkpoint_a2_migra_varchar_para_integer(self) -> None:
        """Simula um cache criado pela PARTE 3 (atend_ano VARCHAR) e reabre.

        A migração deve detectar o tipo INCOMPATÍVEL e CONVERTER a coluna para
        INTEGER (sem apagar linhas), tornando `atend_ano` numérico. Idempotente:
        reabrir de novo NÃO re-converte.
        """
        c = self._cache()
        try:
            self._ingest_all(c)
            er = c.groups["er"]
            er.reopen()

            n_antes = er.connection.execute("SELECT COUNT(*) FROM er.datasus_cache").fetchone()[0]
            self.assertEqual(n_antes, 1)

            # Simula cache da PARTE 3: atend_ano VARCHAR com valor '2022'.
            er.connection.execute(
                "ALTER TABLE er.datasus_cache ALTER COLUMN atend_ano TYPE VARCHAR"
            )
            er.connection.execute("UPDATE er.datasus_cache SET atend_ano = '2022'")
            tipo_antes = {
                r[0].lower(): str(r[1]).upper()
                for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()
            }
            self.assertIn("VARCHAR", tipo_antes["atend_ano"])

            # reabre -> migração converte VARCHAR -> INTEGER.
            er.close()
            er.reopen()
            tipo_depois = {
                r[0].lower(): str(r[1]).upper()
                for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()
            }
            self.assertIn("INT", tipo_depois["atend_ano"])

            # Dados preservados e convertidos.
            n_depois = er.connection.execute("SELECT COUNT(*) FROM er.datasus_cache").fetchone()[0]
            self.assertEqual(n_depois, n_antes)
            ano = er.connection.execute("SELECT atend_ano FROM er.datasus_cache LIMIT 1").fetchone()[0]
            self.assertEqual(ano, 2022)
            self.assertIsInstance(ano, int)

            # Idempotência: reabrir de novo mantém INTEGER e não falha.
            er.close()
            er.reopen()
            tipo_final = {
                r[0].lower(): str(r[1]).upper()
                for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()
            }
            self.assertIn("INT", tipo_final["atend_ano"])
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # CHECKPOINT C: lote incremental preenche atend_ano/atend_mes
    # ------------------------------------------------------------------ #
    def test_checkpoint_c_lote_incremental_preenche(self) -> None:
        c = self._cache()
        try:
            self._ingest_all(c)
            er = c.groups["er"]
            er.reopen()

            # Novo Parquet em outro mês (lote incremental) com campo nativo.
            _make_parquet(self.data / "er" / "2022" / "06" / "ER2206.parquet", "DT_INTER", 20220615, "A90")
            # Usa o CAMINHO DE PRODUÇÃO (pending_batch + import_batch), que é o
            # efetivamente usado pela ingestão por grupos em db/cache.py.
            er.close()
            er.reopen()
            batch, _, _ = er.pending_batch(limit=50)
            self.assertEqual(len(batch), 1)
            res = er.import_batch(batch)
            self.assertEqual(res["files"], 1)

            # Nenhuma linha deve ter atend_ano/atend_mes NULL.
            missing = er.connection.execute(
                "SELECT COUNT(*) FROM er.datasus_cache "
                "WHERE atend_ano IS NULL OR atend_mes IS NULL"
            ).fetchone()[0]
            self.assertEqual(missing, 0)

            rows = er.connection.execute(
                "SELECT atend_ano, atend_mes FROM er.datasus_cache ORDER BY atend_ano, atend_mes"
            ).fetchall()
            self.assertEqual(rows, [(2022, "05"), (2022, "06")])
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # CHECKPOINT D: backfill preenche NULLs de um cache "legado"
    # ------------------------------------------------------------------ #
    def test_checkpoint_d_backfill_preenche_nulos(self) -> None:
        c = self._cache()
        try:
            self._ingest_all(c)
            er = c.groups["er"]
            er.reopen()

            # Simula um cache legado: zera as colunas derivadas e re-backfilla.
            er.connection.execute("UPDATE er.datasus_cache SET atend_ano = NULL, atend_mes = NULL")
            missing_before = er.connection.execute(
                "SELECT COUNT(*) FROM er.datasus_cache WHERE atend_ano IS NULL"
            ).fetchone()[0]
            self.assertEqual(missing_before, 1)

            er._backfill_period_columns()
            missing_after = er.connection.execute(
                "SELECT COUNT(*) FROM er.datasus_cache WHERE atend_ano IS NULL"
            ).fetchone()[0]
            self.assertEqual(missing_after, 0)

            # Idempotente: rodar de novo não muda nada.
            er._backfill_period_columns()
            self.assertEqual(
                er.connection.execute(
                    "SELECT COUNT(*) FROM er.datasus_cache WHERE atend_ano IS NULL"
                ).fetchone()[0],
                0,
            )
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # Migração in-place é bloqueada acima de MIGRATE_MAX_ROWS
    # ------------------------------------------------------------------ #
    def test_migracao_in_place_bloqueada_acima_do_limite(self) -> None:
        """Com o limite em 0, um cache legado NÃO recebe as colunas de período.

        Evita que uma tabela gigante (ex.: `bi`, 929 M linhas) seja reescrita
        dentro do boot: nesse caso o grupo precisa de rebuild.
        """
        c = self._cache()
        try:
            self._ingest_all(c)
            er = c.groups["er"]
            er.reopen()
            er.connection.execute("ALTER TABLE er.datasus_cache DROP COLUMN atend_ano")
            er.connection.execute("ALTER TABLE er.datasus_cache DROP COLUMN atend_mes")

            with mock.patch("db.group_cache.MIGRATE_MAX_ROWS", 0):
                er._migrate_period_columns()

            cols = {
                r[0].lower()
                for r in er.connection.execute("DESCRIBE er.datasus_cache").fetchall()
            }
            self.assertNotIn("atend_ano", cols)
            self.assertNotIn("atend_mes", cols)
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # finalize: view datasus + period_index (sem varredura de integridade)
    # ------------------------------------------------------------------ #
    def test_finalize_publica_view_e_indice_de_periodo(self) -> None:
        """finalize(verify_period=False) cria a view e o índice de período."""
        c = self._cache()
        try:
            self._ingest_all(c)
            er = c.groups["er"]
            er.reopen()
            er.finalize(verify_period=False)

            views = {
                r[0]
                for r in er.connection.execute(
                    "SELECT view_name FROM duckdb_views() WHERE view_name = 'datasus'"
                ).fetchall()
            }
            self.assertIn("datasus", views)
            periodos = er.connection.execute(
                "SELECT parquet_year, parquet_month FROM er.period_index"
            ).fetchall()
            self.assertEqual(periodos, [("2022", "05")])

            # O catalogo publica o numero de ARQUIVOS cacheados (nao linhas do
            # manifesto) - base do `groups_needing_ingest`.
            files = er.connection.execute(
                "SELECT files FROM group_registry WHERE name = 'er'"
            ).fetchone()[0]
            self.assertEqual(files, 1)
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # CHECKPOINT E: UNION usa colunas físicas e recorte continua correto
    # ------------------------------------------------------------------ #
    def test_checkpoint_e_union_usa_colunas_fisicas(self) -> None:
        c = self._cache()
        try:
            self._ingest_all(c)

            # Pergunta de maio/2022: só a linha de maio deve aparecer.
            c.prepare_question_cache(
                "casos em maio de 2022",
                "req_e",
                sql="SELECT atend_ano, atend_mes, CID FROM datasus_query_cache",
            )
            view_sql = c.connection.execute(
                "SELECT sql FROM duckdb_views() WHERE view_name = 'datasus_query_cache'"
            ).fetchone()
            self.assertIsNotNone(view_sql)
            # A view unificada deve referenciar as colunas FÍSICAS atend_ano/atend_mes.
            self.assertIn("atend_ano", view_sql[0])
            self.assertIn("atend_mes", view_sql[0])
            # Não deve mais derivar via SUBSTR(CAST(...)) quando a tabela é física.
            self.assertNotIn("SUBSTR(CAST", view_sql[0])

            rows = c.connection.execute(
                "SELECT atend_ano, atend_mes FROM datasus_query_cache ORDER BY atend_ano, atend_mes"
            ).fetchall()
            self.assertEqual(rows, [(2022, "05")])
        finally:
            c.close()

    # ------------------------------------------------------------------ #
    # Grupo SEM período mapeado: continua sem as colunas físicas
    # ------------------------------------------------------------------ #
    def test_grupo_sem_periodo_nao_cria_colunas(self) -> None:
        # Cria um grupo 'xx' que NÃO está em PERIOD_FIELDS.
        _make_parquet(self.data / "xx" / "2022" / "05" / "XX2205.parquet", "DT_INTER", 20220510, "Z00")
        c = self._cache()
        try:
            self._ingest_all(c)
            self.assertIn("xx", c.groups)
            xx = c.groups["xx"]
            xx.reopen()
            cols = [r[0].lower() for r in xx.connection.execute("DESCRIBE xx.datasus_cache").fetchall()]
            self.assertNotIn("atend_ano", cols)
            self.assertNotIn("atend_mes", cols)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
