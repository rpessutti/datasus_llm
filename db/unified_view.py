"""View unificada datasus_query_cache (UNION ALL das tabelas de grupo).

Modulo puro: monta SQL; a criacao da view fica no DatasusCache.
"""
from __future__ import annotations

import logging
import re

from db.period_fields import (
    ATEND_ANO,
    ATEND_MES,
    PERIOD_FIELDS,
    ano_int_expr,
    mes_expr,
)

LOGGER = logging.getLogger("datasus_llm")


def referenced_columns(sql: str, available: set[str]) -> set[str]:
    """Extrai (best-effort) as colunas da view referenciadas em ``sql``.

    >>> OTIMIZACAO OOM: em vez de projetar TODAS as colunas unificadas
    (~469) em cada um dos 16 grupos, projetamos apenas as que a consulta
    realmente usa (mais as colunas de periodo). Isso reduz drasticamente
    o volume materializado pelo UNION ALL e o consumo de memoria, sem
    alterar o resultado. Se a extracao nao encontrar nada (ex.: SELECT *),
    o chamador deve cair para o conjunto completo.
    """
    if not sql:
        return set()
    if re.search(r"\bSELECT\s+\*", sql, flags=re.IGNORECASE):
        return set()
    referenced: set[str] = set()
    # Colunas citadas entre aspas duplas.
    for match in re.findall(r'"([^"]+)"', sql):
        referenced.add(match)
    # Identificadores "soltos" (sem aspas) que coincidam com colunas existentes.
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql):
        if token in available or token.upper() in available:
            referenced.add(token)
    # Mantem apenas as que existem de fato no conjunto disponivel (case-insensitive).
    upper_map = {c.upper(): c for c in available}
    return {upper_map[c.upper()] for c in referenced if c.upper() in upper_map}


