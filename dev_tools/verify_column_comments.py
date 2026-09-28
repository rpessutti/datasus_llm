#!/usr/bin/env python3
"""Valida os comentários das colunas de datasus_cache de cada grupo.

Verifica, para cada grupo com layout CSV:
  * a integridade do banco (SELECT de contagem em datasus_cache);
  * quantas colunas têm comentário;
  * quantos campos do layout coincidem com colunas.

Uso:
    python dev_tools/verify_column_comments.py            # todos
    python dev_tools/verify_column_comments.py ab pa      # específicos
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import setting  # noqa: E402
from db.column_comments import load_layout, build_comment  # noqa: E402


def main(argv: list[str]) -> int:
    cache_dir = setting.cache_dir
    explicit = [a for a in argv[1:] if not a.startswith("--")]
    if explicit:
        grupos = explicit
    else:
        grupos = sorted(
            p.stem for p in (cache_dir / "layout_csv").glob("*.csv")
            if (cache_dir / f"{p.stem}.duckdb").exists()
        )

    bad = 0
    for g in grupos:
        db = cache_dir / f"{g}.duckdb"
        if not db.exists():
            print(f"[{g}] SEM banco")
            continue
        layout = load_layout(cache_dir, g)
        try:
            conn = duckdb.connect(str(db), read_only=True)
            # integridade
            total = conn.execute("SELECT COUNT(*) FROM datasus_cache").fetchone()[0]
            rows = conn.execute(
                "SELECT column_name, comment FROM duckdb_columns() "
                "WHERE table_name = 'datasus_cache'"
            ).fetchall()
            conn.close()
        except Exception as exc:
            print(f"[{g}] ERRO integridade: {type(exc).__name__}: {exc}")
            bad += 1
            continue

        cols = {r[0] for r in rows}
        com_comentario = {r[0] for r in rows if r[1]}
        matched = [c for c in layout if c in cols]
        # Só é esperado comentário para campos que TENHAM descrição/exemplos no
        # layout. Campos com ambos vazios não geram comentário (correto).
        esperados = {
            c for c in matched
            if build_comment(layout[c].get("descricao", ""), layout[c].get("exemplos", ""))
        }
        sem = [c for c in esperados if c not in com_comentario]
        flag = "" if not sem else "  <-- FALTAM COMENTÁRIOS"
        print(
            f"[{g}] linhas={total:,} colunas={len(cols)} comentadas={len(com_comentario)} "
            f"layout_match={len(matched)} esperados={len(esperados)} sem_comentario={len(sem)}{flag}"
        )
        if sem:
            print(f"     sem comentário: {sem}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

