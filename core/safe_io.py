"""Escrita segura de arquivos: backup, escrita atômica + fsync e restore rápido.

Motivação
=========
Ajustes no código (e artefatos derivados, como caches JSON) NÃO devem corromper
arquivos em caso de falha/energia. Este módulo oferece:

- ``backup_file(path)``      -> cópia datada ``<path>.bak_<YYYYMMDD_HHMMSS>`` + fsync.
- ``atomic_write_text``      -> escreve em ``.tmp`` + ``flush`` + ``os.fsync`` +
  ``os.replace`` + fsync do diretório (rename ATÔMICO no mesmo filesystem).
- ``fsync_path``/``fsync_dir`` -> garante que os bytes chegaram ao disco.
- ``restore_file``           -> restaura a partir do backup mais recente.
- ``verify_integrity``       -> compara SHA256 entre um arquivo e um backup.

Este módulo NÃO faz ``CHECKPOINT`` do DuckDB (ver o plano: evitar lock com os
16 schemas anexados). Ele é para arquivos de texto/JSON e artefatos próprios.

Uso típico antes de editar um arquivo crítico::

    from core import safe_io
    bak = safe_io.backup_file("config/settings.py")        # mantém cópia datada
    safe_io.atomic_write_text("config/settings.py", novo)  # grava com fsync
    assert safe_io.verify_integrity("config/settings.py", bak)

Em caso de corrupção, restore rápido::

    safe_io.restore_file("config/settings.py", backup=bak)
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"


def _timestamp() -> str:
    return time.strftime(_TIMESTAMP_FMT)


def fsync_path(path: "str | os.PathLike[str]") -> None:
    """Força a gravação em disco dos bytes já escritos em ``path``.

    Abre o arquivo em modo somente-leitura e chama ``os.fsync`` no descritor.
    Silencioso em ambientes onde fsync não se aplica (ex.: Windows sem handle).
    """
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError as exc:  # pragma: no cover - dependente de FS
        logger.debug("fsync_path: não foi possível abrir %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # pragma: no cover - no Windows pode falhar
        logger.debug("fsync_path: fsync falhou em %s: %s", path, exc)
    finally:
        os.close(fd)


def fsync_dir(path: "str | os.PathLike[str]") -> None:
    """Força a gravação de um diretório (garante persistência de renames/criações).

    No Windows não há fsync de diretório; a chamada é apenas best-effort.
    """
    directory = os.fspath(path)
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:  # pragma: no cover - no Windows costuma falhar
        logger.debug("fsync_dir: não foi possível abrir dir %s: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # pragma: no cover
        logger.debug("fsync_dir: fsync falhou em %s: %s", directory, exc)
    finally:
        os.close(fd)


def sha256(path: "str | os.PathLike[str]", chunk: int = 1 << 20) -> str:
    """SHA256 de um arquivo (leitura em blocos)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def backup_file(
    path: "str | os.PathLike[str]",
    dest_dir: Optional["str | os.PathLike[str]"] = None,
) -> Optional[Path]:
    """Cria um backup datado de ``path`` e devolve o caminho do backup.

    Formato: ``<nome>.bak_<YYYYMMDD_HHMMSS>`` no mesmo diretório do arquivo
    (ou em ``dest_dir``, se informado). Se o arquivo não existir, retorna
    ``None`` (nada a copiar). O backup é gravado com ``copy2`` (metadados) e
    sofre ``fsync`` para garantir durabilidade.
    """
    src = Path(path)
    if not src.is_file():
        logger.warning("backup_file: arquivo inexistente, nada a copiar: %s", src)
        return None

    target_dir = Path(dest_dir) if dest_dir is not None else src.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"{src.name}.bak_{_timestamp()}"

    # Evita sobrescrever um backup existente com o mesmo segundo.
    if backup_path.exists():
        suffix = 1
        while True:
            candidate = target_dir / f"{src.name}.bak_{_timestamp()}_{suffix}"
            if not candidate.exists():
                backup_path = candidate
                break
            suffix += 1

    shutil.copy2(src, backup_path)
    fsync_path(backup_path)
    fsync_dir(target_dir)
    logger.info("backup_file: %s -> %s", src, backup_path)
    return backup_path


