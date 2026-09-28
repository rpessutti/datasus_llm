#!/usr/bin/env python3
"""Testes SOMENTE-LEITURA profundos: integridade física + leitura de colunas comentadas.

Para cada grupo:
  * PRAGMA verify_* / integrity check quando disponível (apenas leitura);
  * SELECT de uma coluna comentada (a partir do comentário) sobre uma amostra;
  * verifica que comentários batem EXATAMENTE com o layout (conteúdo, não só presença).

Nada é gravado; todas as conexões são read_only=True.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import setting  # noqa: E402
from db.column_comments import load_layout, build_comment  # noqa: E402


def _integrity(conn) -> str:
    for pragma in ("PRAGMA database_size", "PRAGMA database_list"):
        try:
            conn.execute(pragma).fetchall()
        except Exception:
            pass
    # DuckDB não expõe integrity_check clássico; usa uma leitura forçada do catálogo.
    try:
        conn.execute("CHECKPOINT").fetchall()
        return "checkpoint(noop read-only)"
    except Exception:
        # Em read-only CHECKPOINT é rejeitado — esperado.
        return "read-only (sem checkpoint)"


def test_group(cache_dir: Path, g: str) -> bool:
    db = cache_dir / f"{g}.duckdb"
    if not db.exists():
        return True
    conn = duckdb.connect(str(db), read_only=True)
    try:
        layout = load_layout(cache_dir, g)
        if not layout:
            print(f"[{g}] sem layout; pulando teste de conteúdo")
            return True

        rows = dict(conn.execute(
            "SELECT column_name, comment FROM duckdb_columns() WHERE table_name='datasus_cache'"
        ).fetchall())

        ok = True
        lidas = 0
        divergentes = []
        for campo, meta in layout.items():
            if campo not in rows:
                continue
            esperado = build_comment(meta.get("descricao", ""), meta.get("exemplos", ""))
            if not esperado:
                # Não deve haver comentário.
                if rows[campo] is not None:
                    divergentes.append((campo, "esperado None", rows[campo]))
                    ok = False
                continue
            if rows[campo] != esperado:
                divergentes.append((campo, esperado, rows[campo]))
                ok = False
            # Leitura real da coluna (primeiras 3 linhas não nulas)
            try:
                conn.execute(f'SELECT "{campo}" FROM datasus_cache WHERE "{campo}" IS NOT NULL LIMIT 3').fetchall()
                lidas += 1
            except Exception as exc:
                print(f"[{g}] leitura falhou em {campo}: {type(exc).__name__}: {exc}")
                ok = False

        integ = _integrity(conn)
        print(f"[{g}] integridade={integ} colunas_legiveis={lidas} divergencias={len(divergentes)}")
        if divergentes:
            for d in divergentes[:5]:
                print(f"      DIVERGE: {d}")
        return ok
    finally:
        conn.close()


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
        print(f"FALHAS: {failures}")
        return 1
    print("TESTES PROFUNDOS DE LEITURA PASSARAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
