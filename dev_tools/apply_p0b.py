"""P0-B: priorização lexical de grupos/colunas no prompt.

Aplica (de forma segura, com backup + verificação + auto-restore):

  1. ``db/column_dictionary.py``: ``labeled_columns``/``labeled_columns_sql_hint``
     ganham o parâmetro ``rank_question`` (reordena as colunas por relevância
     ANTES do corte de ``limit_per_group``).
  2. ``api/llm_service_parts/generate.py``: reordena os grupos (``rank_groups``)
     antes do corte de ``MAX_GRUPOS_LABELED`` e passa ``rank_question`` às
     chamadas de ``labeled_columns_sql_hint`` (caminho principal e fallback),
     condicionado a ``settings.lexical_rank_enabled``.

O módulo ``db/lexical_rank.py`` é NOVO (criado à parte); este script cobre as
EDIÇÕES nos arquivos existentes.

Rode:  python dev_tools/apply_p0b.py
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

BACKUP_DIR = ROOT / "_backups" / "plano_prompt_cid_p0b"

COLUMN_DICT = ROOT / "db" / "column_dictionary.py"
GENERATE = ROOT / "api" / "llm_service_parts" / "generate.py"


# ---------------------------------------------------------------------------
# 1) column_dictionary.py: labeled_columns(..., rank_question=...)
# ---------------------------------------------------------------------------
CD_SIG_OLD = '''    def labeled_columns(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
        with_comments: bool = False,
        max_comment_chars: int = 90,
    ) -> List[str]:'''
CD_SIG_NEW = '''    def labeled_columns(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
        with_comments: bool = False,
        max_comment_chars: int = 90,
        rank_question: Optional[str] = None,
    ) -> List[str]:'''

CD_BODY_OLD = '''        out: List[str] = []
        for grupo in sorted(set(grupos)):
            cols = self.by_group.get(grupo, [])
            if limit_per_group is not None and limit_per_group > 0:
                cols = cols[:limit_per_group]'''
CD_BODY_NEW = '''        ranker = None
        if rank_question:
            try:
                from db.lexical_rank import rank_columns as _rank_columns
                ranker = _rank_columns
            except Exception:  # pragma: no cover - degradação segura
                ranker = None
        out: List[str] = []
        for grupo in sorted(set(grupos)):
            cols = self.by_group.get(grupo, [])
            if ranker is not None:
                names = [c["column"] for c in cols]
                try:
                    ordered = ranker(rank_question, names, self.comment_for_column)
                except Exception:  # pragma: no cover
                    ordered = names
                pos = {n: i for i, n in enumerate(ordered)}
                cols = sorted(cols, key=lambda c: pos.get(c["column"], len(pos)))
            if limit_per_group is not None and limit_per_group > 0:
                cols = cols[:limit_per_group]'''

CD_HINT_OLD = '''    def labeled_columns_sql_hint(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
        with_comments: bool = False,
        max_comment_chars: int = 90,
    ) -> str:
        return ", ".join(
            self.labeled_columns(
                grupos,
                limit_per_group,
                with_comments=with_comments,
                max_comment_chars=max_comment_chars,
            )
        )'''
CD_HINT_NEW = '''    def labeled_columns_sql_hint(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
        with_comments: bool = False,
        max_comment_chars: int = 90,
        rank_question: Optional[str] = None,
    ) -> str:
        return ", ".join(
            self.labeled_columns(
                grupos,
                limit_per_group,
                with_comments=with_comments,
                max_comment_chars=max_comment_chars,
                rank_question=rank_question,
            )
        )'''


# ---------------------------------------------------------------------------
# 2) generate.py: rank_groups + rank_question
# ---------------------------------------------------------------------------
GEN_OLD = '''            MAX_GRUPOS_LABELED = 6
            MAX_COLS_POR_GRUPO = 25
            if len(relevant_grupos) > MAX_GRUPOS_LABELED:
                grupos_labeled = relevant_grupos[:MAX_GRUPOS_LABELED]
                LOGGER.info(
                    "labeled_hint reduzido para %d de %d grupos (CORREÇÃO 1)",
                    len(grupos_labeled),
                    len(relevant_grupos),
                )
            else:
                grupos_labeled = relevant_grupos
            # P0-A: inclui o comentário curto por coluna (descrição/exemplos
            # dos layouts oficiais) no rótulo por grupo. Controlado por
            # DATASUS_SCHEMA_COMMENTS_ENABLED e truncado por
            # DATASUS_SCHEMA_COMMENT_MAX_CHARS (ver config/settings.py).
            labeled_hint = column_dict.labeled_columns_sql_hint(
                grupos_labeled,
                limit_per_group=MAX_COLS_POR_GRUPO,
                with_comments=getattr(self.settings, "schema_comments_enabled", False),
                max_comment_chars=getattr(self.settings, "schema_comment_max_chars", 90),
            )'''
GEN_NEW = '''            MAX_GRUPOS_LABELED = 6
            MAX_COLS_POR_GRUPO = 25
            # >>> P0-B: priorização lexical de GRUPOS <<<
            # Antes, o corte de 6 pegaria os PRIMEIROS da ordem alfabética
            # (``sorted(grupos)``), podendo descartar grupos relevantes. Agora
            # REORDENAMOS por relevância lexical à pergunta (determinístico,
            # sem ONNX). NÃO removemos nenhum grupo: o corte continua sendo
            # MAX_GRUPOS_LABELED — apenas escolhe os mais prováveis.
            lexical_rank = getattr(self.settings, "lexical_rank_enabled", False)
            if lexical_rank:
                try:
                    from db.lexical_rank import rank_groups as _rank_groups
                    relevant_grupos = _rank_groups(question, relevant_grupos, column_dict)
                except Exception as _rank_err:  # pragma: no cover - best-effort
                    LOGGER.warning("P0-B: rank_groups falhou (%s); mantendo ordem original", _rank_err)
            if len(relevant_grupos) > MAX_GRUPOS_LABELED:
                grupos_labeled = relevant_grupos[:MAX_GRUPOS_LABELED]
                LOGGER.info(
                    "labeled_hint reduzido para %d de %d grupos (CORREÇÃO 1%s)",
                    len(grupos_labeled),
                    len(relevant_grupos),
                    " + P0-B lexical" if lexical_rank else "",
                )
            else:
                grupos_labeled = relevant_grupos
            # P0-A: inclui o comentário curto por coluna (descrição/exemplos
            # dos layouts oficiais) no rótulo por grupo. Controlado por
            # DATASUS_SCHEMA_COMMENTS_ENABLED e truncado por
            # DATASUS_SCHEMA_COMMENT_MAX_CHARS (ver config/settings.py).
            # P0-B: ``rank_question`` reordena as colunas de cada grupo por
            # relevância ANTES do corte de 25 (as mais prováveis sobrevivem).
            labeled_hint = column_dict.labeled_columns_sql_hint(
                grupos_labeled,
                limit_per_group=MAX_COLS_POR_GRUPO,
                with_comments=getattr(self.settings, "schema_comments_enabled", False),
                max_comment_chars=getattr(self.settings, "schema_comment_max_chars", 90),
                rank_question=question if lexical_rank else None,
            )'''

GEN_FALLBACK_OLD = '''            labeled_hint = column_dict.labeled_columns_sql_hint(
                limit_per_group=25,
                with_comments=getattr(self.settings, "schema_comments_enabled", False),
                max_comment_chars=getattr(self.settings, "schema_comment_max_chars", 90),
            )'''
GEN_FALLBACK_NEW = '''            labeled_hint = column_dict.labeled_columns_sql_hint(
                limit_per_group=25,
                with_comments=getattr(self.settings, "schema_comments_enabled", False),
                max_comment_chars=getattr(self.settings, "schema_comment_max_chars", 90),
                rank_question=question if getattr(self.settings, "lexical_rank_enabled", False) else None,
            )'''


def main() -> int:
    ok = True

    res_cd = replace_in_file(
        COLUMN_DICT,
        [(CD_SIG_OLD, CD_SIG_NEW), (CD_BODY_OLD, CD_BODY_NEW), (CD_HINT_OLD, CD_HINT_NEW)],
        expect=["rank_question: Optional[str] = None", "from db.lexical_rank import rank_columns"],
        backup_dir=BACKUP_DIR,
    )
    print("column_dictionary.py:", json.dumps(res_cd, ensure_ascii=False))
    ok = ok and res_cd["ok"]

    res_gen = replace_in_file(
        GENERATE,
        [(GEN_OLD, GEN_NEW), (GEN_FALLBACK_OLD, GEN_FALLBACK_NEW)],
        expect=["from db.lexical_rank import rank_groups", "rank_question=question if lexical_rank else None"],
        backup_dir=BACKUP_DIR,
    )
    print("generate.py:", json.dumps(res_gen, ensure_ascii=False))
    ok = ok and res_gen["ok"]

    if not ok:
        print("P0-B: FALHOU (arquivos restaurados se necessário)", file=sys.stderr)
        return 1
    print("P0-B OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
