"""CID-10: carga da tabela cid10 e prefixos de CID pre-computados.

Modulo com acesso a banco: recebe a conexao principal por parametro.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config.constants import DIAGNOSIS_COLUMN_NAMES
from config.settings import PROJECT_DIR

LOGGER = logging.getLogger("datasus_llm")


def _scalar(conn: Any, sql: str, default: Any = None) -> Any:
    """Executa uma query escalar e devolve o primeiro valor (ou default)."""
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def load_cid10(conn: Any, csv_path: Path | None = None) -> None:
    """Carrega o CID_10.CSV no DuckDB como a tabela cid10 (uma vez).

Idempotente: se a tabela ja existir com linhas, nao recarrega.

Args:
    conn: conexao DuckDB do banco principal.
    csv_path: caminho do CSV (default: PROJECT_DIR/CID_10.CSV).
"""
    # 1) Ja existe? Entao nao recarrega (carga UNICA).
    try:
        existing = _scalar(conn, 
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'cid10'",
            default=0,
        )
        if existing:
            rows = _scalar(conn, "SELECT COUNT(*) FROM cid10", default=0)
            if rows and int(rows) > 0:
                LOGGER.info("tabela cid10 já carregada no DuckDB linhas=%s", rows)
                return
    except Exception as e:
        LOGGER.debug("checagem da tabela cid10 falhou: %s", e)

    csv_path = csv_path or PROJECT_DIR / "CID_10.CSV"
    if not csv_path.exists():
        LOGGER.warning("CID_10.CSV não encontrado em %s; tabela cid10 não sera criada.", csv_path)
        return

    # 2) Carrega via read_csv do DuckDB, especificando o cabecalho REAL.
    #    Usa encoding latin-1 (o arquivo e ISO-8859-1).
    path_sql = "'" + str(csv_path).replace("'", "''") + "'"
    conn.execute("DROP TABLE IF EXISTS cid10")
    conn.execute(
        "CREATE TABLE cid10 AS "
        "SELECT "
        "  UPPER(TRIM(CAST(SUBCAT AS VARCHAR)))   AS subcat, "
        "  CAST(CLASSIF AS VARCHAR)               AS classif, "
        "  CAST(RESTRSEXO AS VARCHAR)             AS restrsexo, "
        "  CAST(CAUSAOBITO AS VARCHAR)            AS causaobito, "
        "  TRIM(CAST(DESCRICAO AS VARCHAR))       AS descricao, "
        "  TRIM(CAST(DESCRABREV AS VARCHAR))      AS descabrev, "
        "  CAST(REFER AS VARCHAR)                 AS refer, "
        "  CAST(EXCLUIDOS AS VARCHAR)             AS excluidos "
        "FROM read_csv(" + path_sql + ", delim=';', header=true, "
        "  encoding='latin-1', quote='', escape='', "
        "  columns={"
        "    'SUBCAT': 'VARCHAR', 'CLASSIF': 'VARCHAR', 'RESTRSEXO': 'VARCHAR', "
        "    'CAUSAOBITO': 'VARCHAR', 'DESCRICAO': 'VARCHAR', 'DESCRABREV': 'VARCHAR', "
        "    'REFER': 'VARCHAR', 'EXCLUIDOS': 'VARCHAR'"
        "  }, ignore_errors=true)"
    )
    rows = _scalar(conn, "SELECT COUNT(*) FROM cid10", default=0)
    LOGGER.info("tabela cid10 carregada no DuckDB linhas=%s path=%s", rows, csv_path)


def diagnosis_columns(conn: Any, relation: str) -> list[str]:
    """Lista as colunas de diagnostico existentes em ``relation``.

Args:
    conn: conexao DuckDB do banco principal.
    relation: relacao qualificada (ex.: "er".datasus_cache).

Returns:
    list[str]: colunas de CID encontradas (ordem do catalogo).
