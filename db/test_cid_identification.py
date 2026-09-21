"""Teste RÁPIDO de identificação de CID-10 (CidMap) + aderência ao cache.

Valida dois níveis:
  1. SEMÂNTICO — o de-para doença -> CID contra o CID_10.CSV (CidMap).
  2. DADOS     — o CID resolvido realmente existe no cache DuckDB
                 (_known_cid_prefixes), evitando grounding para códigos ausentes.

Carrega o CID_10.CSV UMA vez e não faz varredura de 12k entries.

Rodar:
    python db/test_cid_identification.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.cid_map import get_cid_map


# (pergunta, prefixo CORRETO que deve aparecer, prefixo ERRADO que NÃO pode aparecer)
CASOS = [
    ("quantos casos de sarampo em 2024?", "B05", "A67"),
    ("casos de dengue", "A90", "B05"),
    ("internacoes por tuberculose", "A15", "A30"),
    ("covid", "U07", "B05"),
    ("cancer de mama", "C50", "C61"),
    ("diabetes", "E10", "E65"),
    ("infarto", "I21", "I10"),
    ("hepatite c", "B17", "B15"),
    ("varicela", "B01", "B05"),
    ("malaria", "B50", "B05"),
    ("hanseniase", "A30", "B05"),
]


def _bate(codigos: list[str], prefixo: str) -> bool:
    return any(c.startswith(prefixo) or prefixo.startswith(c) for c in codigos)


def test_semantico() -> int:
    """Nível 1: doença -> CID correto segundo o CID_10.CSV."""
    m = get_cid_map()
    if not m.loaded:
        print("SKIP: CID_10.CSV nao carregado")
        return 0

    print(f"entradas no CID_10.CSV: {len(m.entries)}\n")
    falhas = 0
    for pergunta, correto, errado in CASOS:
        # max_resultados maior: queremos ver a ORDEM de prioridade, não só o top-5.
        codigos = [c.upper() for c in m.codigos_para(pergunta, max_resultados=10)]
        ok_correto = _bate(codigos, correto)
        # O código CORRETO deve vir ANTES de qualquer código errado relevante:
        # o de-para específico tem prioridade sobre matches genéricos.
        pos_correto = next(
            (i for i, c in enumerate(codigos) if c.startswith(correto) or correto.startswith(c)),
            None,
        )
        pos_errado = next(
            (i for i, c in enumerate(codigos) if c.startswith(errado) or errado.startswith(c)),
            None,
        )
        ok_prioridade = pos_correto is not None and (pos_errado is None or pos_correto < pos_errado)
        status = "OK " if (ok_correto and ok_prioridade) else "FALHA"
        if status == "FALHA":
            falhas += 1
        print(f"[{status}] {pergunta!r}")
        print(f"        correto={correto}@{pos_correto}  errado={errado}@{pos_errado}")
        print(f"        obtido={codigos[:6]}\n")

    # Regressão do bug central: 'sarampo' NUNCA pode resolver para pinta (A67).
    sarampo = [c.upper() for c in m.codigos_para("sarampo")]
    if any(c.startswith("A67") for c in sarampo):
        print("[FALHA] sarampo resolveu para A67 (pinta)!")
        falhas += 1
    else:
        print("[OK ] sarampo NAO resolve para A67\n")
    return falhas


def test_cache() -> int:
    """Nível 2: o CID resolvido deve existir no cache DuckDB.

    Usa _known_cid_prefixes para confirmar que o grounding aponta para códigos
    PRESENTES nos dados — e não para códigos plausíveis porém ausentes.
    """
    try:
        from api.llm_service_modular import LLMQueryService
        from db.cache import DatasusCache
        from config.settings import Settings
    except Exception as exc:  # dependências pesadas ausentes
        print(f"SKIP nivel cache: {exc}")
        return 0

    try:
        cache = DatasusCache(Settings())
        svc = LLMQueryService.__new__(LLMQueryService)
        svc.cache = cache
        known = svc._known_cid_prefixes("test-cid")
    except Exception as exc:
        print(f"SKIP nivel cache (init): {type(exc).__name__}: {exc}")
        return 0

    if not known:
        print("SKIP nivel cache: _known_cid_prefixes retornou vazio")
        return 0

    print(f"prefixos de CID presentes no cache: {len(known)}")
    known_upper = {p.upper() for p in known}

    falhas = 0
    for cid in ("A90", "B05", "A15", "C50", "E11", "I21"):
        presente = any(k.startswith(cid) or cid.startswith(k) for k in known_upper)
        status = "OK " if presente else "AUSENTE"
        if not presente:
            falhas += 1
        print(f"[{status}] {cid} presente no cache")

    a67 = any(k.startswith("A67") for k in known_upper)
    print(f"[INFO] A67 (pinta) existe no cache: {a67}")
    return falhas


def main() -> int:
    t0 = time.perf_counter()
    print("=" * 64)
    print("NIVEL 1: de-para semantico (CID_10.CSV)")
    print("=" * 64)
    f1 = test_semantico()
    print("=" * 64)
    print("NIVEL 2: aderencia ao cache DuckDB")
    print("=" * 64)
    f2 = test_cache()
    total = f1 + f2
    print(f"\n=== {total} falha(s) em {time.perf_counter() - t0:.2f}s ===")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
