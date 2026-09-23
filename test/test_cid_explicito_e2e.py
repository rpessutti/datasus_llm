"""Teste END-TO-END do suporte a CID EXPLÍCITO (com período), fluxo real.

Exercita o caminho COMPLETO: ``service.ask(pergunta)`` -> geração de SQL via
LLM -> validação/substituição de CID -> execução no DuckDB -> resposta.

Valida, com dados/LLM reais:
  1. Perguntas com CID explícito (único e LISTA) NÃO disparam a descoberta
     automática via LLM (``_discover_cid_via_llm`` é PULADA — precedência).
  2. O SQL final PRESERVA o(s) CID(s) informado(s) (regra A1).
  3. A pergunta inclui SEMPRE um PERÍODO (ano único ou conjunto de anos).
  4. O fluxo termina sem erro (retorna SQL e resultados, mesmo que vazios).

Se o LLM/cache não estiverem disponíveis, os casos são marcados como SKIP
(para não quebrar ambientes sem dependências). Rode com:

    python test/test_cid_explicito_e2e.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ======================================================================
# Casos: (pergunta_com_periodo, cids_esperados_no_sql, anos_esperados)
# ======================================================================
CASOS = [
    ("Quantos casos de A90 em 2024?", ["A90"], ["2024"]),
    ("Quantos casos de A90 e B05 em 2024 por sexo?", ["A90", "B05"], ["2024"]),
    ("Compare os casos de A90, B05 e C50 em 2022 e 2023", ["A90", "B05", "C50"], ["2022", "2023"]),
    ("Casos de A150 em 2022 e 2023 por ano", ["A150"], ["2022", "2023"]),
]


def _extrai_cids_do_sql(sql: str) -> set[str]:
    """Extrai os CIDs usados em cláusulas LIKE do SQL (normalizados, sem ponto)."""
    if not sql:
        return set()
    return {
        m.group(1).upper().replace(".", "")
        for m in re.finditer(r"LIKE\s+'([A-Za-z]\d{2}(?:\.?\d)?)%?'", sql, flags=re.IGNORECASE)
    }


def main() -> int:
    print("=" * 64)
    print("E2E: CID EXPLÍCITO (único e LISTA) + PERÍODO")
    print("=" * 64)

    try:
        from api.routes import cache, service
    except Exception as exc:
        print(f"SKIP: não foi possível importar api.routes ({type(exc).__name__}: {exc})")
        return 0

    if not getattr(service, "client", None):
        print("SKIP: LLM desabilitado (service.client is None). Habilite o Ollama para o e2e.")
        return 0

    # Verifica disponibilidade de dados considerando o modo GRUPOS (DuckDB), em
    # que cache.status()["files"] pode ser 0 mas os grupos têm dados reais.
    tem_dados = False
    try:
        if getattr(cache, "groups", None):
            tem_dados = len(cache.groups) > 0
        if not tem_dados and getattr(cache, "status", None):
            tem_dados = cache.status().get("files", 0) > 0
    except Exception:
        tem_dados = bool(getattr(cache, "groups", None))
    if not tem_dados:
        print("SKIP: cache sem dados (nem grupos DuckDB nem arquivos).")
        return 0

    # ---------- instrumentação: detectar chamadas à descoberta via LLM ----------
    chamadas_discovery: list[str] = []
    original_discovery = service._discover_cid_via_llm

    def _spy_discovery(question: str, request_id: str) -> str:
        chamadas_discovery.append(question)
        return original_discovery(question, request_id)

    service._discover_cid_via_llm = _spy_discovery  # type: ignore[assignment]

    falhas = 0
    try:
        for i, (pergunta, cids_esp, anos_esp) in enumerate(CASOS):
            rid = f"e2e-cid-{i}"
            print("-" * 64)
            print(f"pergunta: {pergunta!r}")
            print(f"  cids esperados no SQL: {cids_esp} | anos: {anos_esp}")

            # Sanidade: a pergunta deve conter período.
            anos_presentes = re.findall(r"\b(?:19|20)\d{2}\b", pergunta)
            if not anos_presentes:
                print("  [FALHA] pergunta sem período (ano)")
                falhas += 1
                continue

            antes = len(chamadas_discovery)
            try:
                resultado = service.ask(pergunta, rid)
            except Exception as exc:
                print(f"  [FALHA] ask() levantou: {type(exc).__name__}: {exc}")
                falhas += 1
                continue

            sql = resultado.get("sql") or ""
            source = resultado.get("source")
            rows = resultado.get("results", [])
            chamou_discovery = len(chamadas_discovery) > antes

            print(f"  source={source} rows={len(rows)}")
            print(f"  SQL: {sql[:240]}")

            # 1) descoberta automática NÃO deve ser chamada para CID explícito
            if chamou_discovery:
                print("  [FALHA] _discover_cid_via_llm FOI chamado (deveria ser pulado)")
                falhas += 1
            else:
                print("  [OK ] _discover_cid_via_llm PULADO (precedência do CID explícito)")

            # 2) o SQL deve preservar exatamente os CIDs informados (A1)
            cids_sql = _extrai_cids_do_sql(sql)
            faltando = {c for c in cids_esp if c not in cids_sql}
            extras = {c for c in cids_sql if c not in cids_esp}
            if faltando or extras:
                print(f"  [FALHA] CIDs no SQL={sorted(cids_sql)} faltando={sorted(faltando)} extras={sorted(extras)}")
                falhas += 1
            else:
                print(f"  [OK ] SQL preserva exatamente {cids_esp}")

            # 3) o fluxo terminou sem erro (tem source e sql)
            if not sql or not source:
                print("  [FALHA] fluxo não produziu SQL/source")
                falhas += 1
    finally:
        service._discover_cid_via_llm = original_discovery  # type: ignore[assignment]

    print("=" * 64)
    print(f"=== {falhas} falha(s) ===")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
