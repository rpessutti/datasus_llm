from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("datasus_llm")

# ---------------------------------------------------------------------------
# I/O resiliente para dispositivos removíveis (USB / disco externo).
# ---------------------------------------------------------------------------
# Um pen drive ou HD externo pode ser desconectado, entrar em suspensão ou ter a
# montagem derrubada a qualquer momento. Chamadas de I/O diretas (stat, rglob,
# open, copyfile, read_parquet) podem então lançar OSError de forma intermitente.
# O objetivo destes helpers é:
#   * tratar cada operação de I/O sobre o disco removível como potencialmente
#     instável, com retry curto e backoff para falhas transitórias;
#   * nunca deixar uma exceção de I/O de um arquivo derrubar a ingestão inteira
#     ou o processo (ao contrário do comportamento atual, onde stat/copyfile
#     dentro dos loops propagam e interrompem a sincronização);
#   * reportar indisponibilidade do disco de forma clara, em vez de stacktrace.
# Configuráveis via env (opcional):
#   USB_IO_RETRIES  (default 3)   -- tentativas por operação de I/O
#   USB_IO_BACKOFF  (default 0.5) -- segundos iniciais entre tentativas (backoff
#                                    exponencial: 0.5s, 1s, ...)

_IO_RETRIES = int(os.getenv("USB_IO_RETRIES", "3"))
_IO_RETRY_BACKOFF = float(os.getenv("USB_IO_BACKOFF", "0.5"))


def _io_retry(description: str, fn: Callable[..., Any], *args: Any) -> Any:
    """Executa `fn(*args)` tratando OSError/EOFError (disco removível).

    Tenta até `_IO_RETRIES` vezes com backoff exponencial. Se todas as tentativas
    falharem, relança a última exceção para o chamador decidir como degradar.
    """
    for attempt in range(1, _IO_RETRIES + 1):
        try:
            return fn(*args)
        except (OSError, EOFError) as exc:
            if attempt >= _IO_RETRIES:
                raise
            delay = _IO_RETRY_BACKOFF * (2 ** (attempt - 1))
            LOGGER.warning(
                "I/O transitório (%s) na tentativa %d/%d; reintentando em %.2fs: %s",
                description, attempt, _IO_RETRIES, delay, exc,
            )
            time.sleep(delay)
    raise OSError(f"I/O falhou após {_IO_RETRIES} tentativas: {description}")


def _retry_stat(path: Path) -> os.stat_result | None:
    """stat() resiliente; retorna None se o arquivo/direcionário não puder ser lido.

    Evita que uma exceção de stat num USB desconectado derrube a ingestão inteira.
    """
    try:
        return _io_retry(f"stat {path}", Path.stat, path)
    except OSError:
        LOGGER.error("I/O falhou ao ler metadados de %s (disco removível?); ignorado", path)
        return None


def _retry_copyfile(src: Path, dst: Path) -> bool:
    """Copia um arquivo de forma resiliente e valida o tamanho depois.

    Retorna True se a cópia foi concluída e tem o tamanho esperado; False caso
    contrário (USB falhou no meio da cópia ou a origem sumiu). Não propaga exceção:
    o chamador trata um arquivo não copiado como inválido.
    """
    try:
        src_stat = _retry_stat(src)
        if src_stat is None:
            return False
        # Copia para um arquivo temporário e depois renomeia, para evitar que uma
        # leitura parcial deixe um .parquet "válido" mas truncado no cache.
        tmp = dst.with_suffix(dst.suffix + ".part")
        dst.parent.mkdir(parents=True, exist_ok=True)
        _io_retry(f"copyfile {src} -> {dst}", shutil.copyfile, src, tmp)
        # Verifica o tamanho da cópia contra a origem fora do retry (não-vídico:
        # um arquivo truncado pode ter tamanho igual; a validação real é o
        # read_parquet, feito pelo chamador depois).
        tmp_stat = _retry_stat(tmp)
        if tmp_stat is None or tmp_stat.st_size != src_stat.st_size:
            tmp.unlink(missing_ok=True)
            LOGGER.error("cópia incompleta de %s -> %s (USB desconectado?)", src, dst)
            return False
        tmp.replace(dst)
        return True
    except OSError as exc:
        LOGGER.error("I/O falha ao copiar %s -> %s (disco removível?): %s", src, dst, exc)
        dst.with_suffix(dst.suffix + ".part").unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        return False


