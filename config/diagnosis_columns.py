# -*- coding: utf-8 -*-
"""Classificação das colunas de diagnóstico/CID por PAPEL (principal x secundário).

Motivação (bug "quantos casos de dengue" — request_id=06affae9bf54):
a função ``_expand_cid_filters_multi_column`` expandia um filtro CID de UMA
coluna para TODAS as 24 colunas de diagnóstico (principal, secundária, causa
de morte e comorbidade). Isso fazia ``COUNT(*)`` contar qualquer registro cujo
diagnóstico SECUNDÁRIO/ASSOCIADO/CAUSA DE MORTE fosse a doença, inflando o
total de "casos" e misturando semânticas distintas.

Este módulo define, para CADA grupo real do cache DATASUS, os nomes EXATOS das
colunas que devem participar de um filtro de DOENÇA:

  * PRINCIPAL  -> diagnóstico principal do atendimento/internação
                  (é o que responde "quantos CASOS de X").
  * SECUNDARIA -> diagnósticos secundários/comorbidades/associados
                  (só entram quando a pergunta pede explicitamente
                   "diagnóstico secundário", "comorbidade" etc.).

Colunas de CAUSA DE MORTE (CID_MORTE, CAUSABAS, MORTE, CAUSAMORTE) NUNCA
entram em um filtro de "casos" — são óbitos, não atendimentos.

IMPORTANTE: os nomes são os REAIS de cada grupo (ver cache/colunas_cache/*.json),
pois grupos diferentes usam nomenclaturas diferentes (SP_CIDPRI, DIAG_PRINC,
PA_CIDPRI, AP_CIDPRI, CIDPRI, ...).
"""
from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Colunas PRINCIPAIS (diagnóstico principal) — por grupo real.
# ---------------------------------------------------------------------------
# Fonte: cache/colunas_cache/<grupo>.json
DIAGNOSIS_PRIMARY_BY_GROUP: Final[dict[str, tuple[str, ...]]] = {
    "ab":  ("AP_CIDPRI", "AP_CIDCAS"),
    "abo": ("CO_CIDPRIM", "AP_CIDCAS"),
    "acf": ("AP_CIDPRI", "AP_CIDCAS"),
    "ad":  ("AP_CIDPRI", "AP_CIDCAS"),
    "am":  ("AP_CIDPRI", "AP_CIDCAS"),
    "amp": ("AP_CIDPRI", "AP_CIDCAS"),
    "aq":  ("AQ_CID10", "AP_CIDPRI", "AP_CIDCAS"),
    "ar":  ("AR_CID10", "AP_CIDPRI", "AP_CIDCAS"),
    "atd": ("AP_CIDPRI", "AP_CIDCAS"),
    "bi":  ("CIDPRI",),
    "er":  (),                       # grupo sem coluna de CID nos dados
    "pa":  ("PA_CIDPRI", "PA_CIDCAS"),
    "ps":  ("CIDPRI",),
    "rd":  ("DIAG_PRINC", "CID_NOTIF"),
    "rj":  ("DIAG_PRINC", "CID_NOTIF"),
    "sp":  ("SP_CIDPRI",),
}

# Colunas PRINCIPAIS conhecidas "soltas" (usadas quando o grupo não está no
# mapa acima, ex.: modo legado). Ordem = prioridade de escolha.
DIAGNOSIS_PRIMARY_NAMES: Final[tuple[str, ...]] = (
    "AP_CIDPRI", "CO_CIDPRIM", "AQ_CID10", "AR_CID10", "CIDPRI",
    "DIAG_PRINC", "SP_CIDPRI", "PA_CIDPRI", "AP_CIDCAS", "PA_CIDCAS",
    "CID_NOTIF", "CIDPRI1", "CIDPRIO",
)

