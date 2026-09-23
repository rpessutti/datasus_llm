"""Teste do suporte a CID EXPLÍCITO na pergunta (precedência absoluta).

Foco especial em LISTAS de CID (separadas por vírgula e/ou ponto e vírgula),
SEMPRE acompanhadas de um PERÍODO (ano único ou conjunto de anos).

Cobre as decisões:
  - A1: aceitar SOMENTE os CIDs informados (igualdade exata de conjuntos).
  - B : CID explícito mantido no escopo da pergunta (período incluso).
  - C : separadores de lista aceitos = vírgula e ponto e vírgula.
  - D : avisar no log, mas PRESERVAR o CID do usuário (nunca reescrever).

Não requer LLM nem cache DuckDB: valida os helpers e a validação com uma
instância mínima da classe (via ``__new__``), injetando apenas o necessário.

Rodar:
    python test/test_cid_explicito.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.llm_service_modular import LLMQueryService


def _svc() -> "LLMQueryService":
    """Instância mínima para testar métodos que não dependem de cache/client."""
    return LLMQueryService.__new__(LLMQueryService)


# ======================================================================
# FASE 5.1 — Extração de CID explícito (unitário e LISTAS) + PERÍODO
# ======================================================================
# Toda pergunta inclui um PERÍODO (ano único ou conjunto de anos), conforme
# requisito. (pergunta, cids_esperados)
CASOS_EXTRACAO = [
    # --- CID único + ano único ---
    ("casos de A90 em 2024", ["A90"]),
    ("quantos casos de CID B05 em 2023", ["B05"]),
    ("CID: G12.1 em 2022", ["G121"]),
    ("compare A15.0 em 2024", ["A150"]),
    # --- LISTAS com vírgula (Decisão C) + ano ---
    ("A90, B05 em 2024", ["A90", "B05"]),
    ("casos de A90, B05, C50 em 2024", ["A90", "B05", "C50"]),
    ("A90,B05 em 2023", ["A90", "B05"]),  # sem espaços
    ("A90, B05, C50, E11, I21 em 2024", ["A90", "B05", "C50", "E11", "I21"]),
    # --- LISTAS com ponto e vírgula (Decisão C) + ano ---
    ("compare A15.0; A15,1 em 2024", ["A150", "A151"]),
    ("A90; B05; C50 em 2022", ["A90", "B05", "C50"]),
    # --- separadores MISTOS (vírgula + ponto e vírgula) + CONJUNTO de anos ---
    ("A90, B05; C50, E11 em 2023 e 2024", ["A90", "B05", "C50", "E11"]),
    ("A15.0, A15.1, A150 em 2024", ["A150", "A151"]),  # dedup A15.0 == A150
    # --- dedup em listas + ano ---
    ("A90, A90, B05 em 2024", ["A90", "B05"]),
    ("A90, A90; A90 em 2024", ["A90"]),
    # --- NÃO é CID (sem falso positivo), mas COM período ---
    ("dengue em 2024", []),
    ("total de atendimentos por mes em 2023", []),
    ("B2B e COVID19 em 2024", []),
]


def test_extracao() -> int:
    svc = _svc()
    falhas = 0
    print("-- extração (CID único e LISTAS) com período --")
    for pergunta, cids_esperado in CASOS_EXTRACAO:
        obtido = svc._extract_explicit_cid(pergunta)
        ok_cid = obtido == cids_esperado
        # Toda pergunta do teste DEVE conter período (ano de 4 dígitos).
        anos = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", pergunta)))
        ok_ano = len(anos) >= 1
        if not (ok_cid and ok_ano):
            falhas += 1
        print(
            f"[{'OK ' if ok_cid and ok_ano else 'FALHA'}] {pergunta!r} -> "
            f"cids={obtido} anos={anos} (esperado cids={cids_esperado})"
        )
    return falhas


def test_is_explicit() -> int:
    svc = _svc()
    falhas = 0
    print("-- detecção booleana (com período) --")
    casos = [
        ("A90 em 2024", True),
        ("dengue em 2024", False),
        ("CID B05 em 2023", True),
        ("A90, B05 em 2024", True),
        ("A90; B05; C50 em 2022", True),
        ("quantos atendimentos em 2024", False),
    ]
    for pergunta, esperado in casos:
        obtido = svc._is_explicit_cid_question(pergunta)
        ok = obtido == esperado
        if not ok:
            falhas += 1
        print(f"[{'OK ' if ok else 'FALHA'}] _is_explicit_cid_question({pergunta!r}) = {obtido}")
    return falhas


# ======================================================================
# FASE 5.2 — Validação A1 (igualdade exata de conjuntos) com LISTAS + PERÍODO
# ======================================================================
def _sql_lista(*cids: str, ano: str | None = None) -> str:
    """Monta um WHERE com um LIKE por CID (formato típico do LLM) + ano opcional."""
    termos = " OR ".join(f'"C" LIKE \'{c}%\' ' for c in cids)
    sql = f"SELECT * FROM datasus_query_cache WHERE {termos}".strip()
    if ano:
        sql += f" AND atend_ano = {ano}"
    return sql


def test_validacao_a1() -> int:
    svc = _svc()
    falhas = 0
    print("-- validação A1 (igualdade exata, com listas + período) --")

    # (sql, pergunta_com_periodo, esperado_invalidos)
    casos = [
        # ===== CID único + ano =====
        (_sql_lista("A90", ano="2024"), "casos de A90 em 2024", []),
        (_sql_lista("A150", ano="2022"), "A15.0 em 2022", []),          # subcategoria
        # SQL usou C50 em vez de A90 -> diferença simétrica = {A90 (faltando), C50 (a mais)}
        (_sql_lista("C50", ano="2024"), "casos de A90 em 2024", ["A90", "C50"]),

        # ===== LISTA de 2 — exata + ano =====
        (_sql_lista("A90", "B05", ano="2024"), "A90, B05 em 2024", []),
        (_sql_lista("A150", "A151", ano="2024"), "A15.0; A15,1 em 2024", []),

        # ===== LISTA de 2 — a mais + ano =====
        (_sql_lista("A90", "B05", "C50", ano="2024"), "A90, B05 em 2024", ["C50"]),
        (_sql_lista("A90", "B05", "C50", "E11", ano="2024"), "A90, B05 em 2024", ["C50", "E11"]),

        # ===== LISTA de 2 — a menos + ano =====
        (_sql_lista("A90", ano="2024"), "A90, B05 em 2024", ["B05"]),
        (_sql_lista("B05", ano="2024"), "A90, B05, C50 em 2024", ["A90", "C50"]),

        # ===== LISTA de 2 — trocou por outro + ano =====
        # SQL usou A67,B20 em vez de A90,B05 -> diferença simétrica = todos os 4.
        (_sql_lista("A67", "B20", ano="2024"), "A90, B05 em 2024", ["A67", "A90", "B05", "B20"]),

        # ===== LISTA de 3 — exata + ano =====
        (_sql_lista("A90", "B05", "C50", ano="2024"), "A90, B05, C50 em 2024", []),

        # ===== LISTA de 5 — exata (separadores mistos) + CONJUNTO de anos =====
        (
            _sql_lista("A90", "B05", "C50", "E11", "I21", ano="2023"),
            "A90, B05; C50, E11, I21 em 2023 e 2024",
            [],
        ),

        # ===== ordem diferente NÃO importa (conjunto) + ano =====
        (_sql_lista("B05", "A90", ano="2024"), "A90, B05 em 2024", []),

        # ===== normalização decimal + ano =====
        (_sql_lista("A150", ano="2024"), "A15.0 em 2024", []),
        (_sql_lista("A150", ano="2024"), "A15,0 em 2024", []),
    ]

    for sql, pergunta, esperado in casos:
        # Sanidade: a pergunta DEVE conter período.
        anos = re.findall(r"\b(?:19|20)\d{2}\b", pergunta)
        if not anos:
            falhas += 1
            print(f"[FALHA] pergunta sem período: {pergunta!r}")
            continue
        obtido = sorted(svc._validate_cid_filters(sql, "test-a1", question=pergunta))
        ok = obtido == sorted(esperado)
        if not ok:
            falhas += 1
        print(f"[{'OK ' if ok else 'FALHA'}] {pergunta!r} usados -> {obtido} (esperado {sorted(esperado)})")
    return falhas


# ======================================================================
# FASE 5.3 — Grounding autoritativo (LISTAS) e bypass determinístico
# ======================================================================
def test_grounding_bloco() -> int:
    svc = _svc()
    falhas = 0
    print("-- grounding autoritativo (listas) --")

    # pergunta com CID + período; o bloco só considera os CIDs.
    pergunta2 = "casos de A90, B05 em 2024"
    bloco2 = svc._explicit_cid_grounding_block(svc._extract_explicit_cid(pergunta2), "test-f5")
    if "A90" in bloco2 and "B05" in bloco2:
        print("[OK ] bloco (2 CIDs) contém A90 e B05")
    else:
        falhas += 1
        print("[FALHA] bloco (2 CIDs) não contém os CIDs informados")

    # bloco com LISTA de 3+ deve listar TODOS
    pergunta3 = "A90, B05, C50 em 2022"
    bloco3 = svc._explicit_cid_grounding_block(svc._extract_explicit_cid(pergunta3), "test-f5")
    if all(c in bloco3 for c in ("A90", "B05", "C50")):
        print("[OK ] bloco (3 CIDs) contém A90, B05 e C50")
    else:
        falhas += 1
        print(f"[FALHA] bloco (3 CIDs) incompleto: {bloco3[:200]}")

    if "NÃO descubra" in bloco2 or "NAO descubra" in bloco2:
        print("[OK ] bloco instrui a não descobrir/substituir")
    else:
        falhas += 1
        print("[FALHA] bloco não instrui a não descobrir/substituir")

    if "OR" in bloco3:
        print("[OK ] bloco (lista) menciona combinação por OR")
    else:
        falhas += 1
        print("[FALHA] bloco (lista) não menciona OR")

    return falhas


def test_bypass_deterministico() -> int:
    svc = _svc()
    falhas = 0
    print("-- bypass da reescrita determinística (com período) --")

    # com CID explícito, a reescrita determinística deve ser PULADA -> None.
    for question in ("casos de A90 em 2024", "A90, B05 em 2024", "A90; B05; C50 em 2022"):
        sql = "SELECT * FROM datasus_query_cache WHERE \"C\" LIKE 'A67%' AND atend_ano = 2024"
        res = svc._fix_cid_filters_deterministic(sql, ["A67"], question, "test-f5")
        if res is None:
            print(f"[OK ] pulado p/ {question!r} (retornou None)")
        else:
            falhas += 1
            print(f"[FALHA] {question!r}: esperava None, obteve {res!r}")

    return falhas


# ======================================================================
# FASE 5.4 — BUG: filtro CID como ``IN ('M05%', 'M06%', 'M10%')``
# ======================================================================
# Regressao do bug "qual a evolucao dos casos de CID M05,M06,M10 ... nao
# retorna dados": o LLM gerava ``... IN ('M05%', ...)`` (igualdade EXATA
# contra strings com o coringa dentro), que nao casa nenhum codigo real.
def test_normalize_cid_in_filters() -> int:
    svc = _svc()
    falhas = 0
    print("-- normalizacao do filtro CID em IN(...) --")

    # (sql_entrada, deve_conter, nao_deve_conter)
    casos = [
        (
            'SELECT atend_ano, COUNT(*) FROM datasus_query_cache WHERE '
            'UPPER(TRIM(CAST("CIDPRI" AS VARCHAR))) IN (\'M05%\', \'M06%\', \'M10%\') '
            'GROUP BY 1',
            ["LIKE 'M05%'", "LIKE 'M06%'", "LIKE 'M10%'"],
            [" IN ("],
        ),
        (
            'SELECT 1 FROM datasus_query_cache WHERE "CIDPRI" IN (\'A90%\', \'B05%\')',
            ["LIKE 'A90%'", "LIKE 'B05%'"],
            [" IN ("],
        ),
    ]
    for sql, deve_conter, nao_deve in casos:
        out = svc._normalize_cid_in_filters(sql, "test-in")
        ok = all(t in out for t in deve_conter) and all(t not in out for t in nao_deve)
        if not ok:
            falhas += 1
        print(f"[{'OK ' if ok else 'FALHA'}] {sql[:60]!r} -> {out[:120]!r}")

    # Nao deve mexer em LIKE ja correto nem em IN que nao e de CID.
    sql_ok = 'SELECT 1 FROM datasus_query_cache WHERE "C" LIKE \'M05%\''
    if svc._normalize_cid_in_filters(sql_ok, "t") == sql_ok:
        print("[OK ] LIKE ja correto permanece intacto")
    else:
        falhas += 1
        print("[FALHA] LIKE ja correto foi alterado")

    sql_nc = "SELECT 1 FROM datasus_query_cache WHERE col IN (\'foo\', \'bar\')"
    if svc._normalize_cid_in_filters(sql_nc, "t") == sql_nc:
        print("[OK ] IN nao-CID permanece intacto")
    else:
        falhas += 1
        print("[FALHA] IN nao-CID foi alterado")

    return falhas


def test_validacao_a1_in() -> int:
    svc = _svc()
    falhas = 0
    print("-- validacao A1 reconhece CID em IN(...) --")

    sql = (
        'SELECT atend_ano AS ano, COUNT(*) AS casos FROM datasus_query_cache WHERE '
        'UPPER(TRIM(CAST("CIDPRI" AS VARCHAR))) IN (\'M05%\', \'M06%\', \'M10%\') GROUP BY 1'
    )
    pergunta = "qual a evolucao dos casos de CID M05,M06,M10 nos ultimos 3 anos, agrupe por ano"
    invalidos = svc._validate_cid_filters(sql, "t-in", question=pergunta)
    if invalidos == []:
        print("[OK ] IN alinhado ao CID do usuario nao gera divergencia")
    else:
        falhas += 1
        print(f"[FALHA] divergencia inesperada: {invalidos}")

    # IN com CID a mais -> deve detectar a divergencia (A1).
    sql_extra = (
        'SELECT 1 FROM datasus_query_cache WHERE UPPER(TRIM(CAST("C" AS VARCHAR))) '
        'IN (\'M05%\', \'M06%\', \'C99%\')'
    )
    div = sorted(svc._validate_cid_filters(sql_extra, "t-in", question="casos de M05, M06"))
    if div == ["C99"]:
        print("[OK ] IN com CID extra detectado: ['C99']")
    else:
        falhas += 1
        print(f"[FALHA] esperava ['C99'], obteve {div}")

    return falhas


def main() -> int:
    print("=" * 64)
    print("FASE 5.1 - extracao de CID explicito (unico e listas) + periodo")
    print("=" * 64)
    f = test_extracao()
    f += test_is_explicit()
    print("=" * 64)
    print("FASE 5.2 - validacao A1 (igualdade exata, listas + periodo)")
    print("=" * 64)
    f += test_validacao_a1()
    print("=" * 64)
    print("FASE 5.3 - grounding (listas) / bypass")
    print("=" * 64)
    f += test_grounding_bloco()
    f += test_bypass_deterministico()
    print("=" * 64)
    print("FASE 5.4 - bug filtro CID em IN(...)")
    print("=" * 64)
    f += test_normalize_cid_in_filters()
    f += test_validacao_a1_in()
    print(f"\n=== {f} falha(s) ===")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
