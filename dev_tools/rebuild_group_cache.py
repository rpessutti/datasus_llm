#!/usr/bin/env python3
"""Reconstrói a tabela datasus_cache de um grupo a partir dos Parquets já cacheados.

Por que isso é necessário:
  O bug de ingestão incremental (view ``temp_batch`` sem ``filename = true``)
  deixou ``filename``/``parquet_path``/``parquet_year``/``parquet_month`` NULL
  em bilhões de linhas. Como essas colunas NÃO podem ser reconstruídas a partir
  das próprias linhas (a informação do arquivo de origem foi perdida no INSERT),
  a ÚNICA forma de recuperar o período correto é reler os Parquets de origem e
  materializar a tabela novamente — agora com os metadados corretos.

Este script NÃO apaga os Parquets: apenas recria a tabela DuckDB do grupo a
partir deles, de forma atômica (CREATE OR REPLACE TABLE ... AS SELECT).

Uso:
    python dev_tools/rebuild_group_cache.py rj            # reconstrói 1 grupo
    python dev_tools/rebuild_group_cache.py pa sp         # reconstrói vários
    python dev_tools/rebuild_group_cache.py --all         # todos com <grupo>.duckdb
    python dev_tools/rebuild_group_cache.py pa --dry-run  # lista o que faria

Após rodar, reinicie a aplicação. A view <grupo>.datasus é recriada
automaticamente na inicialização (reopen/finalize).
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.duckdb_config import apply_duckdb_experimental_settings

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"


def _parquet_files(group: str) -> list[Path]:
    return sorted((CACHE_DIR / group).rglob("*.parquet"))


def rebuild_group(group: str, dry_run: bool) -> None:
    db_path = CACHE_DIR / f"{group}.duckdb"
    files = _parquet_files(group)
    if not db_path.exists():
        print(f"[{group}] SKIP: {db_path} não existe")
        return
    if not files:
        print(f"[{group}] SKIP: nenhum Parquet em {CACHE_DIR / group}")
        return

    print(f"[{group}] arquivos={len(files)} db={db_path.name}")

    if dry_run:
        print(f"[{group}] DRY-RUN: reconstruiria a tabela a partir de {len(files)} Parquets")
        return

    conn = duckdb.connect(":memory:")
    # Grupos grandes (ex.: bi com ~929M linhas, pa com ~1,5bi) estouravam a RAM
    # porque o rebuild rodava sem limite de memória e sem spill em disco. Aqui
    # configuramos limite + diretório temporário para permitir a materialização
    # via spill (arquivos temporários), evitando o OOM que interrompia o processo.
    import os as _os
    mem = _os.getenv("DATASUS_MEMORY_LIMIT", "4GB")
    threads = _os.getenv("DATASUS_THREADS", "4")
    spill = CACHE_DIR / "duckdb_spill"
    try:
        spill.mkdir(parents=True, exist_ok=True)
        conn.execute(f"SET temp_directory='{spill}'")
        conn.execute(f"SET memory_limit='{mem}'")
        conn.execute(f"SET threads={threads}")
    except Exception as exc:
        print(f"[{group}] aviso: não foi possível configurar memória/spill: {exc}")
    # VSS/HNSW: habilita a persistência EXPERIMENTAL dos índices HNSW antes de
    # regenerar o cache no banco persistente (.duckdb).
    apply_duckdb_experimental_settings(conn, label=f"grupo {group}")
    conn.execute(f"ATTACH '{db_path}' AS \"{group}\" (READ_WRITE)")

    # Materializa a tabela VAZIA com o schema e as colunas derivadas de período.
    first = str(files[0]).replace("'", "''")
    conn.execute(
        f'CREATE OR REPLACE TABLE "{group}".datasus_cache AS '
        "SELECT *, filename AS parquet_path, "
        "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
        "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year, "
        "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
        "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month "
        f"FROM read_parquet(['{first}'], union_by_name = true, filename = true) LIMIT 0"
    )

    # Insere ARQUIVO POR ARQUIVO (lotes), evitando picos de memória que causavam
    # OOM nos grupos grandes. Cada lote deriva o período do caminho do parquet.
    import re as _re
    batch_size = 100
    for i in range(0, len(files), batch_size):
        batch = files[i:i + batch_size]
        paths_sql = ", ".join("'" + str(p).replace("'", "''") + "'" for p in batch)
        table_cols = [r[0] for r in conn.execute(f'DESCRIBE "{group}".datasus_cache').fetchall()]

        batch_cols = [r[0] for r in conn.execute(
            f"DESCRIBE SELECT * FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
        ).fetchall()]
        existing = {c.upper() for c in table_cols}
        for c in batch_cols:
            if c.upper() not in existing:
                ctype = conn.execute(
                    f'SELECT typeof("{c}") FROM read_parquet([{paths_sql}], union_by_name = true, filename = true) LIMIT 1'
                ).fetchone()[0]
                conn.execute(f'ALTER TABLE "{group}".datasus_cache ADD COLUMN "{c}" {ctype}')
                table_cols.append(c)
                existing.add(c.upper())

        batch_by_upper = {c.upper(): c for c in batch_cols}
        proj = []
        for col in table_cols:
            esc = col.replace('"', '""')
            if col == "parquet_path":
                proj.append('filename AS parquet_path')
            elif col == "parquet_year":
                proj.append(
                    "COALESCE(2000 + CAST(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 1), '') AS INTEGER), "
                    "TRY_CAST(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 1) AS INTEGER)) AS parquet_year"
                )
            elif col == "parquet_month":
                proj.append(
                    "COALESCE(NULLIF(regexp_extract(filename, '(\\d{2})(0[1-9]|1[0-2])[a-z]*\\.parquet$', 2), ''), "
                    "NULLIF(regexp_extract(filename, '/(\\d{4})/(0[1-9]|1[0-2])/', 2), '')) AS parquet_month"
                )
            elif col.upper() in batch_by_upper:
                bc = batch_by_upper[col.upper()].replace('"', '""')
                proj.append(f'"{bc}"')
            else:
                proj.append(f'CAST(NULL AS VARCHAR) AS "{esc}"')

        conn.execute(
            f'INSERT INTO "{group}".datasus_cache '
            f"SELECT {', '.join(proj)} "
            f"FROM read_parquet([{paths_sql}], union_by_name = true, filename = true)"
        )
        print(f"[{group}] {min(i + batch_size, len(files))}/{len(files)} arquivos", flush=True)

    # Recria a view de compatibilidade usada pelo restante do código.
    conn.execute(f'CREATE OR REPLACE VIEW "{group}".datasus AS SELECT * FROM "{group}".datasus_cache')

    total, sem_fn, sem_pp, sem_py, sem_pm = conn.execute(
        f'SELECT COUNT(*), '
        'COUNT(CASE WHEN filename IS NULL THEN 1 END), '
        'COUNT(CASE WHEN parquet_path IS NULL THEN 1 END), '
        'COUNT(CASE WHEN parquet_year IS NULL THEN 1 END), '
        'COUNT(CASE WHEN parquet_month IS NULL THEN 1 END) '
        f'FROM "{group}".datasus_cache'
    ).fetchone()
    print(
        f"[{group}] reconstruído: total={total:,} "
        f"NULL(filename)={sem_fn:,} NULL(parquet_path)={sem_pp:,} "
        f"NULL(parquet_year)={sem_py:,} NULL(parquet_month)={sem_pm:,}"
    )
    if sem_fn or sem_pp or sem_py or sem_pm:
        print(f"[{group}] ATENÇÃO: campos de metadados com NULL remanescentes "
              f"(verifique nomes de arquivo sem AAMM e pastas ano/mês)")

    conn.close()
    print(f"[{group}] OK")


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    if "--all" in argv or not args:
        groups = sorted(
            p.stem for p in CACHE_DIR.glob("*.duckdb")
            if p.stem not in ("datasus", "datasus_fallback") and _parquet_files(p.stem)
        )
    else:
        groups = args

    print(f"grupos a reconstruir: {groups} (dry_run={dry_run})")
    for g in groups:
        try:
            rebuild_group(g, dry_run)
        except Exception as exc:
            print(f"[{g}] ERRO: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))