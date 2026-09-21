""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Símbolos de nível de módulo do api/llm_service.py:
imports compartilhados, _extract_sql_fallback, FORA_ESCOPO_MESSAGE,
OutOfScopeQuestion e Question. Copiado VERBATIM do original.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from config.settings import Settings
from core.logging_config import log_calls
from config.constants import DIAGNOSIS_COLUMN_NAMES
from db.period_fields import ATEND_ANO, ATEND_MES
from db.cache import DatasusCache
from db.column_dictionary import CONCEPT_LABELS, translate_value_for_column
from api.llm_grafic import LLMGrafic

LOGGER = logging.getLogger("datasus_llm")


def _extract_sql_fallback(content: str | None) -> str:
    """Extração de SQL independente de estado (rede de segurança).

    Usada por :meth:`LLMQueryService._safe_extract_sql` quando o método
    ``_extract_sql`` não existe na instância (ex.: regressão de código em que a
    definição ficou fora da classe). Evita que a ausência do método derrube a
    requisição inteira com HTTP 500 (AttributeError).
    """
    if not content or not content.strip():
        raise ValueError("O LLM não retornou uma consulta SQL.")
    texto = content.strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", texto, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        texto = fenced.group(1).strip()
    else:
        stmt = re.search(r"\b(?:SELECT|WITH)\b", texto, flags=re.IGNORECASE)
        if stmt:
            texto = texto[stmt.start():].strip()
    texto = texto.strip().strip("`").strip()
    m2 = re.search(r"\b(?:WITH|SELECT)\b.*", texto, flags=re.DOTALL | re.IGNORECASE)
    if m2:
        texto = m2.group(0)
    texto = texto.rstrip().rstrip(";")
    if not texto:
        raise ValueError("O LLM não retornou uma consulta SQL.")
    return texto


# >>> NOVO: Mensagem padrão amigável para perguntas fora do escopo DATASUS <<<
FORA_ESCOPO_MESSAGE = (
    "😊 Desculpe, mas eu sou especializado em dados de saúde pública do DATASUS/SUS "
    "(internações, atendimentos, diagnósticos/CID, municípios, sexo, período etc.). "
    "Não consigo responder a essa pergunta, pois ela não está relacionada a esses dados.\n\n"
    "Posso te ajudar com perguntas como:\n"
    "• Quantos casos de dengue foram registrados em 2024?\n"
    "• Quais os 10 municípios com mais atendimentos nos últimos 3 anos?\n"
    "• Qual a evolução dos casos de sarampo por ano e sexo?\n\n"
    "Reformule sua pergunta focando em dados de saúde do DATASUS/SUS e eu terei prazer em ajudar! 🙂"
)


class OutOfScopeQuestion(Exception):
    """Exceção levantada quando a pergunta não é sobre dados de saúde do DATASUS/SUS."""


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    tipo_grafico: str | None = Field(
        default=None,
        description="Tipo de gráfico desejado: pizza, donut, horizontal, vertical, linha, area, dispersao, radar. Se None, detecta automaticamente da pergunta.",
    )


# Constantes de classe do LLMQueryService (usadas pelos segmentos base/cid/sql_fix).
_SYSTEM_DATE_FUNCTIONS = (
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "CURRENT_TIME",
    "CURRENT",
    "NOW",
    "LOCALTIME",
    "LOCALTIMESTAMP",
    "GETDATE",
    "SYSDATE",
    "TODAY",
)

# Colunas "cruas" de ano presentes nos grupos DATASUS.
_RAW_YEAR_COLUMNS = ("ANO_CMPT", "NU_ANO", "SP_AA", "AP_ANO", "CO_ANO", "ANO")

# Schema da tabela de CID-10 disponível no DuckDB (carregada pelo cache).
CID10_SCHEMA_HINT = (
    "BASE DE CID-10 DISPONÍVEL NO DUCKDB: tabela `cid10` "
    "(colunas: subcat VARCHAR [código SEM ponto, ex.: 'A90', 'B019'], "
    "descricao VARCHAR [nome oficial], descabrev VARCHAR [nome curto]). "
    "Use esta tabela para DESCOBRIR o código CID correto da doença citada. "
    "Exemplo: SELECT subcat, descricao FROM cid10 "
    "WHERE UPPER(descricao) LIKE '%VARICELA%' ORDER BY subcat;"
)