def atomic_write_text(
    path: "str | os.PathLike[str]",
    content: str,
    encoding: str = "utf-8",
) -> Path:
    """Grava ``content`` em ``path`` de forma ATÔMICA e durável.

    Escreve em ``<path>.tmp`` no MESMO diretório, faz ``flush`` + ``os.fsync``,
    e então ``os.replace`` (rename atômico). Por fim faz fsync do diretório.
    Um leitor concorrente vê o arquivo antigo OU o novo — nunca um parcial.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")

    with open(tmp, "w", encoding=encoding, newline="\n") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())

    os.replace(tmp, target)  # rename atômico no mesmo FS
    fsync_path(target)
    fsync_dir(target.parent)
    logger.info("atomic_write_text: gravado com fsync: %s (%d chars)", target, len(content))
    return target


def atomic_write_bytes(
    path: "str | os.PathLike[str]",
    content: bytes,
) -> Path:
    """Versão binária de :func:`atomic_write_text`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")

    with open(tmp, "wb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())

    os.replace(tmp, target)
    fsync_path(target)
    fsync_dir(target.parent)
    logger.info("atomic_write_bytes: gravado com fsync: %s (%d bytes)", target, len(content))
    return target


def verify_integrity(
    path: "str | os.PathLike[str]",
    backup: Optional["str | os.PathLike[str]"] = None,
) -> bool:
    """Confere se ``path`` está íntegro comparando SHA256 com ``backup``.

    Se ``backup`` for ``None``, considera íntegro apenas que o arquivo exista e
    seja legível (não há com o que comparar). Retorna ``False`` se o arquivo não
    existir ou se os hashes divergirem.
    """
    p = Path(path)
    if not p.is_file():
        logger.error("verify_integrity: arquivo ausente: %s", p)
        return False
    if backup is None:
        # Sem referência: apenas garante legibilidade.
        try:
            sha256(p)
            return True
        except OSError as exc:
            logger.error("verify_integrity: não foi possível ler %s: %s", p, exc)
            return False

    b = Path(backup)
    if not b.is_file():
        logger.error("verify_integrity: backup ausente: %s", b)
        return False

    try:
        h1 = sha256(p)
        h2 = sha256(b)
    except OSError as exc:
        logger.error("verify_integrity: erro de leitura: %s", exc)
        return False

    if h1 == h2:
        logger.info("verify_integrity: OK (hashes idênticos) %s", p)
        return True
    logger.error(
        "verify_integrity: DIVERGÊNCIA em %s (atual=%s backup=%s)", p, h1[:12], h2[:12]
    )
    return False


def text_differs(path: "str | os.PathLike[str]", backup: "str | os.PathLike[str]") -> bool:
    """True se o conteúdo textual de ``path`` difere de ``backup``.

    Útil para confirmar que um ajuste realmente alterou o arquivo (e que um
    backup pré-edição é diferente do pós-edição).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            a = fh.read()
        with open(backup, "r", encoding="utf-8") as fh:
            b = fh.read()
    except OSError as exc:
        logger.error("text_differs: erro de leitura: %s", exc)
        return True
    return a != b


def latest_backup(
    path: "str | os.PathLike[str]",
    dest_dir: Optional["str | os.PathLike[str]"] = None,
) -> Optional[Path]:
    """Retorna o backup MAIS RECENTE de ``path`` (ou ``None``).

    Procura por ``<nome>.bak_*`` no diretório do arquivo (ou ``dest_dir``) e
    devolve o de maior mtime.
    """
    src = Path(path)
    target_dir = Path(dest_dir) if dest_dir is not None else src.parent
    if not target_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in target_dir.glob(f"{src.name}.bak_*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def restore_file(
    path: "str | os.PathLike[str]",
    backup: Optional["str | os.PathLike[str]"] = None,
    verify: bool = True,
) -> bool:
    """Restaura ``path`` a partir de ``backup`` (ou do backup mais recente).

    Grava de volta com escrita atômica + fsync. Se ``verify=True``, confere o
    hash após a escrita. Retorna ``True`` em caso de sucesso.
    """
    target = Path(path)
    src = Path(backup) if backup is not None else latest_backup(target)
    if src is None or not src.is_file():
        logger.error("restore_file: nenhum backup disponível para %s", target)
        return False

    try:
        content = src.read_bytes()
    except OSError as exc:
        logger.error("restore_file: falha ao ler backup %s: %s", src, exc)
        return False

    atomic_write_bytes(target, content)
    if verify and sha256(target) != sha256(src):
        logger.error("restore_file: verificação falhou após restaurar %s de %s", target, src)
        return False
    logger.warning("restore_file: %s restaurado de %s", target, src)
    return True


def backup_files(paths: Iterable["str | os.PathLike[str]"], dest_dir: "str | os.PathLike[str]") -> dict:
    """Faz backup de vários arquivos em ``dest_dir`` e devolve ``{origem: backup}``.

    O nome do backup preserva a estrutura relativa com ``__`` no lugar dos
    separadores, evitando colisões entre diretórios.
    """
    out: dict[str, Optional[str]] = {}
    base = Path(dest_dir)
    for p in paths:
        src = Path(p)
        if not src.is_file():
            out[str(src)] = None
            continue
        safe_name = str(src).replace("\\", "__").replace("/", "__")
        target = base / safe_name
        base.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        fsync_path(target)
        out[str(src)] = str(target)
    fsync_dir(base)
    return out
