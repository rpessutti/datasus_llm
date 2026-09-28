#!/usr/bin/env python3
"""Aplica descrição/exemplos dos layouts CSV como COMENTÁRIO nas colunas do DuckDB.

Para cada grupo, lê ``<cache_dir>/layout_csv/<grupo>.csv`` e grava a descrição
(e os exemplos, quando existirem) como comentário das colunas da TABELA
``datasus_cache`` do arquivo ``<cache_dir>/<grupo>.duckdb``.

Regra: SÓ comenta quando o nome do campo do CSV coincide EXATAMENTE com uma
coluna existente em ``datasus_cache``. Campos do layout sem coluna
correspondente são ignorados. A VIEW ``datasus`` NÃO é comentada.

Uso:
    python dev_tools/apply_column_comments.py            # todos os grupos
    python dev_tools/apply_column_comments.py pa ps      # grupos específicos
    python dev_tools/apply_column_comments.py --dry-run  # não grava nada
    python dev_tools/apply_column_comments.py --all

Após rodar, os comentários ficam persistidos no próprio arquivo .duckdb.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import setting  # noqa: E402
from db.column_comments import (  # noqa: E402
    apply_comments,
    layout_dir,
    load_layout,
)


def _groups(cache_dir: Path, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    layout = layout_dir(cache_dir)
    if not layout.is_dir():
        return []
    return sorted(
        p.stem for p in layout.glob("*.csv")
        if (cache_dir / f"{p.stem}.duckdb").exists()
    )


def process(cache_dir: Path, grupo: str, dry_run: bool) -> None:
    db_path = cache_dir / f"{grupo}.duckdb"
    if not db_path.exists():
        print(f"[{grupo}] SKIP: {db_path.name} não existe")
        return
    layout = load_layout(cache_dir, grupo)
    if not layout:
        print(f"[{grupo}] SKIP: layout_csv/{grupo}.csv ausente/vazio")
        return

    conn = duckdb.connect(str(db_path), read_only=dry_run)
    try:
        if dry_run:
            cols = {r[0] for r in conn.execute("DESCRIBE datasus_cache").fetchall()}
            matched = [c for c in layout if c in cols]
            print(
                f"[{grupo}] DRY-RUN: {len(matched)} de {len(layout)} campos do layout "
                f"coincidem com colunas de datasus_cache"
            )
            return
        applied = apply_comments(conn, cache_dir, grupo, table="datasus_cache", qualified=False)
        print(f"[{grupo}] OK: {applied} colunas comentadas (layout com {len(layout)} campos)")
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    explicit = [a for a in argv[1:] if not a.startswith("--")]
    cache_dir = setting.cache_dir

    grupos = _groups(cache_dir, explicit)
    if not grupos:
        print(f"nenhum grupo com layout em {layout_dir(cache_dir)}")
        return 0

    print(f"cache_dir={cache_dir} dry_run={dry_run} grupos={grupos}")
    for g in grupos:
        try:
            process(cache_dir, g, dry_run)
        except Exception as exc:  # pragma: no cover
            print(f"[{g}] ERRO: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
