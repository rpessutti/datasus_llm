from __future__ import annotations

import functools
import logging
from logging.handlers import RotatingFileHandler
import os
import time
from pathlib import Path
from typing import Any, Callable

from config.settings import PROJECT_DIR

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
console_log_level = os.getenv("CONSOLE_LOG_LEVEL", "WARNING").upper()
LOGGER = logging.getLogger("datasus_llm")


def _setup_file_logging() -> None:
    """Configura o log em arquivo, caindo para um local seguro se o alvo
    configurado (ex.: um disco removível) não estiver disponível.

    Evita que um `LOG_FILE` apontando para um diretório indisponível (ex.: unidade
    de dados não montada) derrube a aplicação no import. Nesse caso, registra um
    aviso e redireciona o arquivo de log para ./cache/datasus.log local."""
    LOG_FILE_DIR = Path(os.getenv("LOG_DIR", "logs")).expanduser()
    if not LOG_FILE_DIR.is_absolute():
        LOG_FILE_DIR = PROJECT_DIR / LOG_FILE_DIR

    configured = LOG_FILE_DIR / os.getenv("LOG_FILE_NAME", "datasus.log")

    # Se diretório 'logs' não existe ou não é acessível, cria e usa na raiz
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        LOGGER.warning(
            "diretório de logs %s indisponível; usando fallback para root %s",
            configured, PROJECT_DIR / "cache" / "datasus.log"
        )
        configured = PROJECT_DIR / "cache" / "datasus.log"

    # Verifica novamente se o arquivo final é acessível
    if configured is None or not configured.parent.exists():
        LOGGER.warning("nem mesmo fallback root disponível; usando console apenas")
        return  # Sai da função sem configurar file handler

    logging.basicConfig(level=log_level, format=LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        handler.setLevel(console_log_level)

    if configured is not None:
        try:
            file_handler = RotatingFileHandler(
                str(configured), maxBytes=25 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            file_handler.setLevel(log_level)
            logging.getLogger().addHandler(file_handler)
        except OSError as exc:
            LOGGER.warning("não foi possível abrir o arquivo de log %s; apenas console", configured)
            if isinstance(exc, PermissionError):
                LOGGER.error("permissão negada para escrita em %s", configured)


def log_calls(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        LOGGER.debug("start %s args=%s kwargs=%s", method.__qualname__, args[1:], kwargs)
        try:
            result = method(*args, **kwargs)
            LOGGER.debug("done %s duration_ms=%.2f", method.__qualname__, (time.perf_counter() - started) * 1000)
            return result
        except Exception:
            LOGGER.exception("failed %s duration_ms=%.2f", method.__qualname__, (time.perf_counter() - started) * 1000)
            raise

    return wrapped