def _discover_files_in(dir_path: Path) -> list[Path]:
    """Lista recursivamente *.parquet de forma resiliente a desconexão do USB.

    Retorna lista vazia (sem lançar OSError) se o diretório sumir ou não estiver
    disponível, para que a ingestão não seja interrompida.
    """
    if not dir_path.is_dir():
        return []
    try:
        return _io_retry(f"rglob {dir_path}", lambda: sorted(dir_path.rglob("*.parquet")))
    except OSError as exc:
        LOGGER.error("I/O falha ao listar %s (disco removível?); tratando como sem arquivos: %s",
                     dir_path, exc)
        return []


def _disk_available(path: Path) -> bool:
    """Checa rapidamente se o caminho (possivelmente num USB) ainda responde."""
    try:
        _io_retry(f"probe {path}", lambda: path.is_dir())
        return True
    except OSError:
        LOGGER.error("disco removível indisponível em %s", path)
        return False


def _sync_to_official_cache(src_dir: Path, dst_dir: Path) -> bool:
    """Copia o cache temporário (local) para o cache oficial (HD externo).

    Estratégia:
      1. Se o destino for o mesmo do cache temporário, pula (já está sincronizado).
      2. Copia cada arquivo do cache temporário para um diretório temporário
         extra (.sync_tmp) no HD externo, com retries para I/O instável.
      3. Após a cópia completa, remove o cache oficial antigo e renomeia o
         temporário para o atual.

    Retorna True se a sincronização foi concluída; False se o HD não estava
    disponível ou a cópia falhou (neste caso, o cache temporário ainda fica
    íntegro localmente para uso imediato).
    """
    # Se for o mesmo diretório, não há o que sincronizar
    if str(src_dir.resolve()) == str(dst_dir.resolve()):
        LOGGER.info("cache oficial é o mesmo do temporário (%s); pulando sincronização", src_dir)
        return False

    if not src_dir.is_dir():
        LOGGER.error("cache temporário não existe: %s", src_dir)
        return False

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error("cache oficial indisponível (%s): %s", dst_dir, exc)
        return False

    tmp_dst = dst_dir.with_name(dst_dir.name + ".sync_tmp")
    started = time.perf_counter()
    LOGGER.info("sincronização cache temporário -> oficial: %s -> %s", src_dir, dst_dir)

    try:
        # Remove resíduos de sincronizações anteriores.
        if tmp_dst.exists():
            shutil.rmtree(tmp_dst, ignore_errors=True)
        tmp_dst.mkdir(parents=True, exist_ok=True)

        # Copia arquivo por arquivo com retries (HD externo pode estar em suspensão).
        copied = 0
        for src_path in src_dir.rglob("*"):
            if not src_path.is_file():
                continue
            rel = src_path.relative_to(src_dir)
            dst_path = tmp_dst / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if not _retry_copyfile(src_path, dst_path):
                LOGGER.error("falha ao copiar %s -> %s; abortando sincronização", src_path, dst_path)
                shutil.rmtree(tmp_dst, ignore_errors=True)
                return False
            copied += 1

        # Substitui o cache oficial antigo pelo sincronizado (atômico).
        if dst_dir.exists():
            backup = dst_dir.with_name(dst_dir.name + ".old")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            dst_dir.rename(backup)
        tmp_dst.rename(dst_dir)
        if Path(str(dst_dir) + ".old").exists():
            shutil.rmtree(Path(str(dst_dir) + ".old"), ignore_errors=True)

        duration_ms = (time.perf_counter() - started) * 1000
        LOGGER.info("sincronização concluída: %d arquivos, %.2fms", copied, duration_ms)
        return True
    except OSError as exc:
        LOGGER.error("falha na sincronização do cache oficial (%s): %s", dst_dir, exc)
        shutil.rmtree(tmp_dst, ignore_errors=True)
        return False