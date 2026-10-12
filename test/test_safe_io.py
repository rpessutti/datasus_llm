"""Testes de ``core.safe_io``: backup, escrita atômica + fsync e restore rápido.

Garante que:
- ``backup_file`` produz cópia idêntica (mesmo SHA256) e remove nada do original.
- ``atomic_write_text`` não deixa ``.tmp`` para trás e grava o conteúdo exato.
- ``verify_integrity`` detecta divergência após edição.
- ``restore_file`` recupera o conteúdo original e a integridade volta a bater.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import safe_io  # noqa: E402


class TestSafeIO(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.target = self.dir / "arquivo.py"
        self.original = "linha 1\nlinha 2\nvalor = 42\n"
        safe_io.atomic_write_text(self.target, self.original)

    def test_backup_identical_to_original(self) -> None:
        bak = safe_io.backup_file(self.target)
        self.assertIsNotNone(bak)
        self.assertTrue(Path(bak).is_file())
        self.assertTrue(safe_io.verify_integrity(self.target, bak))
        # Conteúdo idêntico byte a byte.
        self.assertEqual(self.target.read_text(encoding="utf-8"),
                         Path(bak).read_text(encoding="utf-8"))

    def test_backup_missing_returns_none(self) -> None:
        self.assertIsNone(safe_io.backup_file(self.dir / "nao_existe.py"))

    def test_atomic_write_no_tmp_left(self) -> None:
        safe_io.atomic_write_text(self.target, "novo conteudo\n")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "novo conteudo\n")
        self.assertFalse((self.dir / "arquivo.py.tmp").exists())

    def test_verify_detects_divergence_and_restore_recovers(self) -> None:
        bak = safe_io.backup_file(self.target)
        # Edita o arquivo (simula ajuste).
        safe_io.atomic_write_text(self.target, "conteudo corrompido\n")
        # Divergência detectada.
        self.assertFalse(safe_io.verify_integrity(self.target, bak))
        self.assertTrue(safe_io.text_differs(self.target, bak))
        # Restore rápido recupera o original.
        self.assertTrue(safe_io.restore_file(self.target, backup=bak))
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.original)
        self.assertTrue(safe_io.verify_integrity(self.target, bak))

    def test_latest_backup(self) -> None:
        bak1 = safe_io.backup_file(self.target)
        # Garante mtime distinto.
        os.utime(bak1, (1, 1))
        bak2 = safe_io.backup_file(self.target)
        latest = safe_io.latest_backup(self.target)
        self.assertEqual(Path(latest), Path(bak2))

    def test_backup_files_multi(self) -> None:
        f2 = self.dir / "outro.py"
        safe_io.atomic_write_text(f2, "x = 1\n")
        dest = self.dir / "_bk"
        out = safe_io.backup_files([self.target, f2], dest)
        self.assertEqual(len(out), 2)
        for src, bak in out.items():
            self.assertTrue(Path(bak).is_file())
            self.assertTrue(safe_io.verify_integrity(src, bak))


if __name__ == "__main__":
    unittest.main()
