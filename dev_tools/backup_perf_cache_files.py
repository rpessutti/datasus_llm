"""Guarda do PLANO_PERFORMANCE_CACHE.md: backup, restore, verify e auditoria de metodos.

Os 4 arquivos que o plano altera sao copiados para ``_backups/perf_cache_<stamp>/``
(com fsync + SHA256SUMS), de modo que qualquer corrupcao seja revertida em segundos.

Uso (da raiz do projeto):
    python dev_tools/backup_perf_cache_files.py                # cria o backup
    python dev_tools/backup_perf_cache_files.py --verify       # SHA256 atual x backup
    python dev_tools/backup_perf_cache_files.py --list         # lista os backups
    python dev_tools/backup_perf_cache_files.py --restore                  # restaura TODOS
    python dev_tools/backup_perf_cache_files.py --restore db/group_cache.py
    python dev_tools/backup_perf_cache_files.py --metodos      # metodos sem docstring
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import safe_io  # noqa: E402

FILES: tuple[str, ...] = (
    "config/settings.py",
    "db/group_cache.py",
    "db/cache.py",
    "dev_tools/rebuild_group_cache.py",
)
BACKUP_ROOT = ROOT / "_backups"
PREFIX = "perf_cache_"


def _safe_name(rel: str) -> str:
    """Nome plano usado por ``safe_io.backup_files`` (``db/a.py`` -> ``db__a.py``)."""
    return rel.replace("\\", "__").replace("/", "__")


def _latest() -> Path | None:
    """Diretorio do backup ``perf_cache_*`` mais recente (ou ``None``)."""
    if not BACKUP_ROOT.is_dir():
        return None
    dirs = [d for d in BACKUP_ROOT.glob(PREFIX + "*") if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def cmd_backup() -> int:
    """Copia os arquivos-alvo para um novo ``_backups/perf_cache_<stamp>/``."""
    dest = BACKUP_ROOT / (PREFIX + time.strftime("%Y%m%d_%H%M%S"))
    mapping = safe_io.backup_files(FILES, dest)
    # ``backup_files`` devolve as chaves com o separador do SO (``\`` no Windows).
    por_rel: dict[str, str | None] = {
        str(k).replace("\\", "/"): v for k, v in mapping.items()
    }
    linhas: list[str] = []
    for rel in FILES:
        bak = por_rel.get(rel)
        if not bak:
            print(f"[FALTA] {rel} (arquivo ausente ou backup nao criado)")
            continue
        linhas.append(f"{safe_io.sha256(rel)}  {rel}  ({Path(bak).name})")
        print(f"[OK] {rel} -> {Path(bak).relative_to(ROOT)}")
    manifest = dest / "SHA256SUMS.txt"
    manifest.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    safe_io.fsync_path(manifest)
    print(f"\nBackup concluido em: {dest}")
    return 0 if len(linhas) == len(FILES) else 1


def cmd_verify() -> int:
    """Compara o SHA256 atual de cada arquivo com o do backup mais recente."""
    dest = _latest()
    if dest is None:
        print("nenhum backup encontrado")
        return 1
    ok = True
    for rel in FILES:
        bak = dest / _safe_name(rel)
        if not bak.is_file():
            print(f"[FALTA] backup de {rel} em {dest.name}")
            ok = False
            continue
        same = safe_io.verify_integrity(rel, bak)
        print(f"[{'OK' if same else 'DIFERE'}] {rel}")
        ok = ok and same
    print(f"backup de referencia: {dest}")
    return 0 if ok else 1


def cmd_restore(alvo: str | None) -> int:
    """Restaura um arquivo (ou todos) a partir do backup mais recente."""
    dest = _latest()
    if dest is None:
        print("nenhum backup encontrado")
        return 1
    alvos = [alvo] if alvo else list(FILES)
    rc = 0
    for rel in alvos:
        bak = dest / _safe_name(rel)
        if not bak.is_file():
            print(f"[FALTA] backup de {rel} em {dest.name}")
            rc = 1
            continue
        ok = safe_io.restore_file(rel, backup=bak, verify=True)
        print(f"[{'OK' if ok else 'FALHOU'}] restaurado {rel} de {bak.name}")
        rc = rc or (0 if ok else 1)
    return rc


def cmd_list() -> int:
    """Lista os backups ``perf_cache_*`` existentes (mais antigo -> mais novo)."""
    dirs = sorted(BACKUP_ROOT.glob(PREFIX + "*")) if BACKUP_ROOT.is_dir() else []
    for d in dirs:
        print(d.name)
    return 0


def cmd_metodos() -> int:
    """Audita os arquivos-alvo: cada metodo/class tem docstring? (AST, somente leitura)."""
    faltando = 0
    for rel in FILES:
        caminho = Path(rel)
        if not caminho.is_file():
            print(f"--- {rel}: NAO EXISTE")
            continue
        arvore = ast.parse(caminho.read_text(encoding="utf-8-sig", errors="replace"))
        print(f"\n=== {rel} ===")

        def visitar(no: ast.AST, prefixo: str) -> None:
            nonlocal faltando
            for filho in getattr(no, "body", []):
                if isinstance(filho, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    tem = ast.get_docstring(filho) is not None
                    faltando += 0 if tem else 1
                    print(f"  [{'ok ' if tem else 'FALTA'}] {prefixo}{filho.name} (linha {filho.lineno})")
                elif isinstance(filho, ast.ClassDef):
                    tem = ast.get_docstring(filho) is not None
                    faltando += 0 if tem else 1
                    print(f"  [{'ok ' if tem else 'FALTA'}] class {filho.name} (linha {filho.lineno})")
                    visitar(filho, prefixo + filho.name + ".")

        visitar(arvore, "")
    print(f"\n[metodos] sem docstring: {faltando}")
    return 0 if faltando == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """CLI: backup (default), ``--verify``, ``--list``, ``--restore``, ``--metodos``."""
    parser = argparse.ArgumentParser(
        description="Backup/restore/verify e auditoria dos arquivos do PLANO_PERFORMANCE_CACHE.md",
    )
    parser.add_argument("--restore", nargs="?", const="", metavar="ARQUIVO",
                        help="restaura do backup mais recente (todos, ou somente ARQUIVO)")
    parser.add_argument("--verify", action="store_true", help="SHA256 atual x backup mais recente")
    parser.add_argument("--list", action="store_true", help="lista os backups existentes")
    parser.add_argument("--metodos", action="store_true", help="metodos sem docstring nos arquivos-alvo")
    args = parser.parse_args(argv)

    os.chdir(ROOT)  # garante que os caminhos relativos de FILES resolvam
    if args.list:
        return cmd_list()
    if args.verify:
        return cmd_verify()
    if args.metodos:
        return cmd_metodos()
    if args.restore is not None:
        return cmd_restore(args.restore or None)
    return cmd_backup()


if __name__ == "__main__":
    raise SystemExit(main())

