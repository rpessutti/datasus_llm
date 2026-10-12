"""Testes da FASE P0-B: priorização lexical determinística de colunas/grupos.

Garante que:
- ``rank_columns`` puxa para frente a coluna que casa com o conceito/token da
  pergunta (sexo, município, idade, valor...) e é ESTÁVEL (mantém a ordem
  original nos empates / quando não há sinal);
- ``rank_groups`` reordena grupos por relevância sem REMOVER nenhum;
- ``ColumnDictionary.labeled_columns(rank_question=...)`` respeita o ranking
  ANTES do corte de ``limit_per_group`` (P0-B);
- ``generate.py`` reordena grupos/colunas quando ``lexical_rank_enabled``.

Sem dependências externas (nem DuckDB): usa um dicionário só com colunas.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.lexical_rank import (  # noqa: E402
    _concept_tokens_for_question,
    _has_signal,
    _tokenize,
    _variantes,
    rank_columns,
    rank_groups,
    score_column,
)
from db.column_dictionary import ColumnDictionary  # noqa: E402


COLS = ["AP_CIDPRI", "AP_SEXO", "AP_MUNICIP", "AP_IDADE", "AP_VALOR", "AP_DTINTER"]


class TestTokenizacao(unittest.TestCase):
    def test_tokenize_remove_stopwords(self):
        self.assertEqual(_tokenize("Quantos casos de dengue por sexo?"), ["dengue"])

    def test_variantes_plural(self):
        self.assertIn("municipio", _variantes("municipios"))
        self.assertIn("espinal", _variantes("espinais"))

    def test_conceito_matchado_mesmo_com_stopword(self):
        # "sexo" é stopword de cid_map, mas ainda deve casar como CONCEITO.
        self.assertIn("sexo", _concept_tokens_for_question("casos por sexo"))
        self.assertIn("municipio", _concept_tokens_for_question("casos por municipio"))

    def test_has_signal(self):
        self.assertTrue(_has_signal("casos por sexo"))   # conceito
        self.assertTrue(_has_signal("casos de dengue"))  # token de conteúdo
        self.assertFalse(_has_signal("quantos casos"))   # só stopwords


class TestRankColumns(unittest.TestCase):
    def test_sexo_puxa_coluna_de_sexo(self):
        ranked = rank_columns("quantos casos por sexo?", COLS)
        self.assertEqual(ranked[0], "AP_SEXO")

    def test_municipio_puxa_coluna_de_municipio(self):
        ranked = rank_columns("casos por municipio", COLS)
        self.assertEqual(ranked[0], "AP_MUNICIP")

    def test_idade_e_valor(self):
        self.assertEqual(rank_columns("casos por idade", COLS)[0], "AP_IDADE")
        self.assertEqual(rank_columns("valor total", COLS)[0], "AP_VALOR")

    def test_token_de_conteudo_no_nome_da_coluna(self):
        ranked = rank_columns("qual o valor da internacao", COLS)
        self.assertEqual(ranked[0], "AP_VALOR")

    def test_estavel_sem_sinal(self):
        # Sem sinal -> ordem ORIGINAL preservada.
        self.assertEqual(rank_columns("quantos casos", COLS), COLS)

    def test_estavel_em_empate(self):
        # Duas colunas que casam igual mantêm a ordem relativa original.
        cols = ["AP_SEXO_A", "AP_SEXO_B", "AP_OUTRA"]
        ranked = rank_columns("casos por sexo", cols)
        self.assertEqual(ranked[:2], ["AP_SEXO_A", "AP_SEXO_B"])

    def test_comentario_influencia(self):
        comments = {"AP_ESPECIAL": "Municipio de residencia do paciente"}
        ranked = rank_columns(
            "casos por municipio",
            ["AP_ESPECIAL", "AP_OUTRA"],
            comment_for_column=comments.get,
        )
        self.assertEqual(ranked[0], "AP_ESPECIAL")

    def test_score_column_conceito(self):
        self.assertGreater(score_column("AP_SEXO", "casos por sexo"),
                           score_column("AP_VALOR", "casos por sexo"))


class TestRankGroups(unittest.TestCase):
    def _dict(self):
        return ColumnDictionary.from_grupo_columns(
            {
                "pa": [{"column": "AP_SEXO", "type": "VARCHAR"},
                       {"column": "AP_VALOR", "type": "DOUBLE"}],
                "rd": [{"column": "RD_SEXO", "type": "VARCHAR"},
                       {"column": "RD_MUNICIP", "type": "VARCHAR"}],
                "sp": [{"column": "SP_VALOR", "type": "DOUBLE"}],
            }
        )

    def test_reordena_sem_remover(self):
        cd = self._dict()
        grupos = ["pa", "rd", "sp"]
        ranked = rank_groups("casos por municipio", grupos, cd)
        self.assertEqual(sorted(ranked), sorted(grupos), "não pode remover grupos")
        # 'rd' tem RD_MUNICIP -> deve subir.
        self.assertEqual(ranked[0], "rd")

    def test_preserva_ordem_sem_sinal(self):
        cd = self._dict()
        grupos = ["pa", "rd", "sp"]
        self.assertEqual(rank_groups("quantos casos", grupos, cd), grupos)

    def test_preserva_ordem_em_todos_zero(self):
        cd = self._dict()
        grupos = ["pa", "rd", "sp"]
        # Termo que não casa com nenhuma coluna -> mantém ordem original.
        self.assertEqual(rank_groups("xyzabc", grupos, cd), grupos)


class TestLabeledColumnsRanking(unittest.TestCase):
    def _dict(self):
        return ColumnDictionary.from_grupo_columns(
            {"pa": [{"column": c, "type": ""} for c in COLS]}
        )

    def test_rank_question_reordena_antes_do_corte(self):
        cd = self._dict()
        # Sem ranking: as 2 primeiras são AP_CIDPRI, AP_SEXO.
        sem = cd.labeled_columns(["pa"], limit_per_group=2)
        self.assertEqual(sem, ['"AP_CIDPRI" [pa]', '"AP_SEXO" [pa]'])
        # Com ranking p/ "valor": AP_VALOR deve entrar no corte de 2.
        com = cd.labeled_columns(["pa"], limit_per_group=2, rank_question="valor total")
        self.assertIn('"AP_VALOR" [pa]', com)

    def test_rank_question_none_mantem_comportamento(self):
        cd = self._dict()
        self.assertEqual(
            cd.labeled_columns(["pa"], limit_per_group=3),
            cd.labeled_columns(["pa"], limit_per_group=3, rank_question=None),
        )


class TestGenerateUsesLexicalRank(unittest.TestCase):
    """P0-B (generate.py): grupos/colunas reordenados quando a flag está ligada."""

    def _build_service(self, lexical_enabled: bool):
        from unittest.mock import MagicMock
        from api.llm_service_modular import LLMQueryService

        svc = LLMQueryService.__new__(LLMQueryService)
        calls: list[dict] = []

        dictionary = ColumnDictionary.from_grupo_columns(
            {
                "pa": [{"column": c, "type": ""} for c in COLS],
                "rd": [{"column": c.replace("AP_", "RD_"), "type": ""} for c in COLS],
            }
        )

        real_hint = dictionary.labeled_columns_sql_hint

        def _spy_hint(*args, **kwargs):
            calls.append({"args": args, "kwargs": dict(kwargs)})
            return real_hint(*args, **kwargs)

        dictionary.labeled_columns_sql_hint = _spy_hint  # type: ignore[assignment]

        fake_cache = MagicMock()
        fake_cache.get_column_dictionary.return_value = dictionary
        fake_cache._summary_tables.return_value = []
        fake_cache.question_wants_monthly_grouping.return_value = False

        settings = MagicMock()
        settings.schema_comments_enabled = False
        settings.schema_comment_max_chars = 90
        settings.lexical_rank_enabled = lexical_enabled
        settings.llm_sql_temperature = 0.0
        settings.llm_sql_max_tokens = 16
        settings.llm_timeout = 5
        settings.llm_sql_timeout = 5

        def _create(**kwargs):
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "SELECT COUNT(*) AS n FROM datasus_query_cache"
            return resp

        client = MagicMock()
        client.chat.completions.create.side_effect = _create

        svc.cache = fake_cache
        svc.settings = settings
        svc.client = client
        svc.model = "test"
        svc._first_attempt_ok = True
        return svc, calls

    def test_rank_question_passado_quando_ligado(self):
        from unittest.mock import patch

        svc, calls = self._build_service(lexical_enabled=True)
        with patch.object(svc, "_determine_relevant_grupos", return_value=["pa", "rd"]):
            svc._generate_sql("casos por municipio", "req-1", {("2025", "01")}, limite=100)
        self.assertTrue(calls)
        self.assertEqual(calls[0]["kwargs"].get("rank_question"), "casos por municipio")

    def test_rank_question_ausente_quando_desligado(self):
        from unittest.mock import patch

        svc, calls = self._build_service(lexical_enabled=False)
        with patch.object(svc, "_determine_relevant_grupos", return_value=["pa", "rd"]):
            svc._generate_sql("casos por municipio", "req-2", {("2025", "01")}, limite=100)
        self.assertTrue(calls)
        self.assertIsNone(calls[0]["kwargs"].get("rank_question"))


if __name__ == "__main__":
    unittest.main()
