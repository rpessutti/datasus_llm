"""Verifica estilo de docstrings/comentarios (ASCII, sem acentos) - F7.

Uso:
    python -u dev_tools/check_comment_style.py
    python -u dev_tools/check_comment_style.py db/cache.py --listar

Regras:
    - docstrings (modulo, classe, funcao) e comentarios ``#`` devem ser ASCII;
    - toda funcao/metodo de um arquivo alvo deve ter docstring;
    - literais de codigo (mensagens, SQL) NAO sao verificados.

Saida: uma linha por arquivo. Codigo de saida 1 quando houver violacao.
"""
from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

ALVOS_PADRAO = [
    "db/cache.py",
    "db/group_cache.py",
    "db/group_registry.py",
    "db/period_parser.py",
    "db/sql_repair.py",
    "db/summary.py",
    "db/cid_prefixes.py",
    "db/unified_view.py",
    "api/routes.py",
    "api/llm_service_parts/base.py",
    "api/llm_service_parts/ask.py",
]


def _docstrings(arvore: ast.AST) -> list[tuple[str, int, str]]:
    """Coleta ``(nome, linha, texto)`` das docstrings do arquivo."""
    itens: list[tuple[str, int, str]] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Module):
            nome = "<module>"
        elif isinstance(no, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            nome = no.name
        else:
            continue
        doc = ast.get_docstring(no, clean=False)
        if doc is None:
            continue
        corpo = no.body[0]
        itens.append((nome, corpo.lineno, doc))
    return itens


def _comentarios(fonte: str) -> list[tuple[int, str]]:
    """Coleta ``(linha, texto)`` dos comentarios ``#`` do arquivo."""
    itens: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(fonte).readline):
            if token.type == tokenize.COMMENT:
                itens.append((token.start[0], token.string))
    except tokenize.TokenError:
        pass
    return itens


def _sem_docstring(arvore: ast.AST) -> list[tuple[str, int]]:
    """Lista funcoes/metodos sem docstring."""
    faltando: list[tuple[str, int]] = []
    for no in ast.walk(arvore):
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(no, clean=False) is None:
                faltando.append((no.name, no.lineno))
    return faltando


def verificar(caminho: Path, listar: bool) -> tuple[int, int, int]:
    """Verifica um arquivo e devolve ``(acentos_doc, acentos_com, sem_doc)``."""
    fonte = caminho.read_text(encoding="utf-8-sig")
    arvore = ast.parse(fonte)
    acentos_doc = 0
    for nome, linha, texto in _docstrings(arvore):
        ruins = [c for c in texto if ord(c) > 127]
        acentos_doc += len(ruins)
        if ruins and listar:
            codigos = ", ".join(f"U+{ord(c):04X}" for c in sorted(set(ruins)))
            print(f"  doc {caminho}:{linha} ({nome}): {codigos}")
    acentos_com = 0
    for linha, texto in _comentarios(fonte):
        ruins = [c for c in texto if ord(c) > 127]
        acentos_com += len(ruins)
        if ruins and listar:
            codigos = ", ".join(f"U+{ord(c):04X}" for c in sorted(set(ruins)))
            print(f"  com {caminho}:{linha}: {codigos}")
    faltando = _sem_docstring(arvore)
    if faltando and listar:
        for nome, linha in faltando:
            print(f"  sem-doc {caminho}:{linha}: {nome}")
    return acentos_doc, acentos_com, len(faltando)


def main(argv: list[str]) -> int:
    """Executa a verificacao nos arquivos indicados (ou nos alvos padrao)."""
    parser = argparse.ArgumentParser(description="estilo de docstrings/comentarios")
    parser.add_argument("arquivos", nargs="*", default=[])
    parser.add_argument("--listar", action="store_true")
    args = parser.parse_args(argv[1:])
    alvos = [Path(p) for p in (args.arquivos or ALVOS_PADRAO)]
    falhas = 0
    for caminho in alvos:
        if not caminho.is_file():
            print(f"{caminho} | AUSENTE")
            falhas += 1
            continue
        acentos_doc, acentos_com, sem_doc = verificar(caminho, args.listar)
        status_doc = "OK" if acentos_doc == 0 else f"FALHA({acentos_doc})"
        status_com = "OK" if acentos_com == 0 else f"FALHA({acentos_com})"
        status_falta = "OK" if sem_doc == 0 else f"FALHA({sem_doc})"
        print(
            f"{caminho} | doc_ascii={status_doc} | coment_ascii={status_com} "
            f"| docstring={status_falta}"
        )
        falhas += (acentos_doc > 0) + (acentos_com > 0) + (sem_doc > 0)
    return 0 if falhas == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
