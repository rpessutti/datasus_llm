"""P0-A (passo 4): usa ``with_comments=True`` nas chamadas de
``labeled_columns_sql_hint`` em ``api/llm_service_parts/generate.py``, para que
os comentários de coluna (descrição/exemplos dos layouts) entrem no prompt.

Controlado por ``settings.schema_comments_enabled`` e truncado por
``settings.schema_comment_max_chars`` (P0-A, ver config/settings.py).

> ORDEM de reaplicação em checkout limpo: rode este script ANTES de
> ``dev_tools/apply_p0b.py`` (a P0-B edita os MESMOS trechos, adicionando
> ``rank_question``).

Rode:  python dev_tools/apply_p0a_generate.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dev_tools.safe_edit import replace_in_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

BACKUP_DIR = ROOT / "_backups" / "plano_prompt_cid_p0a"
TARGET = ROOT / "api" / "llm_service_parts" / "generate.py"


REPLACEMENTS = [
    # 1) Caminho normal (grupos relevantes identificados).
    (
        """            labeled_hint = column_dict.labeled_columns_sql_hint(
                grupos_labeled, limit_per_group=MAX_COLS_POR_GRUPO
            )""",
        """            # P0-A: inclui o comentário curto por coluna (descrição/exemplos
            # dos layouts oficiais) no rótulo por grupo. Controlado por
            # DATASUS_SCHEMA_COMMENTS_ENABLED e truncado por
            # DATASUS_SCHEMA_COMMENT_MAX_CHARS (ver config/settings.py).
            labeled_hint = column_dict.labeled_columns_sql_hint(
                grupos_labeled,
                limit_per_group=MAX_COLS_POR_GRUPO,
                with_comments=getattr(self.settings, "schema_comments_enabled", False),
                max_comment_chars=getattr(self.settings, "schema_comment_max_chars", 90),
            )""",
    ),
    # 2) Fallback (nenhum grupo identificado).
    (
        """            labeled_hint = column_dict.labeled_columns_sql_hint(limit_per_group=40)""",
        """            # P0-A: comentário por coluna também no fallback (best-effort).
            # Reduz de 40 -> 25 (igual ao caminho principal) porque, COM os
            # comentários (P0-A), cada entrada fica ~3x maior; 40 colunas/grupo
            # em vários grupos inflaria demais o prompt e poderia reativar o
            # APITimeoutError. 25 preserva cobertura sem estourar o prefill.
            labeled_hint = column_dict.labeled_columns_sql_hint(
                limit_per_group=25,
                with_comments=getattr(self.settings, "schema_comments_enabled", False),
                max_comment_chars=getattr(self.settings, "schema_comment_max_chars", 90),
            )""",
    ),
]

EXPECT = ["with_comments=getattr(self.settings, \"schema_comments_enabled\"",
          "max_comment_chars=getattr(self.settings, \"schema_comment_max_chars\""]


def main() -> int:
    res = replace_in_file(TARGET, REPLACEMENTS, expect=EXPECT, backup_dir=BACKUP_DIR)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res["ok"]:
        print("P0-A generate: FALHOU (arquivo restaurado se necessário)", file=sys.stderr)
        return 1
    print("P0-A generate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
