"""Aplica a FASE P0-A com edição segura (backup + verificação + auto-restore).

P0-A: injetar os comentários de coluna (descrição/exemplos dos layouts) no
prompt do LLM. Os metadados já existiam (db/column_comments.py) mas eram
DESCARTADOS — agora entram no rótulo por grupo, melhorando a escolha de COLUNA.

Passos:
  1. config/settings.py            -> flags (comments/lexical/cid_discovery)
  2. db/column_dictionary.py       -> comentário por coluna (via column_comments)

Rode:  python dev_tools/apply_p0a.py
Cada passo imprime ok/backup/verified/restored. Em falha de verificação, o
arquivo é RESTAURADO automaticamente do backup (ver dev_tools/safe_edit.py).
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


# ---------------------------------------------------------------------------
# 1) config/settings.py
# ---------------------------------------------------------------------------
SETTINGS_OLD = '''    # Parâmetros de geração de gráficos
    llm_chart_temperature = float(os.getenv("LLM_CHART_TEMPERATURE", "0.2"))
    llm_chart_max_tokens = int(os.getenv("LLM_CHART_MAX_TOKENS", "2048"))


setting = Settings()
DATASUS_DATA_DIR = setting.data_dir'''

SETTINGS_NEW = '''    # Parâmetros de geração de gráficos
    llm_chart_temperature = float(os.getenv("LLM_CHART_TEMPERATURE", "0.2"))
    llm_chart_max_tokens = int(os.getenv("LLM_CHART_MAX_TOKENS", "2048"))

    # ------------------------------------------------------------------
    # P0-A: comentários de coluna (descrição/exemplos dos layouts) no prompt.
    # Os metadados já existem (db/column_comments.py) e eram DESCARTADOS.
    # Injetá-los no prompt melhora a escolha de coluna pelo LLM.
    # ------------------------------------------------------------------
    schema_comments_enabled = (
        os.getenv("DATASUS_SCHEMA_COMMENTS_ENABLED", "true").casefold()
        in {"1", "true", "yes", "on"}
    )
    # Truncamento (chars) do comentário por coluna — evita inflar o prompt.
    schema_comment_max_chars = int(os.getenv("DATASUS_SCHEMA_COMMENT_MAX_CHARS", "90"))

    # ------------------------------------------------------------------
    # P0-B: priorização lexical de colunas/grupos (determinística, sem ONNX).
    # ------------------------------------------------------------------
    lexical_rank_enabled = (
        os.getenv("DATASUS_LEXICAL_RANK_ENABLED", "true").casefold()
        in {"1", "true", "yes", "on"}
    )

    # ------------------------------------------------------------------
    # P2: descoberta de CID retornando TODOS os códigos da doença.
    # ``cid_discovery_max`` é um TETO DE SANIDADE (não de recall).
    # ------------------------------------------------------------------
    cid_discovery_max = int(os.getenv("DATASUS_CID_DISCOVERY_MAX", "200"))


setting = Settings()
DATASUS_DATA_DIR = setting.data_dir'''


def apply_settings() -> dict:
    return replace_in_file(
        ROOT / "config" / "settings.py",
        [(SETTINGS_OLD, SETTINGS_NEW)],
        expect=["schema_comments_enabled", "lexical_rank_enabled",
                "schema_comment_max_chars", "cid_discovery_max",
                "DATASUS_DATA_DIR = setting.data_dir"],
        backup_dir=BACKUP_DIR,
    )


# ---------------------------------------------------------------------------
# 2) db/column_dictionary.py — comentário por coluna (P0-A)
# ---------------------------------------------------------------------------
CD_DOC_OLD = '''- Persistência opcional em DuckDB (tabela ``column_dictionary``).
- Dicionários de tradução código->rótulo do DATASUS (sexo, raça/cor, etc.).
"""'''

CD_DOC_NEW = '''- Persistência opcional em DuckDB (tabela ``column_dictionary``).
- Dicionários de tradução código->rótulo do DATASUS (sexo, raça/cor, etc.).
- P0-A: comentários por coluna (descrição/exemplos dos layouts) para INJETAR
  no prompt do LLM, melhorando a escolha de COLUNA (que hoje só recebe nome+tipo).
