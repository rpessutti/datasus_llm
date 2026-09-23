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

LOGGER = logging.getLogger("datasus_llm")

# Garante que a configuração aconteça apenas uma vez (idempotente), mesmo que o
# módulo seja importado por vários pontos da aplicação.
_CONFIGURED = False


def _resolve_log_level() -> str:
    """Lê o nível de log do ambiente (respeita `LOG_LEVEL` do `.env`).

    Padrão INFO. Valores inválidos (ex.: "VERBOSE") caem para INFO em vez de
    derrubar a aplicação."""
    raw = os.getenv("LOG_LEVEL", "INFO").upper()
    if logging.getLevelName(raw) == f"Level {raw}":
        return "INFO"
    return raw


def _resolve_console_log_level() -> str:
    """Nível do handler de console (padrão WARNING via `CONSOLE_LOG_LEVEL`)."""
    raw = os.getenv("CONSOLE_LOG_LEVEL", "WARNING").upper()
    if logging.getLevelName(raw) == f"Level {raw}":
        return "WARNING"
    return raw


def _resolve_log_file_path() -> Path:
    """Resolve o caminho do arquivo de log.

    Aceita duas formas (compatível com o README e com a implementação antiga):
      * `LOG_FILE`                    -> caminho COMPLETO do arquivo
      * `LOG_DIR` + `LOG_FILE_NAME`   -> diretório + nome (padrão ./logs/datasus.log)
    """
    # 1) LOG_FILE tem precedência (é o que o README documenta).
    env_log_file = os.getenv("LOG_FILE")
    if env_log_file:
        configured = Path(env_log_file).expanduser()
        if not configured.is_absolute():
            configured = PROJECT_DIR / configured
        return configured

    # 2) Fallback para LOG_DIR + LOG_FILE_NAME.
    log_file_dir = Path(os.getenv("LOG_DIR", "logs")).expanduser()
    if not log_file_dir.is_absolute():
        log_file_dir = PROJECT_DIR / log_file_dir
    return log_file_dir / os.getenv("LOG_FILE_NAME", "datasus.log")


def _setup_file_logging() -> None:
    """Configura o log (console + arquivo) respeitando `LOG_LEVEL`/`CONSOLE_LOG_LEVEL`.

    É executada automaticamente no import deste módulo (ver chamada no fim do
    arquivo). Cai para um local seguro se o alvo configurado (ex.: um disco
    removível) não estiver disponível, sem derrubar a aplicação no import."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = _resolve_log_level()
    console_level = _resolve_console_log_level()

    configured = _resolve_log_file_path()

    # Se o diretório do log não existe ou não é acessível, usa fallback local.
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        fallback = PROJECT_DIR / "cache" / "datasus.log"
        LOGGER.warning(
            "diretório de logs %s indisponível; usando fallback para %s",
            configured, fallback,
        )
        configured = fallback
        try:
            configured.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError):
            LOGGER.warning("nem mesmo fallback %s disponível; usando console apenas", configured)
            configured = None

    # Handler de console no root logger, respeitando CONSOLE_LOG_LEVEL.
    # force=True reaplica a configuração mesmo que algo já tenha chamado
    # basicConfig (ex.: import de bibliotecas de terceiros).
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(console_level)

    # Garante que o logger da aplicação emita no nível definido no .env. Sem isso
    # o logger "datasus_llm" poderia herdar um nível mais restritivo e suprimir os
    # LOGGER.debug de @log_calls.
    LOGGER.setLevel(level)

    if configured is not None:
        try:
            file_handler = RotatingFileHandler(
                str(configured), maxBytes=25 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            # O arquivo registra tudo a partir do nível definido no .env.
            file_handler.setLevel(level)
            root.addHandler(file_handler)
            LOGGER.info("logging configurado level=%s file=%s", level, configured)
        except OSError as exc:
            LOGGER.warning("não foi possível abrir o arquivo de log %s; apenas console", configured)
            if isinstance(exc, PermissionError):
                LOGGER.error("permissão negada para escrita em %s", configured)

    _CONFIGURED = True


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


# Ativa a configuração já no import do módulo. Como `log_calls` é importado por
# praticamente todos os módulos da aplicação, isso garante que o arquivo de log e
# o nível de `LOG_LEVEL` sejam aplicados antes de qualquer chamada interna ser
# registrada.
_setup_file_logging()