# ---------------------------------------------------------------------------
# Colunas SECUNDÁRIAS (diagnóstico secundário / comorbidade / associado).
# ---------------------------------------------------------------------------
DIAGNOSIS_SECONDARY_BY_GROUP: Final[dict[str, tuple[str, ...]]] = {
    "ab":  ("AP_CIDSEC",),
    "abo": ("CO_CIDSEC", "AP_CID_C1", "AP_CID_C2", "AP_CID_C3",
            "AP_CID_C4", "AP_CID_C5", "AP_CID_CO"),
    "acf": ("AP_CIDSEC",),
    "ad":  ("AP_CIDSEC",),
    "am":  ("AP_CIDSEC",),
    "amp": ("AP_CIDSEC",),
    "aq":  ("AP_CIDSEC", "AQ_CIDINI1", "AQ_CIDINI2", "AQ_CIDINI3"),
    "ar":  ("AP_CIDSEC", "AR_CIDINI1", "AR_CIDINI2", "AR_CIDINI3",
            "AR_CIDTR1", "AR_CIDTR2", "AR_CIDTR3"),
    "atd": ("AP_CIDSEC",),
    "bi":  (),
    "er":  (),
    "pa":  ("PA_CIDSEC",),
    "ps":  ("CIDASSOC",),
    "rd":  ("DIAG_SECUN", "CID_ASSO", "DIAGSEC1", "DIAGSEC2", "DIAGSEC3",
            "DIAGSEC4", "DIAGSEC5", "DIAGSEC6", "DIAGSEC7", "DIAGSEC8",
            "DIAGSEC9"),
    "rj":  ("DIAG_SECUN", "CID_ASSO"),
    "sp":  ("SP_CIDSEC",),
}

DIAGNOSIS_SECONDARY_NAMES: Final[tuple[str, ...]] = (
    "AP_CIDSEC", "CO_CIDSEC", "AQ_CIDINI1", "AQ_CIDINI2", "AQ_CIDINI3",
    "AR_CIDINI1", "AR_CIDINI2", "AR_CIDINI3", "AR_CIDTR1", "AR_CIDTR2",
    "AR_CIDTR3", "CIDASSOC", "DIAG_SECUN", "CID_ASSO", "SP_CIDSEC",
    "PA_CIDSEC", "AP_CID_C1", "AP_CID_C2", "AP_CID_C3", "AP_CID_C4",
    "AP_CID_C5", "AP_CID_CO", "DIAGSEC1", "DIAGSEC2", "DIAGSEC3",
    "DIAGSEC4", "DIAGSEC5", "DIAGSEC6", "DIAGSEC7", "DIAGSEC8", "DIAGSEC9",
)

# ---------------------------------------------------------------------------
# Colunas de CAUSA DE MORTE — NUNCA usadas em filtro de "casos".
# ---------------------------------------------------------------------------
DIAGNOSIS_DEATH_NAMES: Final[frozenset[str]] = frozenset({
    "CID_MORTE", "CAUSABAS", "CAUSABAS_O", "CAUSAMORTE", "CAUSA_MORTE",
    "MORTE",
})


def _norm_set(names: tuple[str, ...] | list[str]) -> frozenset[str]:
    return frozenset(n.strip().upper() for n in names if n and n.strip())


_PRIMARY_ALL = _norm_set(DIAGNOSIS_PRIMARY_NAMES)
_SECONDARY_ALL = _norm_set(DIAGNOSIS_SECONDARY_NAMES)


def classify_diagnosis_column(column: str, group: str | None = None) -> str | None:
    """Classifica uma coluna de diagnóstico em 'principal'/'secundaria'/'morte'.

    Retorna ``None`` se a coluna não for uma coluna de diagnóstico conhecida.

    A classificação prioriza o mapa POR GRUPO (nomes exatos), pois nomes podem
    coincidir entre papéis em grupos diferentes. Se ``group`` for informado e
    a coluna constar das listas daquele grupo, usa o papel do grupo.
    """
    up = (column or "").strip().upper()
    if not up:
        return None

    if group:
        g = group.strip().lower()
        if up in _norm_set(DIAGNOSIS_PRIMARY_BY_GROUP.get(g, ())):
            return "principal"
        if up in _norm_set(DIAGNOSIS_SECONDARY_BY_GROUP.get(g, ())):
            return "secundaria"
        if up in DIAGNOSIS_DEATH_NAMES:
            return "morte"

    # Fallback por listas globais.
    if up in DIAGNOSIS_DEATH_NAMES:
        return "morte"
    if up in _PRIMARY_ALL:
        return "principal"
    if up in _SECONDARY_ALL:
        return "secundaria"
    # Diagnósticos secundários numerados (ex.: DIAGSEC1..DIAGSEC9 no grupo rd).
    if re.match(r"DIAGSEC\d+$", up):
        return "secundaria"
    return None


def is_case_diagnosis_column(column: str, group: str | None = None) -> bool:
    """True se a coluna deve contar em "casos" (principal ou secundária).

    Exclui explicitamente colunas de causa de morte.
    """
    papel = classify_diagnosis_column(column, group)
    return papel in ("principal", "secundaria")


def case_diagnosis_columns(columns: list[str], group: str | None = None) -> list[str]:
    """Filtra ``columns`` mantendo apenas principal+secundária (sem morte)."""
    return [c for c in columns if is_case_diagnosis_column(c, group)]
