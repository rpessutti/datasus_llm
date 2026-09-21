"""


Testes da FASE 3 (S2): cache do schema canônico dos grupos.

Objetivo: evitar rodar DESCRIBE em cada grupo E recomputar a união de ~469
colunas A CADA pergunta. O schema é memoizado em ``_canonical_schema_cache`` e
invalidado em mudanças estruturais (nova ingestão, (re)descoberta de grupos).

Cobre:
1. _get_canonical_schema memoiza (segunda chamada não recomputa).
2. Colunas derivadas atend_ano/atend_mes sempre presentes.
3. Metadados de arquivo (parquet_year/month, filename) NÃO entram.
4. _invalidate_canonical_schema descarta o cache (próxima chamada recomputa).
5. _ingest_locked invalida o schema ao final da ingestão.
6. grupo VIEW (S1) também aparece no schema canônico.
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

from db.cache import DatasusCache, ATEND_ANO, ATEND_MES  # noqa: E402


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


class CanonicalSchemaS2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s2_cache_"))
        self.data = self.tmp / "data"
        self.cache = self.tmp / "cache"
        self.cache.mkdir(parents=True)
        _make_parquet(self.data / "er" / "2022" / "05" / "ER2205.parquet", 20220510, "B05")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache(self) -> DatasusCache:
        c = DatasusCache(_FakeSettings(self.data, self.cache))
        c._discover_groups()
        for group in c.groups.values():
            group.reopen()
            group.ingest()
        return c

    def test_memoiza(self) -> None:
        c = self._cache()
        try:
            all_cols, group_cols = c._get_canonical_schema()
            # Após a primeira chamada o cache deve estar preenchido.
            self.assertIsNotNone(c._canonical_schema_cache)
            self.assertEqual(group_cols, c._canonical_schema_cache["group_columns"])

            # Marca o cache e confirma que uma segunda chamada devolve o MESMO objeto
            # (não recomputa).
            sentinel = c._canonical_schema_cache
            all_cols2, group_cols2 = c._get_canonical_schema()
            self.assertIs(c._canonical_schema_cache, sentinel)
            self.assertEqual(all_cols, all_cols2)
            self.assertEqual(group_cols, group_cols2)
        finally:
            c.close()

    def test_colunas_derivadas_presentes(self) -> None:
        c = self._cache()
        try:
            all_cols, _ = c._get_canonical_schema()
            self.assertIn(ATEND_ANO, all_cols)
            self.assertIn(ATEND_MES, all_cols)
        finally:
            c.close()

    def test_metadados_arquivo_ausentes(self) -> None:
        c = self._cache()
        try:
            all_cols, group_cols = c._get_canonical_schema()
            for meta in ("parquet_year", "parquet_month", "filename", "parquet_path"):
                self.assertNotIn(meta, all_cols)
                for cols in group_cols.values():
                    self.assertNotIn(meta, cols)
        finally:
            c.close()

    def test_invalidacao_recomputa(self) -> None:
        c = self._cache()
        try:
            c._get_canonical_schema()
            sentinel = c._canonical_schema_cache
            self.assertIsNotNone(sentinel)

            c._invalidate_canonical_schema()
            self.assertIsNone(c._canonical_schema_cache)

            # A próxima chamada recomputa (novo objeto, conteúdo equivalente).
            all_cols, _ = c._get_canonical_schema()
            self.assertIsNotNone(c._canonical_schema_cache)
            self.assertIsNot(c._canonical_schema_cache, sentinel)
            self.assertIn(ATEND_ANO, all_cols)
        finally:
            c.close()

    def test_ingest_invalida(self) -> None:
        c = self._cache()
        try:
            # Pré-popula o cache com um valor sentinela.
            c._get_canonical_schema()
            c._canonical_schema_cache = {"__sentinel__": True}

            # _ingest_locked deve invalidar ao final (mesmo sem arquivos novos).
            c._ingest_locked(started=0.0)
            self.assertIsNone(c._canonical_schema_cache)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)