"""Edição segura de arquivos: backup + substituição + verificação + auto-restore.

Fluxo (por arquivo):
  1. backup datado (via core.safe_io.backup_file);
  2. lê o texto e aplica uma substituição EXATA (uma ou mais);
  3. grava com escrita atômica + fsync;
  4. verifica: (a) o ``.py`` compila; (b) o conteúdo contém os marcadores esperados.
  5. se QUALQUER verificação falhar, RESTAURA do backup automaticamente.

Uso em linha de comando (JSON):

    python dev_tools/safe_edit.py --file config/settings.py \
        --replace "antigo|||novo" --expect "novo"

Também pode ser importado:

    from dev_tools.safe_edit import replace_in_file
    result = replace_in_file("config/settings.py", [("old", "new")], expect=["new"])

Retorna um dict com ``ok``, ``backup``, ``changed``, ``verified`` e ``restored``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import safe_io  # noqa: E402

logger = logging.getLogger("safe_edit")


def _compiles(path: Path) -> bool:
    """True se o arquivo ``.py`` compila (sintaxe válida). Outros: sempre True."""
    if path.suffix != ".py":
        return True
    import py_compile

    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except py_compile.PyCompileError as exc:
        logger.error("compilação falhou em %s: %s", path, exc)
        return False


def _all_expected_present(text: str, expect: Iterable[str]) -> bool:
    missing = [e for e in expect if e not in text]
    if missing:
        logger.error("marcadores ausentes após edição: %s", missing)
        return False
    return True


def replace_in_file(
    filepath: "str | Path",
    replacements: Sequence["tuple[str, str]"],
    expect: Optional[Iterable[str]] = None,
    backup_dir: Optional["str | Path"] = None,
) -> dict:
    """Aplica ``replacements`` (old, new) de forma segura com auto-restore."""
    path = Path(filepath)
    result = {"file": str(path), "ok": False, "backup": None, "changed": False,
              "verified": False, "restored": False}

    if not path.is_file():
        logger.error("replace_in_file: arquivo inexistente: %s", path)
        return result

    original = path.read_text(encoding="utf-8")
    backup = safe_io.backup_file(path, dest_dir=backup_dir)
    result["backup"] = str(backup) if backup else None

    new_text = original
    for old, new in replacements:
        if old not in new_text:
            logger.error("replace_in_file: trecho não encontrado em %s: %r", path, old[:80])
            # Nada foi alterado ainda; arquivo permanece o original. Encerra.
            return result
        new_text = new_text.replace(old, new, 1)

    result["changed"] = new_text != original
    if not result["changed"]:
        logger.warning("replace_in_file: nenhuma mudança efetiva em %s", path)
        result["ok"] = True
        result["verified"] = True
        return result

    safe_io.atomic_write_text(path, new_text)

    # Verificações pós-escrita.
    verified = True
    if not _compiles(path):
        verified = False
    written = path.read_text(encoding="utf-8")
    if expect is not None and not _all_expected_present(written, expect):
        verified = False
    if not safe_io.verify_integrity(path):  # arquivo legível/existe
        verified = False

    result["verified"] = verified
    result["ok"] = verified

    if not verified:
        logger.error("replace_in_file: verificação falhou; RESTAURANDO %s", path)
        ok = safe_io.restore_file(path, backup=backup)
        result["restored"] = ok
        result["ok"] = False
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Edição segura de arquivos.")
    parser.add_argument("--file", required=True)
    parser.add_argument("--replace", action="append", default=[],
                        help="Par 'antigo|||novo' (pode repetir).")
    parser.add_argument("--expect", action="append", default=[],
                        help="Marcador que deve estar presente após a edição.")
    parser.add_argument("--backup-dir", default=None)
    args = parser.parse_args(argv)

    reps = []
    for item in args.replace:
        if "|||" not in item:
            logger.error("--replace precisa do formato 'antigo|||novo': %r", item)
            return 2
        old, new = item.split("|||", 1)
        reps.append((old, new))

    res = replace_in_file(args.file, reps, expect=args.expect, backup_dir=args.backup_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
