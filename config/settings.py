"""Configurações centralizadas da aplicação DATASUS LLM.

Reconstruído para usar o modelo Llama servido via Ollama (API compatível com OpenAI).
As dependências do DeepSeek foram removidas.
"""
from __future__ import annotations

import logging
import os
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

LOGGER = logging.getLogger("datasus_llm")


def _clean_path(value: str) -> str:
    """Remove caracteres invisíveis (ex.: marcas de formatação) de um caminho."""
    value = unicodedata.normalize("NFD", value)
    cleaned = [char for char in value if unicodedata.category(char) != "Mn"]
    if cleaned != list(value):
        LOGGER.warning("caminho limpo de caracteres invisíveis: %r -> %r", value, "".join(cleaned))
    return "".join(cleaned)

def _get_windows_safe_path(raw_path: str) -> Path:
    """Converte caminhos absolutos Linux para relativos compatíveis com Windows."""
    if os.name == "nt" and not raw_path.is_absolute():
        # Usa caminho relativo ao projeto
        return PROJECT_DIR / raw_path
    return Path(_clean_path(raw_path)).expanduser()


PROJECT_DIR = Path(__file__).resolve().parent.parent

# Carrega variáveis de ambiente do arquivo .env na raiz do projeto.
load_dotenv(PROJECT_DIR / ".env")