"""'''

CD_IMPORTS_OLD = '''from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple'''

CD_IMPORTS_NEW = '''from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# P0-A: comentários por coluna carregados dos layouts oficiais (best-effort).
try:
    from db.column_comments import load_layout, build_comment
except Exception:  # pragma: no cover - ambientes sem o módulo
    load_layout = None  # type: ignore[assignment]
    build_comment = None  # type: ignore[assignment]'''

CD_DATACLASS_OLD = '''    by_group: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    signature: str = ""

    # campos derivados (lazy)'''

CD_DATACLASS_NEW = '''    by_group: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    signature: str = ""
    # P0-A: mapa ``COLUNA_UPPER -> comentário`` (descrição/exemplos do layout).
    comments: Dict[str, str] = field(default_factory=dict)

    # campos derivados (lazy)'''

CD_FROM_OLD = '''    @classmethod
    def from_grupo_columns(
        cls,
        grupo_columns: Mapping[str, Sequence[Mapping[str, str]]],
        grupos: Optional[Iterable[str]] = None,
    ) -> "ColumnDictionary":
        by_group: Dict[str, List[Dict[str, str]]] = {}
        selected = set(grupos) if grupos is not None else None
        for grupo, cols in (grupo_columns or {}).items():
            if selected is not None and grupo not in selected:
                continue
            norm: List[Dict[str, str]] = []
            for c in cols or []:
                name = (c.get("column") or "").strip()
                if not name:
                    continue
                norm.append({"column": name, "type": str(c.get("type") or "")})
            by_group[grupo] = norm
        signature = _column_signature(by_group)
        return cls(by_group=by_group, signature=signature)'''

CD_FROM_NEW = '''    @classmethod
    def from_grupo_columns(
        cls,
        grupo_columns: Mapping[str, Sequence[Mapping[str, str]]],
        grupos: Optional[Iterable[str]] = None,
        cache_dir: Optional[str] = None,
    ) -> "ColumnDictionary":
        by_group: Dict[str, List[Dict[str, str]]] = {}
        selected = set(grupos) if grupos is not None else None
        for grupo, cols in (grupo_columns or {}).items():
            if selected is not None and grupo not in selected:
                continue
            norm: List[Dict[str, str]] = []
            for c in cols or []:
                name = (c.get("column") or "").strip()
                if not name:
                    continue
                norm.append({"column": name, "type": str(c.get("type") or "")})
            by_group[grupo] = norm
        signature = _column_signature(by_group)
        comments = cls._load_comments(by_group, cache_dir)
        return cls(by_group=by_group, signature=signature, comments=comments)

    @staticmethod
    def _load_comments(
        by_group: Mapping[str, Sequence[Mapping[str, str]]],
        cache_dir: Optional[str],
    ) -> Dict[str, str]:
        """P0-A: carrega comentários (descrição/exemplos do layout) por coluna.

        Best-effort e SILENCIOSO: se ``cache_dir``/``load_layout`` não estiverem
        disponíveis (ou os layouts não existirem), devolve ``{}`` e o
        comportamento é idêntico ao anterior. O casamento é por NOME EXATO
        (coluna do grupo == campo do layout).
        """
        if not cache_dir or load_layout is None or build_comment is None:
            return {}
        comments: Dict[str, str] = {}
        # Cache de layout por grupo para não reler o CSV a cada coluna.
        for grupo, cols in (by_group or {}).items():
            try:
                layout = load_layout(cache_dir, grupo)
            except Exception:  # pragma: no cover - robustez
                layout = {}
            if not layout:
                continue
            for c in cols or []:
                name = (c.get("column") or "").strip()
                if not name:
                    continue
                meta = layout.get(name)
                if not meta:
                    continue
                try:
                    comment = build_comment(meta.get("descricao", ""), meta.get("exemplos", ""))
                except Exception:  # pragma: no cover
                    comment = ""
                up = name.upper()
                if comment and up not in comments:
                    comments[up] = comment
        return comments'''

CD_API_OLD = '''    def labeled_columns(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
    ) -> List[str]:
        """Colunas rotuladas com o grupo de origem (ex.: ``"SEXOPAC" [ps]``).

        Quando ``grupos`` é None, usa todos os grupos; ``limit_per_group``
        limita quantas colunas de cada grupo entram (útil no fallback para não
        inflar o prompt com centenas/milhares de entradas).
        """
        if grupos is None:
            self._ensure()
            grupos = sorted(self.by_group.keys())
        out: List[str] = []
        for grupo in sorted(set(grupos)):
            cols = self.by_group.get(grupo, [])
            if limit_per_group is not None and limit_per_group > 0:
                cols = cols[:limit_per_group]
            for c in cols:
                out.append(f'"{c["column"]}" [{grupo}]')
        return out

    def labeled_columns_sql_hint(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
    ) -> str:
        return ", ".join(self.labeled_columns(grupos, limit_per_group))'''

CD_API_NEW = '''    def comment_for_column(self, column: str) -> str:
        """P0-A: comentário (descrição/exemplos) da coluna, ou ``""`` se ausente."""
        if not column:
            return ""
        return self.comments.get(column.upper(), "")

    def labeled_columns(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
        with_comments: bool = False,
        max_comment_chars: int = 90,
    ) -> List[str]:
        """Colunas rotuladas com o grupo de origem (ex.: ``"SEXOPAC" [ps]``).

        Quando ``grupos`` é None, usa todos os grupos; ``limit_per_group``
        limita quantas colunas de cada grupo entram (útil no fallback para não
        inflar o prompt com centenas/milhares de entradas).

        P0-A: quando ``with_comments`` é True, anexa a descrição curta da coluna
        (truncada em ``max_comment_chars``), ex.: ``"AP_CIDPRI" [pa] — CID principal``.
        """
        if grupos is None:
            self._ensure()
            grupos = sorted(self.by_group.keys())
        out: List[str] = []
        for grupo in sorted(set(grupos)):
            cols = self.by_group.get(grupo, [])
            if limit_per_group is not None and limit_per_group > 0:
                cols = cols[:limit_per_group]
            for c in cols:
                label = f'"{c["column"]}" [{grupo}]'
                if with_comments:
                    comment = self.comment_for_column(c["column"])
                    if comment:
                        if max_comment_chars and len(comment) > max_comment_chars:
                            comment = comment[: max_comment_chars - 1].rstrip() + "…"
                        label = f"{label} — {comment}"
                out.append(label)
        return out

    def labeled_columns_sql_hint(
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

CD_STORE_OLD = '''    def get(
        self,
        grupo_columns: Mapping[str, Sequence[Mapping[str, str]]],
        grupos: Optional[Iterable[str]] = None,
    ) -> ColumnDictionary:
        dictionary = ColumnDictionary.from_grupo_columns(grupo_columns, grupos)
        sig = dictionary.signature'''

CD_STORE_NEW = '''    def get(
        self,
        grupo_columns: Mapping[str, Sequence[Mapping[str, str]]],
        grupos: Optional[Iterable[str]] = None,
        cache_dir: Optional[str] = None,
    ) -> ColumnDictionary:
        dictionary = ColumnDictionary.from_grupo_columns(grupo_columns, grupos, cache_dir=cache_dir)
        # P0-A: a chave do cache inclui o cache_dir, pois ele determina se o
        # dicionário carrega COMENTÁRIOS (descrição/exemplos dos layouts). Sem
        # isso, um dicionário obtido SEM cache_dir (sem comentários) poderia ser
        # servido para uma chamada COM cache_dir (e vice-versa), perdendo os
        # comentários no prompt. A assinatura sozinha não distingue os dois.
        sig = dictionary.signature + "\\x1f" + (str(cache_dir) if cache_dir else "")'''

CD_REPLACEMENTS = [
    (CD_DOC_OLD, CD_DOC_NEW),
    (CD_IMPORTS_OLD, CD_IMPORTS_NEW),
    (CD_DATACLASS_OLD, CD_DATACLASS_NEW),
    (CD_FROM_OLD, CD_FROM_NEW),
    (CD_API_OLD, CD_API_NEW),
    (CD_STORE_OLD, CD_STORE_NEW),
]


def apply_column_dictionary() -> dict:
    return replace_in_file(
        ROOT / "db" / "column_dictionary.py",
        CD_REPLACEMENTS,
        expect=["def comment_for_column", "with_comments", "_load_comments",
                "cache_dir: Optional[str] = None"],
        backup_dir=BACKUP_DIR,
    )


def main() -> int:
    steps = [
        ("settings", apply_settings),
        ("column_dictionary", apply_column_dictionary),
    ]
    for name, fn in steps:
        res = fn()
        print(json.dumps({"step": name, **res}, ensure_ascii=False, indent=2))
        if not res["ok"]:
            print(f"PASSO FALHOU: {name}", file=sys.stderr)
            return 1
    print("P0-A OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
