"""P2 (2.2) — Análise restritiva de FALSO POSITIVO na descoberta de CID.

O risco de "retornar todos" é o **over-matching**: "hepatite" casar com
"hepatite C" (etiologia distinta) ou "câncer de pele" casar com neoplasias de
OUTRAS topografias (C00/C50). Estes testes garantem que a âncora por ALIAS
CURADO restringe as famílias corretas, sem vazar para doenças vizinhas.

Usa o MESMO catálogo fake e a injeção do ``CidMap`` de
``test_cid_discovery_full`` (partilha via import do módulo).

Rodar:
    python -m pytest test/test_cid_false_positive.py -q
    python test/test_cid_false_positive.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(TEST_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db.cid_map as cid_map_mod  # noqa: E402
from _cid_fake_catalogo import catalogo_map  # noqa: E402
import db.cid_map  # noqa: E402


@pytest.fixture()
def svc(monkeypatch):
    monkeypatch.setattr(cid_map_mod, "_CID_MAP_SINGLETON", catalogo_map())

    from unittest.mock import MagicMock
    from api.llm_service_modular import LLMQueryService

    s = LLMQueryService.__new__(LLMQueryService)
    s.settings = MagicMock()
    s.settings.cid_discovery_max = 200
    s.cache = MagicMock()
    s._cid_prefixes_cache = {
        "r1": ["A90", "A91", "B05", "A15", "A16", "A17", "A18", "A19",
               "B15", "B16", "B17", "B18", "B19", "C43", "C44", "C00", "C50"],
    }
    return s


class TestAntiFalsoPositivo:
    def test_hepatite_c_nao_inclui_outras_hepatites(self, svc):
        codes = svc._discover_cid_full("casos de hepatite c", "r1")
        fams = {c[:3] for c in codes}
        assert fams <= {"B17"}, f"vazou etiologia: {fams - {'B17'}}"
        assert "B15" not in fams and "B16" not in fams and "B18" not in fams

    def test_hepatite_generica_inclui_todas_familias(self, svc):
        # Sem especificar letra, o genérico cobre B15..B19 (NÃO é over-match:
        # é a família do alias genérico "hepatite").
        codes = svc._discover_cid_full("casos de hepatite", "r1")
        fams = {c[:3] for c in codes}
        assert {"B15", "B16", "B17", "B18", "B19"} <= fams

    def test_cancer_de_pele_nao_vaza_outras_topografias(self, svc):
        codes = svc._discover_cid_full("obitos por cancer de pele", "r1")
        fams = {c[:3] for c in codes}
        assert fams <= {"C43", "C44"}, f"topografia vazada: {fams - {'C43', 'C44'}}"
        assert "C50" not in fams and "C00" not in fams

    def test_cancer_generico_e_amplo(self, svc):
        # "cancer" sem topografia -> família ampla (C00/C97 do alias genérico).
        codes = svc._discover_cid_full("casos de cancer", "r1")
        fams = {c[:3] for c in codes}
        assert "C00" in fams or "C50" in fams  # amplo, inclui topografias

    def test_tuberculose_nao_inclui_outras_bacterianas(self, svc):
        codes = svc._discover_cid_full("casos de tuberculose", "r1")
        fams = {c[:3] for c in codes}
        assert fams <= {"A15", "A16", "A17", "A18", "A19"}

    def test_identificacao_de_alias_ancora_especifica(self):
        cm = catalogo_map()
        # "hepatite c" -> somente B17 (não mistura A/B).
        assert cm.identificacao_de_alias("casos de hepatite c") == ["B17"]
        # "cancer de pele" -> C43, C44 (não C00/C97).
        assert set(cm.identificacao_de_alias("cancer de pele")) == {"C43", "C44"}
        # Genérico "hepatite" -> família completa.
        assert set(cm.identificacao_de_alias("hepatite")) == {
            "B15", "B16", "B17", "B18", "B19"
        }

    def test_grounding_usa_familia_completa(self, svc):
        # _resolve_cid_grounding deve listar TODAS as categorias da família.
        bloco = svc._resolve_cid_grounding("casos de tuberculose", "r1")
        for fam in ("A15", "A16", "A17", "A18", "A19"):
            assert fam in bloco, f"faltou {fam} no grounding"

    def test_cid_map_prefixes_para_familia_do_alias(self, svc):
        # Com alias curado, a família de validação é a INTEIRA (não top-3).
        fams = svc._cid_map_prefixes_para("casos de tuberculose")
        assert {"A15", "A16", "A17", "A18", "A19"} <= fams

    def test_validacao_semantica_rejeita_fora_da_familia(self, svc):
        # SQL com A67 (pinta) para pergunta de sarampo -> inválido (família B05).
        sql = (
            'SELECT 1 FROM datasus_query_cache '
            'WHERE UPPER(TRIM(CAST("CIDPRI" AS VARCHAR))) LIKE \'A67%\''
        )
        invalidos = svc._validate_cid_filters(sql, "r1", question="casos de sarampo em 2024")
        assert invalidos == ["A67"]


class TestMultiplasDoencas:
    """Perguntas com MAIS DE UMA doença nomeada: todas as famílias entram.

    Regressão: ``identificacao_de_alias`` escolhia apenas a âncora mais LONGA,
    descartando as demais doenças citadas (ex.: "dengue e sarampo" → só B05).
    Agora TODAS as âncoras específicas independentes são coletadas, SEM
    reintroduzir o over-matching de uma única doença específica.
    """

    def test_identificacao_de_alias_duas_doencas_distintas(self):
        cm = catalogo_map()
        fams = set(cm.identificacao_de_alias("casos de dengue e sarampo em 2024"))
        assert {"A90", "A91", "B05"} <= fams, f"faltou doença: {fams}"

    def test_identificacao_de_alias_tuberculose_e_aids(self):
        cm = catalogo_map()
        fams = set(cm.identificacao_de_alias("obitos por tuberculose ou aids"))
        assert {"A15", "A16", "A17", "A18", "A19"} <= fams
        assert {"B20", "B21", "B22", "B23", "B24"} <= fams

    def test_especifica_ainda_engole_generica(self):
        # Anti-falso-positivo preservado: "hepatite c" (específica) engole o
        # genérico "hepatite" -> só B17.
        cm = catalogo_map()
        assert cm.identificacao_de_alias("casos de hepatite c") == ["B17"]

    def test_descoberta_completa_duas_doencas(self, svc):
        codes = svc._discover_cid_full("casos de dengue e sarampo em 2024", "r1")
        fams = {c[:3] for c in codes}
        assert {"A90", "A91", "B05"} <= fams, f"faltou doença: {fams}"

    def test_familia_de_validacao_inclui_ambas(self, svc):
        # A família de validação semântica deve conter as duas doenças.
        fams = svc._cid_map_prefixes_para("casos de dengue e sarampo")
        assert {"A90", "A91", "B05"} <= fams


def main() -> int:
    import unittest

    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
