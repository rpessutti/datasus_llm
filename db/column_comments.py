"""Metadados (descrição/exemplos) dos campos do DATASUS nas colunas do DuckDB.

Os layouts oficiais dos arquivos DATASUS ficam em ``<cache_dir>/layout_csv/<grupo>.csv``.
Cada CSV descreve os campos de um grupo (mesmo nome do banco ``<grupo>.duckdb``),
com as colunas ``campo``, ``descricao`` e (em alguns layouts) ``exemplos``.

Este módulo lê esse layout e grava a descrição (e os exemplos, quando houver)
como COMENTÁRIO das colunas da TABELA ``datasus_cache`` do grupo — a tabela
física que materializa os dados. Coerente com o pedido, a VIEW ``datasus`` NÃO
é comentada.

Regra de casamento (importante): só é aplicado comentário quando o nome do
campo do CSV coincide EXATAMENTE com o nome de uma coluna existente na tabela
``datasus_cache``. Campos do layout que não existem no banco (ou colunas do
banco sem entrada no layout) são ignorados.

Idempotente: reaplicar sobrescreve o comentário com o mesmo valor; criar/remover
colunas não é feito aqui.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable

LOGGER = logging.getLogger("datasus_llm")

# Nome do diretório de layout por grupo: <cache_dir>/layout_csv/<grupo>.csv
LAYOUT_DIRNAME = "layout_csv"


def layout_dir(cache_dir: Path | str) -> Path:
    """Diretório onde ficam os CSVs de layout (``<cache_dir>/layout_csv``)."""
    return Path(cache_dir) / LAYOUT_DIRNAME


def load_layout(cache_dir: Path | str, grupo: str) -> Dict[str, Dict[str, str]]:
    """Lê ``<cache_dir>/layout_csv/<grupo>.csv`` e devolve ``{campo: {...}}``.

    Retorna um dicionário vazio quando o arquivo não existe ou está ilegível.
    Cada valor contém as chaves ``descricao`` e ``exemplos`` (strings, podendo
    ser vazias). Linhas sem ``campo`` são ignoradas.
    """
    path = layout_dir(cache_dir) / f"{grupo}.csv"
    if not path.is_file():
        LOGGER.debug("column_comments: layout inexistente para grupo %s (%s)", grupo, path)
        return {}

    result: Dict[str, Dict[str, str]] = {}
    try:
        # utf-8-sig remove o BOM, presente nos layouts oficiais.
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                campo = (row.get("campo") or "").strip()
                if not campo:
                    continue
                descricao = (row.get("descricao") or "").strip()
                exemplos = (row.get("exemplos") or "").strip()
                # Em layouts com campo duplicado, mantém o primeiro preenchido.
                if campo in result and result[campo].get("descricao"):
                    continue
                result[campo] = {"descricao": descricao, "exemplos": exemplos}
    except Exception:
        LOGGER.exception("column_comments: falha ao ler layout %s", path)
        return {}
    return result


def build_comment(descricao: str, exemplos: str) -> str:
    """Monta o texto do comentário a partir de descrição e exemplos.

    Formato: ``<descricao>`` quando não há exemplos; caso contrário
    ``<descricao> Exemplos: <exemplos>``. Trechos vazios são omitidos.
    """
    descricao = (descricao or "").strip()
    exemplos = (exemplos or "").strip()
    if descricao and exemplos:
        return f"{descricao} Exemplos: {exemplos}"
    return descricao or exemplos


def _quote_ident(name: str) -> str:
    """Escapa e cita um identificador DuckDB (schema/tabela/coluna)."""
    return '"' + name.replace('"', '""') + '"'


def _qualified_name(grupo: str, table: str, qualified: bool) -> str:
    """Monta ``<grupo>.<table>`` (ou só ``<table>``), citando cada parte.

    IMPORTANTE: cada parte é citada SEPARADAMENTE. Citar tudo junto
    (``"grupo.datasus_cache"``) faria o DuckDB procurar um único identificador
    literal contendo o ponto, resultando em
    "Catalog Error: Table with name grupo.datasus_cache does not exist!".
    """
    if qualified:
        return f"{_quote_ident(grupo)}.{_quote_ident(table)}"
    return _quote_ident(table)


def _table_columns(connection: Any, table_sql: str) -> set[str]:
    """Nomes de coluna de uma tabela/view DuckDB (args já citáveis em SQL)."""
    return {row[0] for row in connection.execute(f"DESCRIBE {table_sql}").fetchall()}


def apply_comments(
    connection: Any,
    cache_dir: Path | str,
    grupo: str,
    table: str = "datasus_cache",
    qualified: bool = False,
) -> int:
    """Aplica comentários das colunas da ``table`` do grupo a partir do layout.

    Args:
        connection: conexão DuckDB já aberta (do grupo ou a principal).
        cache_dir: diretório de cache onde vive ``layout_csv/<grupo>.csv``.
        grupo: nome do grupo (ex.: ``pa``), usado para achar o CSV.
        table: tabela alvo dos comentários (default: ``datasus_cache``).
        qualified: quando True, qualifica a tabela como ``<grupo>.<table>``
            (necessário ao usar a conexão principal com schemas anexados).

    Returns:
        Quantidade de comentários gravados com sucesso.

    Casamento EXATO: só comenta colunas cujo nome coincide com o ``campo`` do
    layout. Ignora o restante. Nunca toca na VIEW ``datasus``.
    """
    if connection is None:
        return 0

    layout = load_layout(cache_dir, grupo)
    if not layout:
        return 0

    target_sql = _qualified_name(grupo, table, qualified)
    try:
        existing = _table_columns(connection, target_sql)
    except Exception:
        # Tabela ausente (ex.: grupo ainda não materializado): nada a fazer.
        LOGGER.debug(
            "column_comments: tabela %s inexistente para o grupo %s; ignorando",
            target_sql, grupo,
        )
        return 0

    applied = 0
    for campo, meta in layout.items():
        if campo not in existing:
            # Campo do layout SEM coluna correspondente no banco: ignorado.
            continue
        comment = build_comment(meta.get("descricao", ""), meta.get("exemplos", ""))
        if not comment:
            continue
        col_sql = _quote_ident(campo)
        esc_comment = comment.replace("'", "''")
        try:
            connection.execute(
                f"COMMENT ON COLUMN {target_sql}.{col_sql} IS '{esc_comment}'"
            )
            applied += 1
        except Exception:
            LOGGER.exception(
                "column_comments: falha ao comentar coluna %s.%s", target_sql, campo,
            )
    LOGGER.info(
        "column_comments: grupo %s -> %d colunas comentadas (de %d campos no layout)",
        grupo, applied, len(layout),
    )
    return applied


def matched_fields(cache_dir: Path | str, grupo: str, columns: Iterable[str]) -> list[str]:
    """Campos do layout que coincidem EXATAMENTE com ``columns`` (para inspeção/testes)."""
    layout = load_layout(cache_dir, grupo)
    colset = set(columns)
    return [c for c in layout if c in colset]


def apply_comments_for_groups(
    connection: Any,
    cache_dir: Path | str,
    table: str = "datasus_cache",
    qualified: bool = True,
) -> Dict[str, int]:
    """Aplica comentários para todos os grupos com layout disponível.

    Útil para rodar sobre uma conexão principal que já anexou vários schemas.
    """
    counts: Dict[str, int] = {}
    directory = layout_dir(cache_dir)
    if not directory.is_dir():
        return counts
    for fname in sorted(os.listdir(directory)):
        if not fname.lower().endswith(".csv"):
            continue
        grupo = os.path.splitext(fname)[0]
        counts[grupo] = apply_comments(
            connection, cache_dir, grupo, table=table, qualified=qualified
        )
    return counts