"""
    try:
        desc = conn.execute(f"DESCRIBE {relation}").fetchall()
    except Exception as exc:
        LOGGER.debug("cid_prefixes: DESCRIBE falhou p/ %s: %s", relation, exc)
        return []
    cols_upper = {str(row[0]).upper(): str(row[0]) for row in desc}
    diag = [
        cols_upper[c.upper()] for c in DIAGNOSIS_COLUMN_NAMES if c.upper() in cols_upper
    ]
    if not diag:
        diag = [
            real for up, real in cols_upper.items()
            if "CID" in up or "DIAG" in up or "CAUSA" in up
        ]
    return diag


def populate_prefixes(conn: Any, relations: list[str], force: bool = False, min_len: int = 3, limit: int = 500) -> int:
    """Pre-computa os prefixos de CID reais na tabela cid_prefixes.

Idempotente: se a tabela tem linhas e force=False, nao recomputa.

Args:
    conn: conexao DuckDB do banco principal.
    relations: relacoes a varrer (ex.: ["er".datasus_cache]).
    force: recalcula mesmo com a tabela populada.
    min_len: tamanho do prefixo extraido do dado.
    limit: maximo de valores distintos por coluna.

Returns:
    int: total de prefixos na tabela ao final.
"""
    try:
        existentes = int(_scalar(conn, "SELECT COUNT(*) FROM cid_prefixes", default=0) or 0)
    except Exception:
        existentes = 0
    if existentes > 0 and not force:
        LOGGER.info("cid_prefixes: ja populado (%d prefixos); nada a fazer", existentes)
        return existentes

    prefixos: set[str] = set()

    for relation in relations:
        diag_cols = diagnosis_columns(conn, relation)
        for col in diag_cols[:10]:
            esc = col.replace('"', '""')
            sql = (
                f"SELECT DISTINCT SUBSTR(UPPER(TRIM(CAST(\"{esc}\" AS VARCHAR))), 1, {min_len}) AS p "
                f"FROM {relation} "
                f"WHERE \"{esc}\" IS NOT NULL AND TRIM(CAST(\"{esc}\" AS VARCHAR)) <> ''"
            )
            try:
                for row in conn.execute(sql).fetchmany(limit):
                    val = str(row[0] or "").upper()
                    if len(val) >= 2 and val[0].isalpha():
                        prefixos.add(val)
            except Exception as exc:
                LOGGER.debug("cid_prefixes: falha em %s.%s: %s", relation, col, exc)
                continue

    # Prefixos do de-para OFICIAL (nunca devem faltar).
    try:
        from db.cid_map import CID_ALIASES, get_cid_map
        for codigos in CID_ALIASES.values():
            for codigo in codigos:
                prefixos.add(codigo.upper()[:max(2, min_len)])
        cid_map = get_cid_map()
        if cid_map.loaded:
            for entry in cid_map.entries:
                prefixos.add(entry.inicio.upper()[:3])
    except Exception as exc:
        LOGGER.debug("cid_prefixes: de-para indisponivel: %s", exc)

    prefixos = {p for p in prefixos if len(p) >= 2 and p[0].isalpha()}
    if prefixos:
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO cid_prefixes (prefixo) VALUES (?)",
                [(p,) for p in sorted(prefixos)],
            )
        except Exception as exc:
            LOGGER.warning("cid_prefixes: falha ao inserir: %s", exc)
    total = int(_scalar(conn, "SELECT COUNT(*) FROM cid_prefixes", default=0) or 0)
    LOGGER.info("cid_prefixes: populados %d prefixos (tabela total=%d)", len(prefixos), total)
    return total


def read_prefixes(conn: Any, min_len: int = 2) -> list[str]:
    """Le os prefixos de CID pre-computados.

Args:
    conn: conexao DuckDB do banco principal.
    min_len: tamanho minimo do prefixo.

Returns:
    list[str]: prefixos conhecidos, ordenados.
"""
    try:
        rows = conn.execute("SELECT prefixo FROM cid_prefixes").fetchall()
    except Exception:
        return []
    vals = {
        str(r[0]).upper() for r in rows
        if r and r[0] is not None and len(str(r[0])) >= 2 and str(r[0])[0].isalpha()
    }
    return sorted(vals)
