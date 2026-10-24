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

# IMPORTANTE: Inicializa o cache com protecao contra falhas.
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
    """Inicializacao sincrona: le e cacheia os Parquet antes de servir requisicoes."""
    error = None
    try:
        db_exists = cache.database_path.exists()
        LOGGER.info("Sincronização síncrona do cache iniciada. db_exists=%s grupos=%d",
                    db_exists, len(cache.groups))

        try:
            # Ingestao SINCRONA e AUTOMATICA por grupo: detecta os grupos cujo
            # cache DuckDB esta ausente OU incompleto (parquets faltantes) e
            # executa a ingestao completa, bloqueando a inicializacao ate que
            # todos estejam prontos. Pendencias remanescentes levantam
            # RuntimeError (a app sobe em estado de erro e /api/ask responde
            # 503 em vez de servir dados incompletos).
            cache.ensure_ready_sync()
        except Exception as ingest_exc:
            LOGGER.error("Erro na ingestão durante inicialização: %s", ingest_exc)
            error = str(ingest_exc)
        LOGGER.info("Sincronização do cache concluída (ou com erros tratados).")
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
    """Entrega a pagina HTML do assistente."""
    return FileResponse(Path(__file__).resolve().parent.parent / "static" / "index.html")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    """Retorna o estado do cache (grupos, linhas estimadas, resumos)."""
    return cache.status()


@app.post("/api/ingest")
def api_ingest() -> dict[str, Any]:
    """Executa a ingestao dos grupos e reconstroi os resumos."""
    result = cache.ingest(build_summary=True)
    cache.mark_ready()
    return result


@app.api_route("/api/build-indexes", methods=["GET", "POST"])
def api_build_indexes(request: Request) -> dict[str, Any]:
    """Reconstroi as tabelas agregadas ``resumo_*`` (GET ou POST)."""
    LOGGER.info("build indexes requested method=%s", request.method)
    return cache.build_summary_tables()


@app.post("/api/ask")
def api_ask(payload: Question) -> dict[str, Any]:
    """Responde uma pergunta em linguagem natural sobre os dados do cache."""
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
