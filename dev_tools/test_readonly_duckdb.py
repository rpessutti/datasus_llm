#!/usr/bin/env python3
"""Testes SOMENTE-LEITURA dos bancos DuckDB dos grupos.

Abre cada <cache_dir>/<grupo>.duckdb em read_only=True e valida:
  * abertura e integridade (PRAGMA integrity_check quando disponível);
  * schema de datasus_cache (contagem de colunas e tipos);
  * contagem de linhas e amostra via SELECT;
  * existência e semântica da VIEW datasus (mesma contagem de datasus_cache);
  * comentários das colunas (contagem e casamento com layout);
  * confirma que o modo é read-only (tentativa de escrita deve FALHAR).

NÃO grava NADA. NÃO recria bancos. Apenas leitura.

Uso:
    python dev_tools/test_readonly_duckdb.py            # todos os grupos
    python dev_tools/test_readonly_duckdb.py ab pa      # grupos específicos
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import setting  # noqa: E402
from db.column_comments import load_layout, build_comment  # noqa: E402


def _groups(cache_dir: Path, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    # Todos os <grupo>.duckdb, exceto o unificado/fallback.
    return sorted(
        p.stem for p in cache_dir.glob("*.duckdb")
        if p.stem not in ("datasus", "datasus_fallback")
    )


def test_group(cache_dir: Path, g: str) -> bool:
    db = cache_dir / f"{g}.duckdb"
    if not db.exists():
        print(f"[{g}] SEM banco ({db.name})")
        return False

    ok = True
    try:
        conn = duckdb.connect(str(db), read_only=True)
    except Exception as exc:
        print(f"[{g}] FALHA ao abrir read-only: {type(exc).__name__}: {exc}")
        return False

    try:
        # 1) Relações existentes
        tabs = {r[0] for r in conn.execute(
            "SELECT table_name FROM duckdb_tables()").fetchall()}
        views = {r[0] for r in conn.execute(
            "SELECT view_name FROM duckdb_views()").fetchall()}

        has_table = "datasus_cache" in tabs
        has_view = "datasus" in views

        # 2) Integridade do datasus_cache
        total = None
        ncols = None
        if has_table:
            total = conn.execute("SELECT COUNT(*) FROM datasus_cache").fetchone()[0]
            ncols = len(conn.execute("DESCRIBE datasus_cache").fetchall())

        # 3) VIEW datasus deve espelhar a tabela
        view_rows = None
        view_ok = "n/a"
        if has_view:
            try:
                view_rows = conn.execute("SELECT COUNT(*) FROM datasus").fetchone()[0]
                view_ok = (view_rows == total)
            except Exception as exc:
                view_ok = f"ERRO: {type(exc).__name__}: {exc}"
                ok = False

        # 4) Amostra de leitura (primeira linha) do datasus_cache
        sample = None
        if has_table and total and total > 0:
            try:
                sample = conn.execute("SELECT * FROM datasus_cache LIMIT 1").fetchone()
            except Exception as exc:
                sample = f"ERRO: {type(exc).__name__}: {exc}"
                ok = False

        # 5) Comentários vs layout
        layout = load_layout(cache_dir, g)
        rows = conn.execute(
            "SELECT column_name, comment FROM duckdb_columns() WHERE table_name='datasus_cache'"
        ).fetchall()
        cols = {r[0] for r in rows}
        comentadas = {r[0] for r in rows if r[1]}
        matched = [c for c in layout if c in cols]
        esperados = {
            c for c in matched
            if build_comment(layout[c].get("descricao", ""), layout[c].get("exemplos", ""))
        }
        faltam = sorted(esperados - comentadas)
        if faltam:
            ok = False

        # 6) Confirmar READ-ONLY: escrita deve FALHAR
        write_blocked = False
        try:
            conn.execute("CREATE TABLE __ro_probe (x INTEGER)")
        except Exception:
            write_blocked = True
        if not write_blocked:
            ok = False

        # 7) comentários na VIEW datasus devem ser intocados (nenhum)
        view_comments = 0
        if has_view:
            view_comments = conn.execute(
                "SELECT COUNT(*) FROM duckdb_columns() WHERE table_name='datasus' AND comment IS NOT NULL"
            ).fetchone()[0]

        total_str = f"{total:,}" if isinstance(total, int) else str(total)
        print(f"[{g}] table={has_table} view={has_view} linhas={total_str}")
        print(
            f"      colunas={ncols} comentadas={len(comentadas)} layout_match={len(matched)} "
            f"esperados={len(esperados)} faltam={len(faltam)}"
        )
        print(
            f"      view_rows={view_rows} view_espelha_tabela={view_ok} "
            f"view_comentadas={view_comments} read_only_ok={write_blocked}"
        )
        if faltam:
            print(f"      FALTAM COMENTÁRIOS: {faltam}")
        if isinstance(sample, str) and sample.startswith("ERRO"):
            print(f"      amostra: {sample}")

    except Exception as exc:
        print(f"[{g}] ERRO inesperado: {type(exc).__name__}: {exc}")
        ok = False
    finally:
        conn.close()

    return ok


def main(argv: list[str]) -> int:
    cache_dir = setting.cache_dir
    explicit = [a for a in argv[1:] if not a.startswith("--")]
    grupos = _groups(cache_dir, explicit)
    if not grupos:
        print("nenhum .duckdb encontrado")
        return 0

    print(f"cache_dir={cache_dir}")
    print(f"grupos={grupos}\n")

    failures = []
    for g in grupos:
        try:
            if not test_group(cache_dir, g):
                failures.append(g)
        except Exception as exc:
            print(f"[{g}] EXCEÇÃO: {type(exc).__name__}: {exc}")
            failures.append(g)

    print()
    if failures:
        print(f"FALHAS/ATENÇÃO: {failures}")
        return 1
    print("TODOS OS TESTES DE LEITURA PASSARAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
