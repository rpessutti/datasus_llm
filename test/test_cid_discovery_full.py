"""P2 (2.1) — Descoberta COMPLETA de CID: ``_discover_cid_full`` sem truncar.

Objetivo: para uma doença citada, obter TODAS as (sub)categorias CID-10 que a
pertencem (categoria de 3 chars + subcategorias de 4 chars), comparando contra o
próprio catálogo ``cid10`` — sem depender de DuckDB nem de LLM.

Estratégia do teste: injetamos um ``CidMap`` com entradas "fake" via
``CidMap._parse_rows`` (o mesmo parser usado pelo loader real) e substituímos o
singleton ``db.cid_map._CID_MAP_SINGLETON``. Assim exercitamos o caminho REAL de
``_discover_cid_full`` (família por alias + termos + priorização + teto) sem
abrir o banco.

Rodar:
    python -m pytest test/test_cid_discovery_full.py -q
    python test/test_cid_discovery_full.py
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


@pytest.fixture()
def svc(monkeypatch):
    """Instância mínima do serviço com um CidMap fake e prefixos reais fake."""
    monkeypatch.setattr(cid_map_mod, "_CID_MAP_SINGLETON", catalogo_map())

    from unittest.mock import MagicMock
    from api.llm_service_modular import LLMQueryService

    s = LLMQueryService.__new__(LLMQueryService)
    s.settings = MagicMock()
    s.settings.cid_discovery_max = 200
    s.cache = MagicMock()
    # Prefixos "presentes nos dados" (para priorização; NÃO descarte).
    s._cid_prefixes_cache = {
        "r1": ["A90", "A91", "B05", "A15", "A16", "A17", "A18", "A19",
               "B15", "B16", "B17", "C43", "C44", "C50"],
    }
    return s


def _familia_de(codes: list[str]) -> set[str]:
    return {c[:3] for c in codes}


class TestDiscoveryFull:
    def test_dengue_retorna_familia_completa(self, svc):
        codes = svc._discover_cid_full("quantos casos de dengue em 2024", "r1")
        # Todas as (sub)categorias de A90/A91 do catálogo devem aparecer.
        esperado = {e.inicio for e in catalogo_map().familia_completa(["A90", "A91"])}
        assert esperado <= set(codes), f"faltando: {esperado - set(codes)}"

    def test_sarampo_inclui_subcategorias(self, svc):
        codes = svc._discover_cid_full("obitos por sarampo", "r1")
        assert "B05" in codes
        assert "B050" in codes and "B051" in codes

    def test_tuberculose_familia_a15_a19(self, svc):
        codes = svc._discover_cid_full("casos de tuberculose", "r1")
        assert {"A15", "A16", "A17", "A18", "A19"} <= set(codes)
        assert "A150" in codes

    def test_cancer_de_pele_ancora_c43_c44(self, svc):
        codes = svc._discover_cid_full("obitos por cancer de pele", "r1")
        assert {"C43", "C44"} <= _familia_de(codes) or {"C43", "C44"} <= set(codes)
        # NÃO deve trazer outras topografias.
        assert "C50" not in codes
        assert "C00" not in codes

    def test_sem_doenca_retorna_vazio(self, svc):
        assert svc._discover_cid_full("total de atendimentos por mes", "r1") == []

    def test_teto_de_sanidade(self, svc):
        # Com teto baixo, o resultado é truncado (mas nunca abaixo do teto).
        svc.settings.cid_discovery_max = 2
        codes = svc._discover_cid_full("casos de tuberculose", "r1")
        assert len(codes) == 2

    def test_ordenacao_categoria_antes_de_subcategoria(self, svc):
        codes = svc._discover_cid_full("casos de dengue", "r1")
        # Os presentes nos dados e as categorias (3 chars) vêm primeiro.
        cats = [c for c in codes if len(c) == 3]
        subs = [c for c in codes if len(c) == 4]
        if cats and subs:
            assert codes.index(cats[0]) < codes.index(subs[0])

    def test_bloco_prompt_menciona_like_categoria(self, svc):
        codes = svc._discover_cid_full("casos de sarampo", "r1")
        bloco = svc._cid_discovery_block(codes, "r1")
        assert "LIKE 'B05%'" in bloco
        assert "MESMA doença" in bloco


class TestIntersecaoDeTermos:
    """P2 (2.2): descoberta por termos deve EXIGIR a INTERSEÇÃO dos termos.

    Regressão (bug "esclerose múltipla" → 174 CIDs de doenças não relacionadas):
    a versão anterior aceitava a entrada que casasse com QUALQUER termo (união),
    trazendo aids/tuberculose/câncer. Agora a entrada precisa casar com TODOS os
    termos da doença.
    """

    def test_esclerose_multipla_so_g35(self, svc):
        codes = svc._discover_cid_full("casos de esclerose multipla", "r1")
        fams = {c[:3] for c in codes}
        assert fams == {"G35"}, f"over-matching por união de termos: {fams}"

    def test_esclerose_multipla_exclui_ruido(self, svc):
        codes = svc._discover_cid_full("casos de esclerose multipla", "r1")
        # E03 casa só 'esclerose'; G99 casa só 'multipla' — ambos devem sair.
        assert "E03" not in codes and "G99" not in codes

    def test_termo_curto_valido_gota(self, svc):
        # "gota" tem 4 chars; com min_len=4 ainda é descoberta (M10).
        codes = svc._discover_cid_full("casos de gota", "r1")
        assert {"M10", "M100"} <= set(codes), f"perdeu gota: {codes}"

    def test_entries_por_termos_intersecao(self):
        # Direto na API: dois termos que só coincidem em G35.
        cm = catalogo_map()
        entries = cm.entries_por_termos(("esclerose", "multipla"))
        inicios = {e.inicio for e in entries}
        assert inicios == {"G35"}, f"esperado só G35, veio {inicios}"


class TestVarianteAdjetivoSubstantivo:
    """P2: a pergunta usa o ADJETIVO e a descrição o SUBSTANTIVO.

    Regressão ("degeneração macular"): o termo "macular" (adjetivo) não casava
    "mácula" (substantivo) — a interseção zerava e o fallback devolvia as 13
    entradas que casavam só "degeneração" (termo COMUM), isto é, dezenas de
    degenerações de OUTROS órgãos. A correção tem duas partes:
      (1) variante derivada "macular"→"macula"; e
      (2) fallback por RARIDADE (IDF): o termo raro domina o comum.
    """

    def test_macular_casa_macula(self):
        cm = catalogo_map()
        entries = cm.entries_por_termos(("degeneracao", "macular"))
        inicios = {e.inicio for e in entries}
        assert "H353" in inicios, f"perdeu degeneração macular: {inicios}"
        # Ruído que casa SÓ 'degeneracao' (comum) NÃO deve entrar.
        assert "I515" not in inicios and "G311" not in inicios

    def test_descoberta_completa_degeneracao_macular(self, svc):
        codes = svc._discover_cid_full("casos de degeneracao macular", "r1")
        assert "H353" in codes
        # Não podem vazar degenerações de outros órgãos (termo comum).
        assert "I515" not in codes and "G311" not in codes

    def test_variante_direta_hepatica(self):
        # "hepatica" (adjetivo) -> radical "hepat" para casar descrições afins.
        from db.cid_map import CidMap
        vs = CidMap._variantes_termo("hepatica")
        assert "hepat" in vs or "hepatica" in vs


class TestSubstringPorPrefixo:
    """P2: o casamento por substring deve ser por PREFIXO (evita over-matching).

    Regressão ("cistite" → K80/K81 'colecistite'): a versão anterior aceitava
    QUALQUER substring ("cistite" dentro de "cole**cistite**"), trazendo a
    doença da VESÍCULA (colecistite) para a pergunta da BEXIGA (cistite).
    """

    def test_cistite_nao_casa_colecistite(self):
        cm = catalogo_map()
        entries = cm.entries_por_termos(("cistite",))
        inicios = {e.inicio for e in entries}
        assert "N30" in inicios and "N300" in inicios
        assert "K81" not in inicios and "K810" not in inicios, (
            f"'cistite' vazou p/ vesícula: {inicios}"
        )

    def test_prefixo_casa_plural(self):
        # 'cistite' deve casar 'cistites' (plural) por prefixo do segmento.
        from db.cid_map import CidMap
        assert CidMap._casa_termo({"cistites"}, "cistite")
        # Mas NÃO deve casar 'colecistite' (prefixo diferente).
        assert not CidMap._casa_termo({"colecistite"}, "cistite")


class TestAliasColoquialNovo:
    """Aliases curados adicionados: termos coloquiais que NÃO aparecem
    literalmente nas descrições oficiais.
    """

    def test_sepse_resolve_para_a40_a41(self):
        cm = catalogo_map()
        assert set(cm.identificacao_de_alias("casos de sepse")) == {"A40", "A41"}

    def test_descoberta_completa_sepse(self, svc):
        codes = svc._discover_cid_full("casos de sepse", "r1")
        fams = {c[:3] for c in codes}
        assert fams == {"A40", "A41"}, f"sepse -> {fams}"

    def test_alias_enfarte_pressao_alta(self):
        cm = catalogo_map()
        assert set(cm.identificacao_de_alias("casos de enfarte")) == {"I21", "I22", "I23"}
        assert "I10" in cm.identificacao_de_alias("casos de pressao alta")


class TestAliasSintomasColoquiais:
    """Aliases de SINTOMAS populares (códigos R) e sinônimos de doenças.

    Expressões coloquiais cujo termo NÃO aparece na descrição oficial (que usa
    o termo técnico): "falta de ar" → Dispneia (R060), "dor de cabeça" →
    Cefaleia (R51). Sem alias, a busca por termo casava por ARTEFATO palavras
    isoladas ("falta" → X530 "Falta de alimento"), trazendo código errado.
    """

    def test_falta_de_ar_nao_casa_falta_de_alimento(self):
        cm = catalogo_map()
        assert cm.identificacao_de_alias("casos de falta de ar") == ["R06"]
        # Direto nos termos: 'falta' isolado casa X530 (ruído) — por isso o
        # alias é necessário; a descoberta determinística usa o alias.
        codes = cm.codigos_para("casos de falta de ar")
        fams = {c[:3] for c in codes}
        assert "R06" in fams
        assert "X53" not in fams, f"'falta de ar' vazou p/ 'falta de alimento': {fams}"

    def test_dor_de_cabeca_cefaleia(self):
        cm = catalogo_map()
        assert cm.identificacao_de_alias("casos de dor de cabeca") == ["R51"]

    def test_sinonimos_populares(self):
        cm = catalogo_map()
        assert cm.identificacao_de_alias("casos de catapora") == ["B01"]
        assert "K80" in cm.identificacao_de_alias("casos de pedra na vesicula")
        assert "E11" in cm.identificacao_de_alias("casos de acucar no sangue")
        assert "B68" in cm.identificacao_de_alias("casos de verminose")

    def test_descoberta_falta_de_ar(self, svc):
        codes = svc._discover_cid_full("casos de falta de ar", "r1")
        fams = {c[:3] for c in codes}
        assert "R06" in fams
        assert "X53" not in fams

    def test_descoberta_catapora(self, svc):
        codes = svc._discover_cid_full("casos de catapora", "r1")
        assert {c[:3] for c in codes} == {"B01"}


class TestDiscoveryEstrategia:
    """Garante que a via determinística tem PRIORIDADE sobre o LLM."""

    def test_discover_via_llm_usa_deterministica_primeiro(self, svc, monkeypatch):
        from unittest.mock import MagicMock

        # Se o LLM fosse chamado, o mock registraria a chamada.
        fake_client = MagicMock()
        dynamic = MagicMock()
        fake_client.chat.completions.create.side_effect = dynamic
        svc.client = fake_client
        svc.model = "test"

        res = svc._discover_cid_via_llm("casos de dengue em 2024", "r1")
        assert "A90" in res
        # O LLM NÃO deve ter sido chamado (a via determinística resolveu).
        assert dynamic.call_count == 0

    def test_discover_via_llm_sem_doenca_retorna_vazio(self, svc):
        svc.client = None  # nem haveria LLM
        assert svc._discover_cid_via_llm("total por mes", "r1") == ""


class TestFallbackSemCid10:
    """Robustez: sem a tabela ``cid10`` carregada (ambiente sem o catálogo),
    a descoberta ainda usa as FAMÍLIAS do alias curado (categorias de 3 chars).
    """

    @pytest.fixture()
    def svc_sem_catalogo(self, monkeypatch):
        from db.cid_map import CidMap
        from unittest.mock import MagicMock
        from api.llm_service_modular import LLMQueryService

        # Simula o ambiente SEM a tabela cid10: ``_loaded=True`` com
        # ``_entries=[]`` impede que o ``_load()`` lazy carregue o catálogo
        # REAL do DuckDB (é exatamente o cenário "cid10 indisponível").
        vazio = CidMap()
        vazio._loaded = True
        vazio._entries = []
        vazio._by_norm_desc = {}
        monkeypatch.setattr(cid_map_mod, "_CID_MAP_SINGLETON", vazio)

        s = LLMQueryService.__new__(LLMQueryService)
        s.settings = MagicMock()
        s.settings.cid_discovery_max = 200
        s.cache = MagicMock()
        s._cid_prefixes_cache = {"r1": []}
        return s

    def test_tuberculose_usa_familias_do_alias(self, svc_sem_catalogo):
        codes = svc_sem_catalogo._discover_cid_full("casos de tuberculose", "r1")
        assert set(codes) == {"A15", "A16", "A17", "A18", "A19"}

    def test_sarampo_usa_familia_do_alias(self, svc_sem_catalogo):
        assert svc_sem_catalogo._discover_cid_full("casos de sarampo", "r1") == ["B05"]

    def test_hepatite_c_nao_vaza(self, svc_sem_catalogo):
        assert svc_sem_catalogo._discover_cid_full("casos de hepatite c", "r1") == ["B17"]


def main() -> int:
    import unittest

    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
