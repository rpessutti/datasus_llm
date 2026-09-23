"""
Testes da FASE 2 (S1): modo VIEW sobre Parquet (DATASUS_CACHE_AS_VIEW=true).

No modo S1 o `datasus_cache` de cada grupo deixa de ser uma TABELA física
(cópia dos Parquet) e passa a ser uma VIEW que lê os Parquet diretamente.
Isso elimina a duplicação de dados.

Cobre:
1. ingest cria datasus_cache como VIEW (não tabela), com parquet_year/parquet_month.
2. Idempotência: reingestão não falha (DROP TABLE em view) e não recopia.
3. Detecção de relação (tabela OU view) via _cache_relation_exists.
4. Integração com prepare_question_cache (grupo-view aparece na query unificada).
5. _verify_period_integrity não tenta UPDATE em view.
6. PARTE 4 (#4): a view S1 expõe atend_ano/atend_mes do CAMPO NATIVO
   (mesmos tipos da tabela física: atend_ano INTEGER, atend_mes VARCHAR) e o
   recorte temporal tem PARIDADE com a tabela física.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Garante o modo VIEW antes de importar os módulos que leem a flag.
os.environ["DATASUS_CACHE_AS_VIEW"] = "true"

from db.cache import DatasusCache  # noqa: E402
from db.group_cache import _GroupCache  # noqa: E402


class _FakeSettings:
    """Settings mínimo para instanciar DatasusCache em diretórios temporários."""

    def __init__(self, data_dir: Path, cache_dir: Path) -> None:
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.official_cache_dir = cache_dir / "official"
        self.database_path = cache_dir / "datasus.duckdb"


def _make_parquet(path: Path, dt: int, cid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        f"COPY (SELECT {dt} AS DT_INTER, '{cid}' AS CID) TO '{path}' (FORMAT PARQUET)"
    )


class GroupCacheS1Test(unittest.TestCase):
    """Modo VIEW no nível do _GroupCache."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s1_group_"))
        self.data = self.tmp / "data"
        self.cache = self.tmp / "cache"
        self.cache.mkdir(parents=True)
        # 2 Parquets do grupo 'er' em estrutura data/er/AAAA/MM/
        for ano, mes, nome, dt, cid in [
            ("2022", "05", "ER2205.parquet", 20220510, "B05"),
            ("2022", "06", "ER2206.parquet", 20220615, "A90"),
        ]:
            _make_parquet(self.data / "er" / ano / mes / nome, dt, cid)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _group(self) -> _GroupCache:
        g = _GroupCache("er", self.data / "er", self.cache, None)
        g.reopen()
        return g

    def test_cache_as_view_enabled(self) -> None:
        self.assertTrue(_GroupCache("er", self.data / "er", self.cache, None)._cache_as_view_enabled())

    def test_ingest_cria_view_com_periodo(self) -> None:
        g = self._group()
        try:
            result = g.ingest()
            self.assertEqual(result["status"], "view_ok")
            self.assertEqual(result["copied"], 2)

            # datasus_cache é VIEW, não tabela.
            is_view = bool(
                g.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_views() WHERE view_name = 'datasus_cache'"
                ).fetchone()[0]
            )
            is_table = bool(
                g.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = 'datasus_cache'"
                ).fetchone()[0]
            )
            self.assertTrue(is_view)
            self.assertFalse(is_table)

            rows = g.connection.execute(
                "SELECT parquet_year, parquet_month FROM datasus_cache ORDER BY parquet_year, parquet_month"
            ).fetchall()
            self.assertEqual(rows, [(2022, "05"), (2022, "06")])

            # PARTE 4 (#4): a view S1 TAMBÉM expõe atend_ano/atend_mes do CAMPO
            # NATIVO (DT_INTER), com os mesmos tipos da tabela física (FASE 3B):
            # atend_ano INTEGER, atend_mes VARCHAR. parquet_year/parquet_month
            # continuam presentes por compatibilidade.
            types = {
                r[0].lower(): str(r[1]).upper()
                for r in g.connection.execute("DESCRIBE datasus_cache").fetchall()
            }
            self.assertIn("atend_ano", types)
            self.assertIn("atend_mes", types)
            self.assertIn("INT", types["atend_ano"])
            self.assertIn("VARCHAR", types["atend_mes"])
            self.assertIn("parquet_year", types)

            atend = g.connection.execute(
                "SELECT atend_ano, atend_mes FROM datasus_cache ORDER BY atend_ano, atend_mes"
            ).fetchall()
            self.assertEqual(atend, [(2022, "05"), (2022, "06")])
        finally:
            g.close()

    def test_ingest_idempotente(self) -> None:
        g = self._group()
        try:
            first = g.ingest()
            self.assertEqual(first["status"], "view_ok")
            # Segunda execução não deve falhar (DROP TABLE em view) nem recopiar.
            second = g.ingest()
            self.assertEqual(second["status"], "view_ok")
            self.assertEqual(second["copied"], 0)
            self.assertEqual(second["already_cached"], 2)
        finally:
            g.close()

    def test_cache_relation_exists_cobre_view(self) -> None:
        g = self._group()
        try:
            g.ingest()
            self.assertTrue(g._cache_view_exists())
            self.assertTrue(g._cache_relation_exists())
        finally:
            g.close()

    def test_verify_period_integrity_nao_falha_em_view(self) -> None:
        g = self._group()
        try:
            g.ingest()
            # Não deve levantar exceção (o backfill UPDATE é pulado em view).
            g.finalize()
        finally:
            g.close()

    def test_view_s1_expoe_atend_nativo_e_paridade_recorte(self) -> None:
        """PARTE 4 (#4): a view S1 deriva atend_ano/atend_mes do CAMPO NATIVO.

        Paridade S1↔TABELA: o recorte temporal por atend_ano/atend_mes deve
        devolver EXATAMENTE as mesmas linhas que a tabela física (FASE 3)
        devolveria para os mesmos dados — i.e., os valores coincidem com os
        derivados de DT_INTER (20220510 -> 2022/05; 20220615 -> 2022/06).
        """
        g = self._group()
        try:
            g.ingest()
            # atend_ano/atend_mes derivados do campo nativo (não do nome do arquivo).
            self.assertEqual(
                g.connection.execute(
                    "SELECT atend_ano, atend_mes FROM datasus_cache "
                    "WHERE atend_ano = 2022 AND atend_mes = '05'"
                ).fetchone(),
                (2022, "05"),
            )
            # Recorte por período nativo conta as linhas certas.
            n_maio = g.connection.execute(
                "SELECT COUNT(*) FROM datasus_cache WHERE atend_ano = 2022 AND atend_mes = '05'"
            ).fetchone()[0]
            self.assertEqual(n_maio, 1)
            # Um mês sem dados não retorna nada (o filtro é pelo campo nativo).
            n_vazio = g.connection.execute(
                "SELECT COUNT(*) FROM datasus_cache WHERE atend_ano = 2022 AND atend_mes = '07'"
            ).fetchone()[0]
            self.assertEqual(n_vazio, 0)
        finally:
            g.close()


