# -*- coding: utf-8 -*-
"""Campos NATIVOS de período (atendimento/competência) por grupo do cache DATASUS.

Este módulo é a FONTE ÚNICA DE VERDADE para o recorte temporal das consultas.
Ele substitui o uso de ``parquet_year``/``parquet_month`` (metadados derivados do
NOME DO ARQUIVO — competência de processamento/remessa) por CAMPOS REAIS do
DATASUS presentes em cada grupo.

Mapeamento validado contra o cache real (cobertura de nulos/vazios = 0% e mês
válido 01-12 = 100% em todos os grupos):

    grupo                campo          formato      descrição
    -------------------  -------------  -----------  -----------------------------------
    sp                    SP_DTINTER     AAAAMMDD     data de internação
    rj, rd, er            DT_INTER       AAAAMMDD     data de internação
    ps, bi                DT_ATEND       AAAAMM       data de atendimento
    pa                    PA_CMP         AAAAMM       competência da produção (SIA/PA)
    acf, ad, am, amp,
    aq, ar, atd           AP_DTINIC      AAAAMMDD     início do atendimento
    ab, abo               AP_DTINIC      AAAAMMDD     início do atendimento

Não há FALLBACK: cada grupo usa o SEU campo nativo. ``parquet_year`` e
``parquet_month`` NÃO são usados em filtros nem em agrupamento.
"""

from __future__ import annotations

from dataclasses import dataclass

# Colunas derivadas expostas pela view ``datasus_query_cache`` (ano/mês do
# atendimento, calculadas a partir do campo nativo de cada grupo).
ATEND_ANO = "atend_ano"
ATEND_MES = "atend_mes"


@dataclass(frozen=True)
class PeriodField:
    """Descreve o campo nativo de período de um grupo.

    Attributes:
        kind: "date8" (AAAAMMDD) ou "ym6" (AAAAMM). Ambos extraem ano/mês com o
            mesmo SUBSTR, mas o rótulo semântico e validações podem diferir.
        columns: tupla com o(s) nome(s) de coluna no cache do grupo.
        label: descrição legível (usada em mensagens/prompt).
    """

    kind: str
    columns: tuple[str, ...]
    label: str


# Mapa canônico grupo -> campo nativo de período.
PERIOD_FIELDS: dict[str, PeriodField] = {
    "sp":  PeriodField("date8", ("SP_DTINTER",), "data de internação"),
    "rj":  PeriodField("date8", ("DT_INTER",),   "data de internação"),
    "rd":  PeriodField("date8", ("DT_INTER",),   "data de internação"),
    "er":  PeriodField("date8", ("DT_INTER",),   "data de internação"),
    "ps":  PeriodField("ym6",   ("DT_ATEND",),   "data de atendimento"),
    "bi":  PeriodField("ym6",   ("DT_ATEND",),   "data de atendimento"),
    "pa":  PeriodField("ym6",   ("PA_CMP",),     "competência da produção (SIA/PA)"),
    "acf": PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "ad":  PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "am":  PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "amp": PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "aq":  PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "ar":  PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "atd": PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "ab":  PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
    "abo": PeriodField("date8", ("AP_DTINIC",),  "início do atendimento"),
}


def get_period_field(grupo: str) -> PeriodField | None:
    """Retorna o ``PeriodField`` do grupo (ou ``None`` se não mapeado)."""
    return PERIOD_FIELDS.get(grupo)


def period_column_for_group(grupo: str) -> str | None:
    """Retorna o nome da COLUNA de período do grupo (ex.: 'DT_ATEND')."""
    pf = PERIOD_FIELDS.get(grupo)
    return pf.columns[0] if pf else None


def ano_expr(col: str) -> str:
    """Expressão SQL (DuckDB) para o ANO (AAAA) a partir de ``col``.

    Funciona tanto para AAAAMMDD quanto para AAAAMM (SUBSTR(...,1,4)).
    """
    return f'SUBSTR(CAST("{col}" AS VARCHAR), 1, 4)'


def ano_int_expr(col: str) -> str:
    """Expressão SQL (DuckDB) para o ANO (AAAA) como INTEGER a partir de ``col``.

    Diferente de :func:`ano_expr` (que devolve VARCHAR), esta expressão produz
    um INTEGER — o TIPO canônico de ``atend_ano`` na tabela/view/``resumo_*``.
    Usar INTEGER permite comparações numéricas nativas (ex.: ``atend_ano = 2024``
    e ``atend_ano IN (2022, 2023)``) sem casts implícitos.

    ``TRY_CAST`` torna a expressão resiliente a valores não numéricos/ausentes
    (resulta em NULL em vez de erro).
    """
    return f'TRY_CAST(SUBSTR(CAST("{col}" AS VARCHAR), 1, 4) AS INTEGER)'


def mes_expr(col: str) -> str:
    """Expressão SQL (DuckDB) para o MÊS ('MM') a partir de ``col``.

    Funciona tanto para AAAAMMDD quanto para AAAAMM (SUBSTR(...,5,2)).
    """
    return f'SUBSTR(CAST("{col}" AS VARCHAR), 5, 2)'


def atend_ano_expr(grupo: str) -> str | None:
    """Expressão SQL do ano de atendimento do grupo (ou ``None`` se não mapeado)."""
    col = period_column_for_group(grupo)
    return ano_expr(col) if col else None


def atend_mes_expr(grupo: str) -> str | None:
    """Expressão SQL do mês de atendimento do grupo (ou ``None`` se não mapeado)."""
    col = period_column_for_group(grupo)
    return mes_expr(col) if col else None


def group_has_period(grupo: str) -> bool:
    """True quando o grupo tem campo nativo de período mapeado."""
    return grupo in PERIOD_FIELDS