class Settings:
    """Configurações globais lidas do ambiente (``.env`` e variáveis ``DATASUS_*``).

    Concentra os caminhos de dados/cache e os limites do DuckDB
    (``memory_limit``, ``threads``, ``temp_dir``) usados pela conexão principal e
    pelos bancos de grupo — ver ``PLANO_PERFORMANCE_CACHE.md``.
    """

    _data_dir_raw = os.getenv("DATASUS_DATA_DIR", r"D:\datasus\cache\parquet")
    _cache_dir_raw = os.getenv("DATASUS_TEMP_CACHE_DIR", r"D:\datasus\cache")
    _official_cache_dir_raw = os.getenv("DATASUS_OFFICIAL_CACHE_DIR", r"D:\datasus\cache")

    data_dir = Path(_clean_path(_data_dir_raw)).expanduser()
    cache_dir = Path(_clean_path(_cache_dir_raw)).expanduser()
    official_cache_dir = Path(_clean_path(_official_cache_dir_raw)).expanduser()

    if not data_dir.is_absolute():
        data_dir = PROJECT_DIR / data_dir
    if not cache_dir.is_absolute():
        cache_dir = PROJECT_DIR / cache_dir
    if not official_cache_dir.is_absolute():
        official_cache_dir = PROJECT_DIR / official_cache_dir

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe = cache_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("DATASUS_TEMP_CACHE_DIR indisponível (%s); usando cache local ./cache", cache_dir)
        cache_dir = PROJECT_DIR / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

    database_path = cache_dir / "datasus.duckdb"

    # DuckDB
    # PLANO_PERFORMANCE_CACHE.md, Etapas 1-2: 16 threads e 20 GB (~63% da RAM)
    # reduzem o spill; `temp_dir` fica num DISCO SEPARADO do banco (padrao
    # C:\datasus_spill) para o DuckDB nao competir consigo mesmo na mesma fila I/O.
    memory_limit = os.getenv("DATASUS_MEMORY_LIMIT", "20GB")
    threads = int(os.getenv("DATASUS_THREADS", "16"))
    _temp_dir_raw = os.getenv(
        "DATASUS_TEMP_DIR",
        "/tmp" if os.name != "nt" else r"C:\datasus_spill",
    )
    temp_dir = Path(_clean_path(_temp_dir_raw)).expanduser()
    if not temp_dir.is_absolute():
        temp_dir = PROJECT_DIR / temp_dir

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_dir_probe = temp_dir / ".write_test"
        temp_dir_probe.write_text("ok", encoding="utf-8")
        temp_dir_probe.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("DATASUS_TEMP_DIR indisponível (%s); usando cache_dir local", temp_dir)
        temp_dir = cache_dir
        temp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # LLM (Llama via Ollama — API compatível com OpenAI)
    # ------------------------------------------------------------------
    llm_enabled = os.getenv("LLM_ENABLED", "true").casefold() in {"1", "true", "yes", "on"}

    # Modelo Llama servido pelo Ollama.
    llm_model = os.getenv("LLM_MODEL", "qwen2.5-coder:7b")
    llm_base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    llm_api_key = os.getenv("LLM_API_KEY", "ollama")
    # >>> CORREÇÃO F: timeout global maior p/ o qwen3-coder:30b (prompts de ~15k chars).
    # OBS.: as chamadas de geração/correção de SQL aplicam um teto POR CHAMADA
    # (min(llm_timeout, 60)) em api/llm_service.py, então aumentar este valor NÃO
    # faz a requisição inteira travar por todo esse tempo.
    llm_timeout = float(os.getenv("LLM_TIMEOUT", "180"))
    llm_max_retries = int(os.getenv("LLM_MAX_RETRIES", "0"))

    # >>> CORREÇÃO 2 (timeout por etapa): o prompt de geração de SQL pode chegar
    # a ~30k chars (14 grupos, 462 colunas) e o prefill do qwen3-coder:30b nesse
    # caso ultrapassa os 110s (log: APITimeoutError em 112054ms com prompt de
    # 29619 chars). Em vez de um teto fixo apertado, expomos um timeout
    # ESPECÍFICO para geração de SQL, parametrizável e limitado por llm_timeout.
    llm_sql_timeout = float(os.getenv("LLM_SQL_TIMEOUT", str(llm_timeout)))
    # Correções/refazer/round-trips usam tetos menores (prompts menores).
    llm_fix_timeout = float(os.getenv("LLM_FIX_TIMEOUT", "90"))
    llm_answer_timeout = float(os.getenv("LLM_ANSWER_TIMEOUT", "90"))

    # Parâmetros de geração SQL
    llm_sql_temperature = float(os.getenv("LLM_SQL_TEMPERATURE", "0.1"))
    llm_sql_max_tokens = int(os.getenv("LLM_SQL_MAX_TOKENS", "3000"))

    # Parâmetros de geração da resposta final
    llm_answer_temperature = float(os.getenv("LLM_ANSWER_TEMPERATURE", "0.2"))
    llm_answer_max_tokens = int(os.getenv("LLM_ANSWER_MAX_TOKENS", "1500"))

    # Parâmetros de geração de gráficos
    llm_chart_temperature = float(os.getenv("LLM_CHART_TEMPERATURE", "0.2"))
    llm_chart_max_tokens = int(os.getenv("LLM_CHART_MAX_TOKENS", "2048"))

    # ------------------------------------------------------------------
    # P0-A: comentários de coluna (descrição/exemplos dos layouts) no prompt.
    # Os metadados já existem (db/column_comments.py) e eram DESCARTADOS.
    # Injetá-los no prompt melhora a escolha de coluna pelo LLM.
    # ------------------------------------------------------------------
    schema_comments_enabled = (
        os.getenv("DATASUS_SCHEMA_COMMENTS_ENABLED", "true").casefold()
        in {"1", "true", "yes", "on"}
    )
    # Truncamento (chars) do comentário por coluna — evita inflar o prompt.
    schema_comment_max_chars = int(os.getenv("DATASUS_SCHEMA_COMMENT_MAX_CHARS", "90"))

    # ------------------------------------------------------------------
    # P0-B: priorização lexical de colunas/grupos (determinística, sem ONNX).
    # ------------------------------------------------------------------
    lexical_rank_enabled = (
        os.getenv("DATASUS_LEXICAL_RANK_ENABLED", "true").casefold()
        in {"1", "true", "yes", "on"}
    )

    # ------------------------------------------------------------------
    # P2: descoberta de CID retornando TODOS os códigos da doença.
    # ``cid_discovery_max`` é um TETO DE SANIDADE (não de recall).
    # ------------------------------------------------------------------
    cid_discovery_max = int(os.getenv("DATASUS_CID_DISCOVERY_MAX", "200"))


setting = Settings()
DATASUS_DATA_DIR = setting.data_dir