"""Catalogo de grupos do cache (group_registry / group_periods / ingest_log).

Substitui a descoberta por glob + sonda por grupo: o catalogo vive no banco
principal e e reconciliado com o disco por :func:`sync`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("datasus_llm")

# Nomes que nunca sao grupos (banco principal, fallback e pastas de apoio).
NOMES_RESERVADOS = {
    "datasus", "datasus_fallback", "parquet", "cache", "tmp",
    "colunas_cache", "duckdb_spill", "layout_csv", "official",
}

_IDENTIFICADOR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")

DDL = (
    """CREATE TABLE IF NOT EXISTS group_registry (
        name VARCHAR PRIMARY KEY,
        db_path VARCHAR,
        source_dir VARCHAR,
        period_column VARCHAR,
        schema_version INTEGER DEFAULT 2,
        rows_est BIGINT DEFAULT 0,
        files INTEGER DEFAULT 0,
        status VARCHAR DEFAULT 'unknown',
        updated_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS group_periods (
        grupo VARCHAR,
        atend_ano INTEGER,
        atend_mes VARCHAR,
        PRIMARY KEY (grupo, atend_ano, atend_mes)
    )""",
    """CREATE TABLE IF NOT EXISTS ingest_log (
        id BIGINT,
        grupo VARCHAR,
        started_at TIMESTAMP,
        files INTEGER,
        invalid INTEGER,
        rows_est BIGINT,
        status VARCHAR
    )""",
)


def nome_valido(nome: str) -> bool:
    """Informa se ``nome`` pode ser usado como grupo/schema SQL.

    Args:
        nome: nome da pasta ou do arquivo .duckdb.

    Returns:
        bool: True para identificador seguro e nao reservado.
    """
    return (
        bool(nome)
        and nome not in NOMES_RESERVADOS
        and bool(_IDENTIFICADOR.match(nome))
    )


def ensure_schema(conn: Any) -> None:
    """Cria as tabelas do catalogo no banco principal (idempotente).

    Args:
        conn: conexao DuckDB do banco principal.
    """
    for sql in DDL:
        conn.execute(sql)


def _subpastas(base: Path | None) -> list[str]:
    """Lista as subpastas com nome valido de ``base`` (vazio se ausente)."""
    if base is None or not base.is_dir():
        return []
    try:
        return sorted(
            p.name for p in base.iterdir()
            if p.is_dir() and nome_valido(p.name)
        )
    except OSError as exc:
        LOGGER.warning("group_registry: falha ao listar %s: %s", base, exc)
        return []


def grupos_no_disco(
    cache_dir: Path, data_dir: Path | None
) -> tuple[set[str], dict[str, Path]]:
    """Descobre grupos no disco (bancos .duckdb e pastas de origem).

    A uniao das fontes corrige o caso de grupo com Parquet em
    ``cache_dir/<grupo>`` mas sem ``<grupo>.duckdb`` (ex.: ab/abo).

    Args:
        cache_dir: diretorio de cache (bancos e subpastas por grupo).
        data_dir: diretorio de dados original (opcional).

    Returns:
        tuple[set[str], dict[str, Path]]: nomes encontrados e a pasta de
        origem de cada um (quando existir).
    """
    nomes: set[str] = set()
    origem: dict[str, Path] = {}
    try:
        for db in sorted(cache_dir.glob("*.duckdb")):
            if nome_valido(db.stem):
                nomes.add(db.stem)
    except OSError as exc:
        LOGGER.warning("group_registry: falha ao listar %s: %s", cache_dir, exc)
    for base in (data_dir, cache_dir):
        for nome in _subpastas(base):
            nomes.add(nome)
            if base is not None:
                origem.setdefault(nome, base / nome)
    return nomes, origem


def registrar(conn: Any, nome: str, db_path: Path, source_dir: Path | None) -> None:
    """Registra ou atualiza um grupo no catalogo (idempotente).

    Args:
        conn: conexao DuckDB do banco principal.
        nome: nome do grupo.
        db_path: caminho do ``<grupo>.duckdb``.
        source_dir: pasta de origem dos Parquet (opcional).
    """
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO group_registry "
        "(name, db_path, source_dir, status, updated_at) "
        "VALUES (?, ?, ?, 'unknown', now()) "
        "ON CONFLICT (name) DO UPDATE SET "
        "db_path = excluded.db_path, "
        "source_dir = COALESCE(excluded.source_dir, group_registry.source_dir)",
        [nome, str(db_path), str(source_dir) if source_dir else None],
    )


def sync(conn: Any, cache_dir: Path, data_dir: Path | None) -> list[str]:
    """Reconcilia o catalogo com o disco e devolve os grupos conhecidos.

    Insere grupos novos e atualiza ``db_path``/``source_dir``; nunca apaga
    registros (grupo removido do disco fica com status antigo, visivel).

    Args:
        conn: conexao DuckDB do banco principal.
        cache_dir: diretorio de cache.
        data_dir: diretorio de dados original (opcional).

    Returns:
        list[str]: nomes de grupos encontrados no disco, ordenados.
    """
    ensure_schema(conn)
    nomes, origem = grupos_no_disco(cache_dir, data_dir)
    for nome in sorted(nomes):
        registrar(conn, nome, cache_dir / f"{nome}.duckdb", origem.get(nome))
    LOGGER.info("group_registry: %d grupos sincronizados", len(nomes))
    return sorted(nomes)


def registrados(conn: Any) -> dict[str, dict[str, Any]]:
    """Le o catalogo e devolve ``{nome: linha}`` (vazio em caso de erro).

    Args:
        conn: conexao DuckDB do banco principal.

    Returns:
        dict[str, dict[str, Any]]: registros indexados pelo nome do grupo.
    """
    try:
        cur = conn.execute(
            "SELECT name, db_path, source_dir, period_column, schema_version, "
            "rows_est, files, status FROM group_registry ORDER BY name"
        )
        colunas = [desc[0] for desc in cur.description]
        return {row[0]: dict(zip(colunas, row)) for row in cur.fetchall()}
    except Exception as exc:
        LOGGER.warning("group_registry: falha ao ler catalogo: %s", exc)
        return {}


def atualizar_apos_ingestao(
    conn: Any, grupo: str, files: int, rows_est: int, periodos: list[tuple[int, str]]
) -> None:
    """Atualiza status/tamanho do grupo e seus periodos apos a ingestao.

    Args:
        conn: conexao DuckDB do banco principal.
        grupo: nome do grupo.
        files: quantidade de arquivos no manifesto do grupo.
        rows_est: estimativa de linhas do grupo.
        periodos: pares ``(atend_ano, atend_mes)`` disponiveis.
    """
    ensure_schema(conn)
    conn.execute(
        "UPDATE group_registry SET files = ?, rows_est = ?, status = 'ok', "
        "updated_at = now() WHERE name = ?",
        [files, rows_est, grupo],
    )
    conn.execute("DELETE FROM group_periods WHERE grupo = ?", [grupo])
    if periodos:
        conn.executemany(
            "INSERT INTO group_periods (grupo, atend_ano, atend_mes) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [(grupo, int(ano), str(mes).zfill(2)) for ano, mes in periodos],
        )


def periodos(conn: Any) -> list[tuple[str, int, str]]:
    """Le ``(grupo, atend_ano, atend_mes)`` do catalogo.

    Args:
        conn: conexao DuckDB do banco principal.

    Returns:
        list[tuple[str, int, str]]: periodos conhecidos (vazio em erro).
    """
    try:
        return [
            (str(grupo), int(ano), str(mes))
            for grupo, ano, mes in conn.execute(
                "SELECT grupo, atend_ano, atend_mes FROM group_periods"
            ).fetchall()
        ]
    except Exception as exc:
        LOGGER.warning("group_registry: falha ao ler periodos: %s", exc)
        return []
