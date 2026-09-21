from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from api.llm_service_modular import LLMQueryService, Question
from config.settings import Settings, PROJECT_DIR
from core.logging_config import log_calls
from db.cache import DatasusCache

LOGGER = logging.getLogger("datasus_llm")

settings = Settings()

# IMPORTANTE: Inicializa o cache com proteção contra falhas.
try:
    cache = DatasusCache(settings)
except Exception as e:
    LOGGER.error("Erro crítico ao inicializar cache: %s", e)
    LOGGER.warning("Usando configuração de fallback para o cache")
    settings.cache_dir = Path(os.getenv("DATASUS_TEMP_CACHE_DIR", "/home/robinson/datasus_llm/cache"))
    settings.database_path = settings.cache_dir / "datasus.duckdb"
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache = DatasusCache(settings)
    except Exception as e2:
        LOGGER.error("Falha total ao inicializar cache: %s", e2)
        raise

service: LLMQueryService | None = None
try:
    service = LLMQueryService(cache, settings)
except Exception as e:
    LOGGER.error("Erro ao inicializar LLM service: %s", e)
    service = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Inicialização síncrona: lê e cacheia os Parquet antes de servir requisições."""
    error = None
    try:
        db_exists = cache.database_path.exists()
        cache_ready = cache._cache_table_exists()
        LOGGER.info("Sincronização síncrona do cache iniciada. db_exists=%s cache_ready=%s grupos=%s",
                    db_exists, cache_ready, list(cache.groups))

        try:
            if not cache.groups:
                # Modo legado (sem grupos): ingere apenas se o cache principal
                # ainda não tiver sido materializado.
                need_ingest = not cache._cache_table_exists()
                if need_ingest:
                    LOGGER.info("Cache principal ausente; executando ingestão...")
                    cache.ingest()
                else:
                    LOGGER.info("Cache principal já completo; pulando ingestão.")
            else:
                # >>> CORREÇÃO: ingestão SÍNCRONA e AUTOMÁTICA por grupo.
                #
                # detecta os grupos cujo cache DuckDB está ausente OU incompleto
                # (parquets faltantes) e executa a ingestão COMPLETA, bloqueando a
                # inicialização até que TODOS os grupos estejam prontos. Se, ao
                # final, ainda houver pendências, o método levanta RuntimeError,
                # o que faz a app subir em estado de erro (503 em /api/ask) em vez
                # de servir dados incompletos.
                cache.ensure_ready_sync()
        except Exception as ingest_exc:
            LOGGER.error("Erro na ingestão durante inicialização: %s", ingest_exc)
            error = str(ingest_exc)

        try:
            if not cache.groups and cache._cache_table_exists() and not cache._datasus_view_exists():
                cache.connection.execute("CREATE VIEW datasus AS SELECT * FROM datasus_cache")
        except Exception as view_exc:
            LOGGER.warning("erro ao criar view datasus: %s", view_exc)
            if error is None:
                error = str(view_exc)
        LOGGER.info("Sincronização do cache conluída (ou com erros tratados).")
    except Exception as exc:
        LOGGER.exception("Falha geral na sincronização do cache na inicialização")
        error = str(exc)
    finally:
        cache.mark_ready(error)

    yield

    try:
        if cache.connection is not None:
            cache.connection.close()
    except Exception:
        LOGGER.exception("erro ao fechar conexão principal")
    for group in cache.groups.values():
        try:
            if group.connection is not None and group.connection != cache.connection:
                group.connection.close()
        except Exception:
            LOGGER.exception("erro ao fechar conexão do grupo %s", group.name)


app = FastAPI(title="DATASUS LLM", version="1.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent.parent / "static" / "index.html")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return cache.status()


@app.post("/api/ingest")
def api_ingest() -> dict[str, Any]:
    result = cache.ingest(build_summary=True)
    cache.mark_ready()
    return result


@app.api_route("/api/build-indexes", methods=["GET", "POST"])
def api_build_indexes(request: Request) -> dict[str, Any]:
    LOGGER.info("build indexes requested method=%s", request.method)
    return cache.build_summary_tables()


@app.post("/api/ask")
def api_ask(payload: Question) -> dict[str, Any]:
    status = cache.status()
    if not cache.is_ready():
        detail = cache.startup_status() or "O cache ainda está sendo carregado. Tente novamente em instantes."
        raise HTTPException(status_code=503, detail=detail)
    if status.get("files", 0) == 0:
        return {
            "answer": "Não há dados no cache do DuckDB para responder à pergunta. Adicione arquivos Parquet em data/ e execute a ingestão antes de consultar.",
            "results": [],
            "sql": None,
            "source": "sem dados",
            "cache": {"files": 0, "names": [], "periods": set()},
            "chart": None,
        }
    if service is None:
        LOGGER.error("Serviço LLM não inicializado")
        raise HTTPException(status_code=500, detail="O serviço de consulta não foi inicializado corretamente.")
    request_id = uuid.uuid4().hex[:12]
    LOGGER.info(
        "api ask received request_id=%s tipo_grafico=%s",
        request_id,
        payload.tipo_grafico,
    )
    try:
        return service.ask(payload.question, request_id, tipo_grafico=payload.tipo_grafico)
    except Exception as error:
        LOGGER.exception("question failed request_id=%s", request_id)
        raise HTTPException(status_code=500, detail=str(error)) from error
