# -*- coding: utf-8 -*-
"""Testes da PARTE 5 (#1): qualificacao SQL sem regex arriscada.

O `_GroupCache._qualify_sql` reescrevia QUALQUER token `datasus_cache`/
`datasus`/etc. no SQL INTEIRO via regex com lookbehind/lookahead de aspas,
o que CORROMPIA literais cujo token nao estava adjacente a aspas — por exemplo
``LIKE '%datasus%'`` virava ``'%pa.datasus%'``.

A nova implementacao ancora a substituicao em clausulas FROM/JOIN, qualificando
APENAS a relacao (tabelas internas do grupo) e NUNCA tocando em literais,
comentarios ou relacoes ja qualificadas.

Cobre:
  * FROM/JOIN qualificados (<grupo>.<tabela>).
  * Literais preservados (inclusive com % antes/depois do token).
  * Relacoes ja qualificadas NAO duplicam o schema.
  * Relacoes externas (nao internas) NAO sao tocadas.
  * Delimitadores (aspas duplas / colchetes / crases) preservados.
  * Idempotencia (aplicar 2x = aplicar 1x).
"""

import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from db.group_cache import _GroupCache  # noqa: E402


class QualifySqlF5Test(unittest.TestCase):
    def setUp(self) -> None:
        # Nao abre conexao: _qualify_sql e puro texto.
        self.group = _GroupCache("pa", Path("."), Path("."), None)

    def _q(self, sql: str) -> str:
        return self.group._qualify_sql(sql)

    # ------------------------------------------------------------------ #
    # Qualificacao basica em FROM/JOIN
    # ------------------------------------------------------------------ #
    def test_qualifica_from(self) -> None:
        self.assertEqual(
            self._q("SELECT COUNT(*) FROM file_manifest"),
            "SELECT COUNT(*) FROM pa.file_manifest",
        )

    def test_qualifica_join(self) -> None:
        out = self._q(
            "SELECT * FROM datasus_cache d JOIN period_index p ON d.a = p.a"
        )
        self.assertIn("FROM pa.datasus_cache", out)
        self.assertIn("JOIN pa.period_index", out)

    def test_qualifica_todas_tabelas_internas(self) -> None:
        for tabela in (
            "file_manifest",
            "metadata",
            "period_index",
            "group_checkpoint",
            "datasus_cache",
            "datasus",
        ):
            self.assertEqual(
                self._q(f"SELECT * FROM {tabela}"),
                f"SELECT * FROM pa.{tabela}",
                msg=tabela,
            )

    # ------------------------------------------------------------------ #
    # CRITICO (#1): literais NAO sao reescritos
    # ------------------------------------------------------------------ #
    def test_nao_altera_literal_com_percent(self) -> None:
        """`LIKE '%datasus%'` NAO deve ser alterado (bug da versao anterior)."""
        sql = "SELECT * FROM datasus_cache WHERE x LIKE '%datasus%'"
        out = self._q(sql)
        self.assertIn("'%datasus%'", out)
        self.assertNotIn("pa.datasus", out.split("WHERE", 1)[1])
        self.assertEqual(
            out, "SELECT * FROM pa.datasus_cache WHERE x LIKE '%datasus%'"
        )

    def test_nao_altera_literal_exato(self) -> None:
        sql = "SELECT * FROM datasus WHERE nome = 'datasus_cache'"
        out = self._q(sql)
        self.assertIn("= 'datasus_cache'", out)
        self.assertEqual(out, "SELECT * FROM pa.datasus WHERE nome = 'datasus_cache'")

    def test_nao_altera_like_prefixo(self) -> None:
        sql = "SELECT * FROM datasus_cache WHERE cid LIKE 'datasus%'"
        out = self._q(sql)
        self.assertIn("LIKE 'datasus%'", out)

    def test_nao_altera_literal_em_join_condicional(self) -> None:
        sql = "SELECT * FROM datasus_cache WHERE cid LIKE '%datasus_cache%'"
        out = self._q(sql)
        self.assertIn("'%datasus_cache%'", out)

    # ------------------------------------------------------------------ #
    # Nao duplica schema em relacoes ja qualificadas
    # ------------------------------------------------------------------ #
    def test_nao_duplica_schema(self) -> None:
        sql = "SELECT * FROM pa.datasus_cache"
        self.assertEqual(self._q(sql), "SELECT * FROM pa.datasus_cache")

    def test_nao_duplica_schema_com_join(self) -> None:
        sql = "SELECT * FROM pa.datasus_cache d JOIN pa.period_index p ON d.a = p.a"
        self.assertEqual(self._q(sql), sql)

    # ------------------------------------------------------------------ #
    # Relacoes externas NAO sao tocadas
    # ------------------------------------------------------------------ #
    def test_relacao_externa_intacta(self) -> None:
        sql = "SELECT * FROM cid10 WHERE codigo = 'A90'"
        self.assertEqual(self._q(sql), sql)

    def test_relacao_qualificada_outra_intacta(self) -> None:
        sql = "SELECT * FROM outros.dados x JOIN datasus_cache d ON x.a = d.a"
        out = self._q(sql)
        self.assertIn("FROM outros.dados", out)
        self.assertIn("JOIN pa.datasus_cache", out)

    # ------------------------------------------------------------------ #
    # Delimitadores preservados
    # ------------------------------------------------------------------ #
    def test_preserva_aspas_duplas(self) -> None:
        out = self._q('SELECT * FROM "datasus_cache"')
        self.assertEqual(out, 'SELECT * FROM "pa.datasus_cache"')

    # ------------------------------------------------------------------ #
    # Idempotencia
    # ------------------------------------------------------------------ #
    def test_idempotente(self) -> None:
        sql = "SELECT * FROM datasus_cache WHERE x LIKE '%datasus%'"
        once = self._q(sql)
        twice = self._q(once)
        self.assertEqual(once, twice)

    # ------------------------------------------------------------------ #
    # Sem FROM/JOIN: nada a qualificar
    # ------------------------------------------------------------------ #
    def test_sem_from_nao_altera(self) -> None:
        sql = "SELECT 1 AS datasus_cache"
        self.assertEqual(self._q(sql), sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
