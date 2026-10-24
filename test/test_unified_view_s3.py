"""
Testes da FASE 3 (S3): VIEW unificada lazy (DATASUS_UNIFIED_VIEW=true).

No modo S3 o `datasus_query_cache` deixa de ser uma TEMP TABLE materializada
(UNION ALL dos grupos copiado para uma tabela) e passa a ser uma TEMP VIEW
avaliada sob demanda. Objetivo: reduzir pico de memória e tempo de preparo,
reversível via DATASUS_UNIFIED_VIEW=false.

Cobre:
1. Com a flag ligada, prepare_question_cache cria datasus_query_cache como VIEW.
2. execute() detecta a VIEW (information_schema.views) e NÃO levanta RuntimeError.
3. Resultados idênticos ao modo TEMP TABLE (paridade de consulta).
4. A VIEW unificada respeita o recorte de período (campos nativos do DATASUS).
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

# Garante o modo VIEW unificada antes de importar os módulos que leem a flag.
os.environ["DATASUS_UNIFIED_VIEW"] = "true"

from db.cache import DatasusCache  # noqa: E402


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


class DatasusCacheS3Test(unittest.TestCase):
    """Modo VIEW unificada (lazy) no nível do DatasusCache."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s3_cache_"))
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
        c = DatasusCache(_FakeSettings(self.data, self.cache))
        c.sync_groups()
        for group in c.groups.values():
            group.reopen()
            group.ingest()
        return c

    def test_grupos_registrados(self) -> None:
        """O cache descobre os grupos (a view unificada e sempre montada)."""
        c = DatasusCache(_FakeSettings(self.data, self.cache))
        try:
            self.assertIn("er", c.groups)
        finally:
            c.close()

    def test_prepare_cria_view_nao_tabela(self) -> None:
        c = self._cache()
        try:
            c.prepare_question_cache("casos de dengue em 2022", "req_s3")
            is_view = bool(
                c.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_views() WHERE view_name = 'datasus_query_cache'"
                ).fetchone()[0]
            )
            # information_schema.tables inclui VIEWs; duckdb_tables() NÃO.
            is_table = bool(
                c.connection.execute(
                    "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = 'datasus_query_cache'"
                ).fetchone()[0]
            )
            self.assertTrue(is_view, "datasus_query_cache deveria ser VIEW no modo S3")
            self.assertFalse(is_table, "datasus_query_cache NÃO deveria ser TEMP TABLE")
        finally:
            c.close()

    def test_execute_detecta_view(self) -> None:
        c = self._cache()
        try:
            sql = "SELECT CID, COUNT(*) AS casos FROM datasus_query_cache GROUP BY CID"
            c.prepare_question_cache("casos de dengue em 2022", "req_s3", sql=sql)
            # Não deve levantar RuntimeError (a checagem precisa enxergar a VIEW).
            rows = c.execute(sql, "req_s3")
            self.assertEqual(rows, [{"CID": "B05", "casos": 2}])
        finally:
            c.close()

    def test_recorte_de_periodo(self) -> None:
        c = self._cache()
        try:
            sql = "SELECT COUNT(*) AS total FROM datasus_query_cache"
            # Apenas 2022/05 deve casar -> 1 registro.
            c.prepare_question_cache("total de casos em maio de 2022", "req_s3", sql=sql)
            rows = c.execute(sql, "req_s3")
            self.assertEqual(rows, [{"total": 1}])
        finally:
            c.close()

    def test_grupo_sem_periodo_e_excluido_do_union(self) -> None:
        """PARTE 1 (#5): grupo SEM campo de período mapeado NÃO entra no UNION.

        Antes, um grupo sem PERIOD_FIELDS entrava com atend_ano/atend_mes NULL
        e WHERE TRUE — contamina o recorte (linhas aparecem em qualquer período).
        Agora ele deve ser EXCLUÍDO do UNION: se todos os grupos do exemplo
        forem filtrados, prepare_question_cache deve cair no erro já existente
        ("Nenhum período do cache corresponde...").
        """
        # Simula um grupo cujo nome NÃO consta em PERIOD_FIELDS.
        from db import cache as cache_mod
        original = dict(cache_mod.PERIOD_FIELDS)
        c = self._cache()
        try:
            # Remove o mapeamento de período do grupo 'er' que acabou de ser ingerido.
            for name in list(c.groups.keys()):
                cache_mod.PERIOD_FIELDS.pop(name, None)
            with self.assertRaises(RuntimeError) as ctx:
                c.prepare_question_cache("casos de dengue em 2022", "req_no_period")
            self.assertIn("Nenhum período", str(ctx.exception))
        finally:
            cache_mod.PERIOD_FIELDS.clear()
            cache_mod.PERIOD_FIELDS.update(original)
            c.close()

    def test_paridade_com_modo_tabela(self) -> None:
        sql = "SELECT CID, COUNT(*) AS casos FROM datasus_query_cache GROUP BY CID ORDER BY CID"
        # Modo S3 (view)
        c_view = self._cache()
        try:
            c_view.prepare_question_cache("casos de dengue em 2022", "s3", sql=sql)
            rows_view = c_view.connection.execute(sql).fetchall()
        finally:
            c_view.close()

        # Modo materializado (table): desliga a flag temporariamente.
        prev = os.environ.get("DATASUS_UNIFIED_VIEW")
        os.environ["DATASUS_UNIFIED_VIEW"] = "false"
        try:
            c_tbl = self._cache()
            try:
                c_tbl.prepare_question_cache("casos de dengue em 2022", "tbl", sql=sql)
                rows_tbl = c_tbl.connection.execute(sql).fetchall()
            finally:
                c_tbl.close()
        finally:
            if prev is None:
                os.environ.pop("DATASUS_UNIFIED_VIEW", None)
            else:
                os.environ["DATASUS_UNIFIED_VIEW"] = prev

        self.assertEqual(rows_view, rows_tbl)
        self.assertEqual(rows_view, [("B05", 2)])


if __name__ == "__main__":
    unittest.main(verbosity=2)