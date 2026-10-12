"""Testes do reparo do cache de grupos duplicado (2x).

Exercita ``dev_tools/repair_group_cache_duplication.py`` com dados FAKE em um
diretório temporário (DuckDB + Parquet pequenos) — NÃO toca no cache real:

- monta um ``<grupo>.duckdb`` com ``datasus_cache`` DUPLICADA (o mesmo arquivo
  com ``parquet_path`` "antigo" de outra máquina e com o caminho atual) e
  ``file_manifest`` com 2 entradas sendo uma órfã (caminho inexistente);
- exige que o reparo mantenha APENAS as linhas do cache atual (razão 1,000),
  remova a entrada órfã, NUNCA esvazie a tabela e não apague nenhum Parquet;
- verifica que a reescrita do arquivo preserva linhas e manifesto.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dev_tools.repair_group_cache_duplication import (  # noqa: E402
    compactar_arquivo,
    conectar,
    deduplicar_tabela,
    diagnosticar,
    limpar_manifesto,
    manifesto_orfaos,
    parquets_do_grupo,
    reparar,
    reparar_por_ano,
)

GRUPO = "zz"
LINHAS_POR_ARQUIVO = 20
CAMINHO_ANTIGO = "/mnt/antigo/cache"


class RepairGroupCacheTest(unittest.TestCase):
    """Cenário: grupo com a tabela e o manifesto duplicados (migração Linux→Windows)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="repair_cache_"))
        self.cache = self.tmp / "cache"
        self.group_dir = self.cache / GRUPO
        self.parquets: list[Path] = []
        for ano in (2024, 2025):
            destino = self.group_dir / str(ano) / "01" / f"ZZ{str(ano)[2:]}01.parquet"
            destino.parent.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(":memory:")
            try:
                con.execute(
                    f"COPY (SELECT i AS CID, 'acidente' AS NOME, {ano} AS AA "
                    f"FROM range({LINHAS_POR_ARQUIVO}) t(i)) TO '{destino}' (FORMAT PARQUET)"
                )
            finally:
                con.close()
            self.parquets.append(destino)

        self.total_linhas = LINHAS_POR_ARQUIVO * len(self.parquets)
        self.db = self.cache / f"{GRUPO}.duckdb"
        con = conectar(self.db)
        try:
            lista = "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in self.parquets) + "]"
            con.execute(
                "CREATE TABLE datasus_cache AS SELECT CID, NOME, AA, AA AS parquet_year, "
                f"filename AS parquet_path "
                f"FROM read_parquet({lista}, union_by_name = true, filename = true)"
            )
            for parquet in self.parquets:  # cópia "antiga" (mesmo arquivo, outra máquina)
                con.execute(
                    "INSERT INTO datasus_cache (CID, NOME, AA, parquet_year, parquet_path) "
                    "SELECT CID, NOME, AA, parquet_year, ? || '/' || ? FROM datasus_cache "
                    "WHERE parquet_path = ?",
                    [CAMINHO_ANTIGO, parquet.name, str(parquet)],
                )
            con.execute(
                "CREATE TABLE file_manifest (source VARCHAR PRIMARY KEY, size BIGINT, "
                "modified DOUBLE, cached VARCHAR, in_db INTEGER DEFAULT 0, invalid INTEGER DEFAULT 0)"
            )
            for parquet in self.parquets:
                stat = parquet.stat()
                rel = str(parquet.relative_to(self.group_dir))
                con.execute(
                    "INSERT INTO file_manifest VALUES (?, ?, ?, ?, 1, 0)",
                    [rel, stat.st_size, stat.st_mtime, str(parquet)],
                )
                con.execute(
                    "INSERT INTO file_manifest VALUES (?, ?, ?, ?, 1, 0)",
                    [parquet.name, stat.st_size, stat.st_mtime, f"{CAMINHO_ANTIGO}/{parquet.name}"],
                )
        finally:
            con.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def test_diagnostico_detecta_duplicacao(self) -> None:
        info = diagnosticar(self.cache, GRUPO)
        self.assertEqual(info["razao"], 2.0)
        self.assertEqual(info["tabela"]["linhas"], self.total_linhas * 2)
        self.assertEqual(info["parquet_linhas"], self.total_linhas)
        self.assertEqual(info["manifesto"]["total"], len(self.parquets) * 2)
        self.assertEqual(info["manifesto"]["orfaos"], len(self.parquets))

    def test_deduplicar_mantem_apenas_o_cache_atual(self) -> None:
        con = conectar(self.db)
        try:
            resultado = deduplicar_tabela(con, self.cache)
            linhas = con.execute("SELECT COUNT(*) FROM datasus_cache").fetchone()[0]
            distintos = con.execute(
                "SELECT COUNT(DISTINCT parquet_path) FROM datasus_cache"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(resultado["removidas"], self.total_linhas)
        self.assertEqual(linhas, self.total_linhas)
        self.assertEqual(distintos, len(self.parquets))

    def test_deduplicar_aborta_quando_removeria_tudo(self) -> None:
        con = conectar(self.db)
        try:
            con.execute(
                "DELETE FROM datasus_cache WHERE parquet_path LIKE ?", [str(self.cache) + "%"]
            )
            with self.assertRaises(RuntimeError):
                deduplicar_tabela(con, self.cache)
            self.assertGreater(
                con.execute("SELECT COUNT(*) FROM datasus_cache").fetchone()[0], 0
            )
        finally:
            con.close()

    def test_limpar_manifesto_remove_orfaos(self) -> None:
        con = conectar(self.db)
        try:
            orfaos = manifesto_orfaos(con)
            removidas = limpar_manifesto(con, orfaos)
            total = con.execute("SELECT COUNT(*) FROM file_manifest").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(len(orfaos), len(self.parquets))
        self.assertEqual(removidas, len(self.parquets))
        self.assertEqual(total, len(self.parquets))

    def test_reparar_end_to_end_preserva_parquets(self) -> None:
        resultado = reparar(self.cache, GRUPO)
        self.assertNotIn("erro", resultado)
        depois = resultado["depois"]
        self.assertEqual(depois["razao"], 1.0)
        self.assertEqual(depois["manifesto"]["total"], len(self.parquets))
        self.assertEqual(depois["manifesto"]["orfaos"], 0)
        self.assertEqual(depois["tabela"]["linhas"], self.total_linhas)
        self.assertEqual(len(parquets_do_grupo(self.cache, GRUPO)), len(self.parquets))

    def test_reparar_por_ano_repara_as_fatias(self) -> None:
        resultado = reparar_por_ano(self.cache, GRUPO)
        self.assertNotIn("erro", resultado)
        self.assertEqual(resultado["depois"]["razao"], 1.0)
        self.assertEqual(resultado["depois"]["tabela"]["linhas"], self.total_linhas)
        self.assertEqual(
            [item["removidas"] for item in resultado["por_ano"]],
            [LINHAS_POR_ARQUIVO] * len(self.parquets),
        )

    def test_reescrita_preserva_linhas_e_manifesto(self) -> None:
        reparar(self.cache, GRUPO)
        resultado = compactar_arquivo(self.cache, GRUPO)
        self.assertNotIn("erro", resultado)
        self.assertEqual(resultado["tabela"]["linhas"], self.total_linhas)
        self.assertEqual(resultado["manifesto"]["total"], len(self.parquets))
        self.assertTrue(self.db.is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
