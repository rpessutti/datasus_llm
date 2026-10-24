"""Agregados resumo_* do cache (diagnosticos, cid_periodo e anual_cid).

Modulo com acesso a banco: recebe a conexao principal por parametro.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from config.constants import DIAGNOSIS_COLUMN_NAMES
from db.period_fields import PERIOD_FIELDS, ATEND_ANO, ATEND_MES

LOGGER = logging.getLogger("datasus_llm")


def summary_tables(conn: Any) -> list[str]:
    """Lista quais tabelas de resumo existem no banco principal.

    Returns:
        list[str]: subconjunto de ``resumo_diagnosticos``,
        ``resumo_cid_periodo`` e ``resumo_anual_cid``.
    """
    names = {"resumo_diagnosticos", "resumo_cid_periodo", "resumo_anual_cid"}
    result = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    present = [row[0] for row in result if row and row[0] in names]

    # >>> CORRECAO (schema obsoleto dos resumos persistidos) <<<
    # As tabelas resumo_* vivem no .duckdb PRINCIPAL e sao PERSISTIDAS entre
    # execucoes. Se foram criadas por uma versao ANTIGA do codigo (que ainda
    # derivava o periodo de parquet_year/parquet_month e nao normalizava as
    # colunas), elas continuam no banco com um schema INCOMPATIVEL:
    #   resumo_cid_periodo(casos, parquet_month, parquet_year)
    # Como _summary_tables() so checava o NOME, ela anunciava essas tabelas
    # ao LLM como se tivessem (atend_ano, atend_mes, sexo, cid, casos). O LLM
    # entao gerava SQL CORRETO contra o schema ESPERADO
    #   ... FROM resumo_cid_periodo WHERE atend_ano = 2025 AND cid LIKE 'A90%'
    # mas o DuckDB falhava na execucao (so existiam parquet_year/parquet_month):
    #   Binder Error: ... does not have a column named "atend_ano"
    # Aqui VALIDAMOS o schema real e DROPAMOS qualquer resumo INCOMPATIVEL.
    # Assim ele nao e anunciado/usado ate ser reconstruido por
    # build_summary_tables() (que ja cria o schema correto).
    # Valida o schema COMPLETO que anunciamos ao LLM: se faltar qualquer
    # coluna normalizada esperada, a tabela e considerada obsoleta.
    required = {
        "resumo_diagnosticos": {"atend_ano", "atend_mes", "sexo", "cid", "casos"},
        "resumo_cid_periodo": {"atend_ano", "atend_mes", "sexo", "cid", "casos"},
        "resumo_anual_cid": {"atend_ano", "cid", "casos"},
    }
    healthy: list[str] = []
    for table in present:
        try:
            cols = {
                row[0].lower()
                for row in conn.execute(
                    f"DESCRIBE {table}"
                ).fetchall()
            }
        except Exception as exc:
            LOGGER.warning("summary: falha no DESCRIBE de %s: %s", table, exc)
            continue
        missing = required.get(table, set()) - cols
        if missing:
            LOGGER.warning(
                "summary: tabela %s tem schema OBSOLETO (colunas=%s; faltam %s); "
                "descartando para ser reconstruída pelo próximo build_summary_tables().",
                table, sorted(cols), sorted(missing),
            )
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception as exc:
                LOGGER.warning("summary: falha ao DROP de %s: %s", table, exc)
            continue
        healthy.append(table)
    return sorted(healthy)


def build_summary_tables(conn: Any, relations: list[str]) -> dict[str, Any]:
    """(Re)constroi resumo_diagnosticos, resumo_cid_periodo e resumo_anual_cid.

    Le as TABELAS de grupo (nao os Parquet), agrega diagnosticos por
    periodo/sexo/CID e grava no banco principal.

    Args:
        conn: conexao DuckDB do banco principal.
        relations: relacoes qualificadas (ex.: '"er".datasus_cache').

    Returns:
        dict[str, Any]: {"files", "tables", "duration_ms"}.
    """
    started = time.perf_counter()
    if conn is None:
        raise RuntimeError(
            "A conexao DuckDB nao esta disponivel para construir os resumos."
        )
    if not relations:
        raise RuntimeError("Nenhuma tabela de grupo para materializar os resumos.")
    LOGGER.info("summary build start groups=%d", len(relations))

    conn.execute("DROP TABLE IF EXISTS resumo_diagnosticos")
    conn.execute("DROP TABLE IF EXISTS resumo_cid_periodo")
    conn.execute("DROP TABLE IF EXISTS resumo_anual_cid")
    conn.execute(
        f"CREATE TABLE resumo_diagnosticos ("
        f"{ATEND_ANO} INTEGER, {ATEND_MES} VARCHAR, sexo VARCHAR, "
        "cid VARCHAR, casos BIGINT)"
    )

    groups_found = 0
    # Candidatos a coluna nativa de periodo (db/period_fields.py).
    _period_cols_candidates: list[str] = []
    for _pf in PERIOD_FIELDS.values():
        for _c in _pf.columns:
            if _c not in _period_cols_candidates:
                _period_cols_candidates.append(_c)

    for relation in relations:
        try:
            available = {
                row[0]
                for row in conn.execute(f"DESCRIBE {relation}").fetchall()
            }
        except Exception as e:
            LOGGER.error("Erro ao descrever %s: %s. Pulando...", relation, e)
            continue
        available_upper = {col.upper(): col for col in available}
        period_col = None
        for cand in _period_cols_candidates:
            if cand.upper() in available_upper:
                period_col = available_upper[cand.upper()]
                break
        if period_col is None:
            LOGGER.warning(
                "summary: %s sem coluna nativa de periodo; pulando", relation
            )
            continue
        diagnosis_columns = [
            available_upper[col_name.upper()]
            for col_name in DIAGNOSIS_COLUMN_NAMES
            if col_name.upper() in available_upper
        ]
        if not diagnosis_columns:
            LOGGER.warning(
                "summary: %s sem colunas de diagnostico; pulando", relation
            )
            continue
        groups_found += 1
        ano_sql = ano_expr(period_col)
        mes_sql = mes_expr(period_col)
        sex_expression = (
            'CAST("AP_SEXO" AS VARCHAR)' if "AP_SEXO" in available
            else "CAST(NULL AS VARCHAR)"
        )
        diagnosis_selects = []
        for column in diagnosis_columns:
            escaped = column.replace('"', '""')
            diagnosis_selects.append(
                "SELECT " + ano_sql + f" AS {ATEND_ANO}, "
                + mes_sql + f" AS {ATEND_MES}, "
                + sex_expression + " AS sexo, "
                + 'UPPER(TRIM(CAST("' + escaped + '" AS VARCHAR))) AS cid FROM '
                + relation + ' WHERE "' + escaped + '" IS NOT NULL AND '
                + 'TRIM(CAST("' + escaped + '" AS VARCHAR)) <> ' + "''"
            )
        batch_sql = (
            "INSERT INTO resumo_diagnosticos SELECT "
            + f"{ATEND_ANO}, {ATEND_MES}, sexo, cid, COUNT(*) AS casos FROM ("
            + " UNION ALL ".join(diagnosis_selects)
            + ") AS diagnoses GROUP BY 1, 2, 3, 4"
        )
        try:
            conn.execute(batch_sql)
        except Exception as e:
            LOGGER.error("Erro ao resumir %s: %s. Pulando...", relation, e)

    if groups_found == 0:
        raise RuntimeError(
            "Nenhuma tabela de grupo com periodo/diagnostico foi encontrada."
        )

    conn.execute(
        f"CREATE OR REPLACE TABLE resumo_cid_periodo AS SELECT {ATEND_ANO}, "
        f"{ATEND_MES}, sexo, cid, casos FROM resumo_diagnosticos"
    )
    conn.execute(
        f"CREATE OR REPLACE TABLE resumo_anual_cid AS SELECT {ATEND_ANO}, cid, "
        "SUM(casos) AS casos FROM resumo_diagnosticos GROUP BY 1, 2"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_resumo_diagnosticos_periodo "
        f"ON resumo_diagnosticos ({ATEND_ANO}, {ATEND_MES}, cid)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_resumo_anual_cid "
        f"ON resumo_anual_cid ({ATEND_ANO}, cid)"
    )
    tabelas = summary_tables(conn)
    LOGGER.info(
        "summary build complete groups=%d tables=%s duration_ms=%.2f",
        len(relations), tabelas, (time.perf_counter() - started) * 1000,
    )
    return {
        "files": len(relations),
        "tables": tabelas,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
