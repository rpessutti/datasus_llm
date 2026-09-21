""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'base': classe _ServiceBase com constantes de classe, __init__,
_extract_limit_from_question e _determine_relevant_grupos.
Copiado VERBATIM do original (indentação de 4 espaços preservada).
"""
from __future__ import annotations

import logging
import re

from config.settings import Settings
from db.cache import DatasusCache
from api.llm_grafic import LLMGrafic

from api.llm_service_parts._constants import (
    _SYSTEM_DATE_FUNCTIONS,
    _RAW_YEAR_COLUMNS,
    CID10_SCHEMA_HINT,
)

LOGGER = logging.getLogger("datasus_llm")


class _ServiceBase:
    # >>> CORREÇÃO DATA: funções de data que NÃO existem/malfuncionam na base. <<<
    # São usadas para (a) instruir o LLM a não usá-las e (b) detectá-las/removê-las
    # deterministicamente do SQL gerado (ver _fix_system_date_functions).
    _SYSTEM_DATE_FUNCTIONS = _SYSTEM_DATE_FUNCTIONS

    # Schema da tabela de CID-10 disponível no DuckDB (carregada pelo cache).
    CID10_SCHEMA_HINT = CID10_SCHEMA_HINT

    # Colunas "cruas" de ano presentes nos grupos DATASUS.
    _RAW_YEAR_COLUMNS = _RAW_YEAR_COLUMNS

    def __init__(self, cache: DatasusCache, settings: Settings) -> None:
        self.cache = cache
        # Referência para a instância Settings (permite ler configurações centralizadas
        # de temperature/max_tokens sem duplicar defaults nesta classe - sugestão 7)
        self.settings = settings
        self.llm_enabled = settings.llm_enabled
        # Modelo Llama servido via Ollama (API compatível com OpenAI).
        self.model = settings.llm_model
        self.client = None
        if self.llm_enabled:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                max_retries=settings.llm_max_retries,
            )
 
        # Inicializa o módulo de gráficos LLM
        self.llm_grafic = LLMGrafic(settings)
        LOGGER.info(
            "llm configuration enabled=%s client_ready=%s model=%s base_url=%s",
            self.llm_enabled,
            self.client is not None,
            self.model,
            settings.llm_base_url,
        )

    @staticmethod
    def _extract_limit_from_question(question: str) -> int:
        """
        Extrai o limite numérico da pergunta do usuário.
        Exemplos:
          "quais os 10 municipios com mais..." -> 10
          "quais os 5 estados com mais..." -> 5
          "quais os 20 cidades..." -> 20
          "quais os 3 primeiros..." -> 3
        Se nenhum número explícito for encontrado, retorna 100 (padrão).
        """
        # Padrões comuns como "os 10 municipios", "os 5 estados", "as 20 cidades", "os 3 primeiros"
        padrao_os = re.search(
            r"\b(?:os|as|o|a)\s+(\d{1,3})\s+(?:municipios|municípios|cidades|estados|ufs|regioes|regiões|primeiros|primeiras|maiores|menores)",
            question,
            flags=re.IGNORECASE,
        )
        if padrao_os:
            limite = int(padrao_os.group(1))
            return min(limite, 100)

        # Padrões como "top 10", "Top 5", "top 20 municipios"
        padrao_top = re.search(
            r"\b(?:top)\s+(\d{1,3})",
            question,
            flags=re.IGNORECASE,
        )
        if padrao_top:
            limite = int(padrao_top.group(1))
            return min(limite, 100)

        # Padrões genericos: "10 municipios" ou "5 estados"
        padrao_simples = re.search(
            r"\b(\d{1,3})\s+(?:municipios|municípios|cidades|estados|ufs|regioes|regiões|primeiros|primeiras|maiores|menores)",
            question,
            flags=re.IGNORECASE,
        )
        if padrao_simples:
            limite = int(padrao_simples.group(1))
            return min(limite, 100)

        # Padrões como "quais os 10" ou "quais as 5"
        padrao_quais = re.search(
            r"\b(?:quais|qual|quanto|quantos|quantas)\s+(?:os|as|o|a|sao|são)?\s*(\d{1,3})\b",
            question,
            flags=re.IGNORECASE,
        )
        if padrao_quais:
            limite = int(padrao_quais.group(1))
            return min(limite, 100)

        # Se nada encontrado, retorna 100 (padrão)
        return 100

    def _determine_relevant_grupos(self, question: str, periods: set[tuple[str, str]]) -> list[str]:
        """
        Determina quais grupos são relevantes para a pergunta,
        baseado nos períodos selecionados e nos arquivos do cache.
        """
        try:
            files = self.cache.select_files_for_question(question)
            if not files:
                if self.cache.groups:
                    return list(self.cache.groups.keys())
                return ["_legacy"]
            
            grupos_set: set[str] = set()
            for path in files:
                parts = path.parts
                for name in self.cache.groups:
                    if name in parts:
                        grupos_set.add(name)
                        break
            
            if grupos_set:
                return sorted(grupos_set)
        except Exception as e:
            LOGGER.warning("erro ao determinar grupos relevantes: %s", e)
        
        if self.cache.groups:
            return list(self.cache.groups.keys())
        return ["_legacy"]