def build_union_sql(
    s2_groups: list[tuple[str, str]],
    group_columns: dict[str, set[str]],
    all_columns: set[str],
    period_years: list[int],
    period_months: list[str],
    sql: str | None = None,
    extra_columns: set[str] | None = None,
    request_id: str = "-",
) -> tuple[str | None, int]:
    """Monta o UNION ALL das tabelas dos grupos (projecao minima).

    Projeta apenas as colunas referenciadas na SQL (mais atend_ano/
    atend_mes) e aplica o filtro temporal pelo campo nativo de cada grupo.
    Grupos sem campo de periodo mapeado sao excluidos.

    Args:
        s2_groups: pares (safe_name, nome) dos grupos disponiveis.
        group_columns: colunas por safe_name.
        all_columns: uniao das colunas de todos os grupos.
        period_years: anos do recorte.
        period_months: meses do recorte.
        sql: SQL final (para projecao minima).
        extra_columns: colunas adicionais a projetar.
        request_id: identificador para telemetria.

    Returns:
        tuple[str | None, int]: SQL do UNION (None se nenhum grupo entrar)
        e a quantidade de grupos incluidos.
    """
    group_selects: list[str] = []
    projected_columns = set(all_columns)
    if sql:
        referenced = referenced_columns(sql, all_columns)
        if referenced:
            referenced.add(ATEND_ANO)
            referenced.add(ATEND_MES)
            projected_columns = referenced
    # >>> PARTE 7 (#9): colunas EXTRA forcam a projecao a incluir
    # a coluna FALTANTE sem degradar para TODAS as ~469 colunas.
    # Usado pelo fallback de 'coluna ausente' em execute().
    if extra_columns:
        filtrados = {c for c in extra_columns if c in all_columns}
        if filtrados:
            projected_columns = set(projected_columns) | filtrados
            projected_columns.add(ATEND_ANO)
            projected_columns.add(ATEND_MES)
            LOGGER.info(
                "question cache reprojecao minima (+%d colunas faltantes) "
                "request_id=%s colunas=%d de %d",
                len(filtrados), request_id, len(projected_columns), len(all_columns),
            )
            LOGGER.info(
                "question cache projeção mínima (VIEW) request_id=%s colunas=%d de %d",
                request_id, len(projected_columns), len(all_columns),
            )

    for safe_name, name in s2_groups:
        cols = group_columns[safe_name]
        pf = PERIOD_FIELDS.get(name)
        if pf is None:
            # >>> CORRECAO (grupo sem campo de periodo) <<<
            # Antes, um grupo SEM campo nativo de periodo entrava no
            # UNION com atend_ano/atend_mes = NULL e WHERE TRUE, isto
            # e, SEM recorte temporal. Isso CONTAMINAVA o resultado:
            # as linhas desse grupo apareciam em QUALQUER periodo,
            # independentemente do ano/mes pedido na pergunta.
            #
            # Agora o grupo e EXCLUIDO do UNION (nao gera ATEND_ANO/
            # ATEND_MES nulos nem entra sem filtro). Se nenhum grupo
            # sobrar, group_selects fica vazio e o fluxo cai no
            # comportamento ja existente ("Nenhum periodo do cache
            # corresponde ao informado na pergunta."), sem excecao nova.
            LOGGER.warning(
                "grupo %s: sem campo de período mapeado em PERIOD_FIELDS; "
                "EXCLUÍDO do UNION por não respeitar o recorte temporal "
                "(evita contaminação por linhas fora do período).",
                name,
            )
            continue
        pcol = pf.columns[0]                         # campo de data nativo do grupo
        # >>> FASE 3 (#3.5): usa as colunas FISICAS atend_ano/atend_mes <<<
        # Se a tabela do grupo ja materializa atend_ano/atend_mes
        # (FASE 3), referenciamos as colunas fisicas - o filtro vira
        # push-down nativo. Em caches legados (sem as colunas), caimos
        # no calculo derivado a partir do campo nativo, mantendo o
        # comportamento anterior.
        if ATEND_ANO in cols and ATEND_MES in cols:
            ano_sql = f'"{ATEND_ANO}"'                # coluna fisica (push-down)
            mes_sql = f'"{ATEND_MES}"'                # coluna fisica (push-down)
        else:
            # FASE 3B: atend_ano e INTEGER; o fallback derivado tambem
            # precisa ser INTEGER para nao misturar tipos no UNION ALL.
            ano_sql = ano_int_expr(pcol)              # ano (YYYY) INTEGER derivado
            mes_sql = mes_expr(pcol)                  # mes (MM) derivado
        conds = []
        if period_years:
            # FASE 3B: atend_ano e INTEGER na tabela/view/resumos.
            # Monta literais INTEIROS (ex.: 2022) para comparacao
            # numerica nativa e push-down. Fallback para string caso
            # o ano nao seja numerico (defensivo).
            def _ano_lit(y: str) -> str:
                """Formata o ano como literal SQL (inteiro quando possivel)."""
                ys = str(y).strip()
                return ys if ys.lstrip("-").isdigit() else "'" + ys.replace("'", "''") + "'"
            anos_lit = ", ".join(_ano_lit(y) for y in period_years)
            conds.append(f"{ano_sql} IN ({anos_lit})")
        if period_months:
            meses_lit = ", ".join(
                "'" + str(mo).zfill(2) + "'" for mo in period_months
            )
            conds.append(f"{mes_sql} IN ({meses_lit})")
        where_group = " AND ".join(conds) if conds else "TRUE"

        projections = []
        for col in projected_columns:
            esc = col.replace('"', '""')
            if col == ATEND_ANO:
                projections.append(f"{ano_sql} AS {ATEND_ANO}")
            elif col == ATEND_MES:
                projections.append(f"{mes_sql} AS {ATEND_MES}")
            elif col in cols:
                projections.append(f'"{esc}"')
            else:
                projections.append(f'CAST(NULL AS VARCHAR) AS "{esc}"')
        group_selects.append(
            f"SELECT {', '.join(projections)} FROM {safe_name}.datasus_cache "
            f"WHERE {where_group}"
        )

    if not group_selects:
        return None, 0
    return " UNION ALL ".join(group_selects), len(group_selects)