class DatasusCacheS1Test(unittest.TestCase):
    """Integração do modo VIEW com o pipeline de perguntas."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s1_cache_"))
        self.data = self.tmp / "data"
        self.cache = self.tmp / "cache"
        self.cache.mkdir(parents=True)
        for ano, mes, nome, dt, cid in [
            ("2022", "05", "ER2205.parquet", 20220510, "B05"),
            ("2022", "06", "ER2206.parquet", 20220615, "B05"),
        ]:
            _make_parquet(self.data / "er" / ano / mes / nome, dt, cid)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache(self) -> DatasusCache:
        return DatasusCache(_FakeSettings(self.data, self.cache))

    def test_prepare_question_cache_usa_grupo_view(self) -> None:
        c = self._cache()
        try:
            c._discover_groups()
            for group in c.groups.values():
                group.reopen()
                self.assertEqual(group.ingest()["status"], "view_ok")

            sql = "SELECT CID, COUNT(*) AS casos FROM datasus_query_cache GROUP BY CID"
            c.prepare_question_cache("casos de dengue em 2022", "req_s1", sql=sql)

            rows = c.connection.execute(sql).fetchall()
            self.assertEqual(rows, [("B05", 2)])
        finally:
            c.close()

    def test_status_reporta_grupo_view(self) -> None:
        c = self._cache()
        try:
            c._discover_groups()
            for group in c.groups.values():
                group.reopen()
                group.ingest()
            c.mark_ready()
            status = c.status()
            groups = status.get("groups", {})
            self.assertIn("er", groups)
            self.assertEqual(groups["er"]["rows"], 2)
            self.assertTrue(groups["er"]["cache_table"])
        finally:
            c.close()

    def test_prepare_question_cache_s1_usa_campo_nativo(self) -> None:
        """PARTE 4 (#4): em S1 o recorte usa atend_ano/atend_mes do campo nativo.

        A view unificada `datasus_query_cache` NÃO deve derivar período de
        parquet_year/parquet_month; o WHERE usa atend_ano/atend_mes (nativos).
        """
        c = self._cache()
        try:
            c._discover_groups()
            for group in c.groups.values():
                group.reopen()
                self.assertEqual(group.ingest()["status"], "view_ok")

            sql = "SELECT COUNT(*) AS n FROM datasus_query_cache"
            c.prepare_question_cache("casos de dengue em maio de 2022", "req_s1_nat", sql=sql)

            view_sql = c.connection.execute(
                "SELECT sql FROM duckdb_views() WHERE view_name = 'datasus_query_cache'"
            ).fetchone()[0]
            # O recorte usa as colunas nativas (não parquet_year/parquet_month).
            self.assertIn("atend_ano", view_sql)
            self.assertIn("atend_mes", view_sql)
            # Mês solicitado (maio) restringe; junho fica de fora.
            rows = c.connection.execute(sql).fetchall()
            self.assertEqual(rows, [(1,)])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)