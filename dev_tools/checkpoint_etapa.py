"""Checkpoint de uma etapa: compila, valida o ajuste, salva snapshot e marca.

Uso:
    python -u dev_tools/checkpoint_etapa.py F1.1 db/cache_new.py
    python -u dev_tools/checkpoint_etapa.py F1.2 db/cache_new.py --absent _unified_view_enabled
    python -u dev_tools/checkpoint_etapa.py F2.1 db/group_cache_new.py --present attach

Faz, em uma unica chamada:
    1. py_compile de cada arquivo (falha -> exit 1, etapa NAO marcada);
    2. valida --absent (simbolo NAO pode existir) e --present (deve existir);
    3. copia cada arquivo para _backups/wip/<ts>/ (snapshot de 1 comando);
    4. grava estado.txt com status, linhas, SHA256 e resultado dos simbolos;
    5. marca a etapa como done (somente se tudo passar).
"""
from __future__ import annotations

import hashlib
import py_compile
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def parse(argv: list[str]) -> tuple[str, list[Path], list[str], list[str]]:
    """Separa etapa, arquivos, simbolos ausentes e presentes."""
    etapa = argv[0]
    arquivos: list[Path] = []
    ausentes: list[str] = []
    presentes: list[str] = []
    modo = "arquivos"
    for arg in argv[1:]:
        if arg == "--absent":
            modo = "ausentes"
            continue
        if arg == "--present":
            modo = "presentes"
            continue
        if modo == "arquivos":
            arquivos.append(Path(arg))
        elif modo == "ausentes":
            ausentes.append(arg)
        else:
            presentes.append(arg)
    return etapa, arquivos, ausentes, presentes


def contem(texto: str, simbolo: str) -> bool:
    """True se o simbolo aparecer como identificador isolado no texto."""
    return re.search(r"\b" + re.escape(simbolo) + r"\b", texto) is not None


def main() -> int:
    """Executa o checkpoint da etapa informada."""
    if len(sys.argv) < 3:
        print("uso: checkpoint_etapa.py <etapa> <arquivo> [arquivo...] "
              "[--absent sym...] [--present sym...]")
        return 2
    etapa, arquivos, ausentes, presentes = parse(sys.argv[1:])
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    wip = Path(f"_backups/wip/{ts}")
    wip.mkdir(parents=True, exist_ok=True)

    linhas: list[str] = []
    falhas = 0
    for path in arquivos:
        rel = path.as_posix()
        try:
            py_compile.compile(str(path), doraise=True)
            status = "compile=OK"
        except py_compile.PyCompileError as exc:
            status = f"compile=FALHA({exc.msg})"
            falhas += 1
        texto = path.read_text(encoding="utf-8-sig")
        for simbolo in ausentes:
            if contem(texto, simbolo):
                status += f" | absent:{simbolo}=AINDA_PRESENTE"
                falhas += 1
            else:
                status += f" | absent:{simbolo}=OK"
        for simbolo in presentes:
            if contem(texto, simbolo):
                status += f" | present:{simbolo}=OK"
            else:
                status += f" | present:{simbolo}=AUSENTE"
                falhas += 1
        shutil.copy2(path, wip / rel.replace("/", "__"))
        resumo = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        linhas.append(
            f"{rel} | {status} | linhas={len(texto.splitlines())} | sha={resumo}"
        )

    texto_final = "\n".join(linhas)
    print(texto_final)
    (wip / "estado.txt").write_text(texto_final + "\n", encoding="utf-8")

    if falhas == 0:
        subprocess.run(
            [sys.executable, "-u", "dev_tools/refactor_status.py", "--done", etapa],
            check=False,
        )
    else:
        print(f"ETAPA {etapa} NAO marcada: {falhas} verificacao(oes) falharam")
    print(f"WIP={wip}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
