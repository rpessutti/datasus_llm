"""Mede o impacto (chars) da FASE P0-A nos rótulos de coluna do prompt.

Não abre DuckDB: lê os caches de colunas por grupo (`<cache_dir>/colunas_cache/*.json`)
e os layouts oficiais (`<cache_dir>/layout_csv/<grupo>.csv`) e compara o
``labeled_columns_sql_hint`` SEM e COM comentários.

Uso:
    python dev_tools/measure_prompt_comments.py

Saída: por grupo (colunas, chars sem/com comentários, delta) e o total; além da
contagem de colunas que efetivamente GANHARAM comentário (cobertura).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import setting  # noqa: E402
from db.column_dictionary import ColumnDictionary  # noqa: E402
from db.lexical_rank import rank_columns, rank_groups  # noqa: E402

# Grupos reais (exclui 'parquet' e duplicatas com sufixo '[n]').
RESERVED = {"datasus", "datasus_fallback", "parquet", "cache", "tmp"}


def _is_valid_group(name: str) -> bool:
    if name in RESERVED or "[" in name or "]" in name:
        return False
    return name.isidentifier()


def _load_columns(cache_dir: Path) -> dict[str, list[dict[str, str]]]:
    colunas_dir = cache_dir / "colunas_cache"
    out: dict[str, list[dict[str, str]]] = {}
    if not colunas_dir.is_dir():
        return out
    for f in sorted(colunas_dir.glob("*.json")):
        grupo = f.stem
        if not _is_valid_group(grupo):
            continue
        try:
            payload = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        cols = payload.get("columns") if isinstance(payload, dict) else payload
        if cols:
            out[grupo] = cols
    return out


def main() -> int:
    cache_dir = setting.cache_dir
    grupo_columns = _load_columns(cache_dir)
    if not grupo_columns:
        print(f"nenhum cache de colunas válido em {cache_dir / 'colunas_cache'}")
        return 1

    limit_per_group = 25  # MAX_COLS_POR_GRUPO (generate.py)
    max_chars = setting.schema_comment_max_chars

    total_sem = 0
    total_com = 0
    total_cols_sem = 0
    total_cols_com = 0
    com_comentario = 0
    sem_comentario = 0

    print(f"cache_dir={cache_dir}")
    print(f"limit_per_group={limit_per_group}  max_comment_chars={max_chars}")
    print(f"{'grupo':>6} | {'cols':>5} | {'sem':>7} | {'com':>7} | {'delta':>7} | {'coment.':>7}")
    print("-" * 60)

    for grupo in sorted(grupo_columns):
        one = {grupo: grupo_columns[grupo]}
        cd = ColumnDictionary.from_grupo_columns(one, cache_dir=str(cache_dir))
        sem = cd.labeled_columns_sql_hint([grupo], limit_per_group=limit_per_group)
        com = cd.labeled_columns_sql_hint(
            [grupo], limit_per_group=limit_per_group,
            with_comments=True, max_comment_chars=max_chars,
        )
        cols = cd.columns_for_group(grupo)[:limit_per_group]
        n_com = sum(1 for c in cols if cd.comment_for_column(c))
        total_cols_sem += len(cols)
        # colunas COM = inclui as linhas extras dos comentários (mesmas colunas)
        com_comentario += n_com
        sem_comentario += (len(cols) - n_com)

        total_sem += len(sem)
        total_com += len(com)
        print(
            f"{grupo:>6} | {len(cols):>5} | {len(sem):>7} | {len(com):>7} | "
            f"{len(com) - len(sem):>+7} | {n_com:>3}/{len(cols):<3}"
        )

    print("-" * 60)
    print(f"{'TOTAL':>6} | {total_cols_sem:>5} | {total_sem:>7} | {total_com:>7} | "
          f"{total_com - total_sem:>+7} | {com_comentario:>3}/{total_cols_sem:<3}")
    print()
    print(f"chars sem comentários : {total_sem}")
    print(f"chars com comentários : {total_com}")
    delta = total_com - total_sem
    pct = (delta / total_sem * 100) if total_sem else 0.0
    print(f"delta                 : {delta:+d} chars ({pct:+.1f}%)")
    print(f"cobertura de comentário: {com_comentario}/{total_cols_sem} "
          f"({(com_comentario / total_cols_sem * 100) if total_cols_sem else 0:.1f}%)")

    _measure_lexical_rank(cache_dir, grupo_columns)
    return 0


def _measure_lexical_rank(cache_dir: Path, grupo_columns: dict) -> None:
    """Demonstra o efeito da P0-B: reordenação de grupos e colunas."""
    print()
    print("=" * 72)
    print("P0-B: priorização lexical (rank por relevância à pergunta)")
    print("=" * 72)

    full_dict = ColumnDictionary.from_grupo_columns(grupo_columns, cache_dir=str(cache_dir))

    perguntas = [
        "quantos casos por sexo?",
        "casos por municipio de residencia",
        "qual o valor total pago",
        "obitos por faixa de idade",
    ]
    grupos_orig = list(full_dict.by_group.keys())

    for pergunta in perguntas:
        ranked_g = rank_groups(pergunta, grupos_orig, full_dict)
        print(f"\nPergunta: {pergunta!r}")
        print(f"  grupos (top 6 de {len(grupos_orig)}): {ranked_g[:6]}")
        top1 = ranked_g[0]
        cols = full_dict.columns_for_group(top1)
        ranked_c = rank_columns(pergunta, cols, full_dict.comment_for_column)
        print(f"  grupo '{top1}': colunas top 8 -> {ranked_c[:8]}")


if __name__ == "__main__":
    raise SystemExit(main())
