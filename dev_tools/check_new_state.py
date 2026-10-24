"""Relatorio de estado dos arquivos *_new.py (checkpoint do refactor).

Uso:
    python -u dev_tools/check_new_state.py
    python -u dev_tools/check_new_state.py db/cache_new.py

Saida por arquivo: linhas, metodos, docstrings faltando, acentos em
comentarios/docstrings, BOM, SHA256, mtime e status.
Exit code 1 quando houver erro de sintaxe/encoding.
"""
from __future__ import annotations

import ast
import hashlib
import io
import sys
import tokenize
from datetime import datetime
from pathlib import Path

ACENTOS = set("áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ")
IGNORAR = {"_backups", ".venv", "__pycache__", ".git", ".pytest_cache", ".spill"}


def alvos() -> list[Path]:
    """Retorna os caminhos por argumento ou todos os *_new.py do repo."""
    if len(sys.argv) > 1:
        return [Path(arg) for arg in sys.argv[1:]]
    return sorted(
        p for p in Path(".").rglob("*_new.py")
        if not IGNORAR.intersection(p.parts)
    )


def estado(path: Path) -> dict:
    """Coleta as metricas de um arquivo sem alterar nada."""
    dados = path.read_bytes()
    texto = dados.decode("utf-8-sig")  # BOM e tolerado e reportado em bom=
    arvore = ast.parse(texto)
    funcoes = [
        n for n in ast.walk(arvore)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    sem_doc = sum(1 for f in funcoes if ast.get_docstring(f) is None)
    acentos = 0
    for tok in tokenize.generate_tokens(io.StringIO(texto).readline):
        if tok.type == tokenize.COMMENT:
            acentos += sum(1 for c in tok.string if c in ACENTOS)
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef,
                           ast.AsyncFunctionDef)):
            doc = ast.get_docstring(no, clean=False) or ""
            acentos += sum(1 for c in doc if c in ACENTOS)
    return {
        "linhas": len(texto.splitlines()),
        "metodos": len(funcoes),
        "sem_doc": sem_doc,
        "acentos": acentos,
        "bom": dados[:3] == b"\xef\xbb\xbf",
        "sha256": hashlib.sha256(dados).hexdigest()[:16],
        "mtime": datetime.fromtimestamp(
            path.stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    """Imprime a tabela de estado e devolve 1 se algum arquivo falhar."""
    falhou = False
    encontrados = alvos()
    if not encontrados:
        print("nenhum *_new.py encontrado (F0.4 ainda nao executada)")
        return 0
    for path in encontrados:
        if not path.is_file():
            print(f"{path}: AUSENTE")
            falhou = True
            continue
        try:
            info = estado(path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"{path}: FALHA ({type(exc).__name__}: {exc})")
            falhou = True
            continue
        print(
            f"{path} | linhas={info['linhas']} | metodos={info['metodos']} | "
            f"sem_doc={info['sem_doc']} | acentos={info['acentos']} | "
            f"utf8=OK | bom={info['bom']} | sha={info['sha256']} | "
            f"mtime={info['mtime']} | status=OK"
        )
    return 1 if falhou else 0


if __name__ == "__main__":
    raise SystemExit(main())
