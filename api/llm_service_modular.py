""">>> Fachada modular gerada a partir de api/llm_service.py (original preservado) <<<

Este módulo reúne os mixins extraídos em ``api/llm_service_parts/`` para
reconstruir a classe ``LLMQueryService`` com a MESMA API pública do original.

O arquivo ``api/llm_service.py`` permanece INTACTO e serve como oráculo de
controle (golden). Para migrar a aplicação para esta versão modular, basta
trocar o import em ``api/routes.py``::

    # de:
    from api.llm_service import LLMQueryService, Question
    # para:
    from api.llm_service_modular import LLMQueryService, Question

Símbolos reexportados (espelhando o original):
- ``LLMQueryService``
- ``Question``
- ``OutOfScopeQuestion``
- ``FORA_ESCOPO_MESSAGE``
"""
from __future__ import annotations

# Reexporta a API pública do original (models, exceção e mensagem).
from api.llm_service_parts._constants import (
    FORA_ESCOPO_MESSAGE,
    OutOfScopeQuestion,
    Question,
)

from api.llm_service_parts.base import _ServiceBase
from api.llm_service_parts.prompt import _PromptMixin
from api.llm_service_parts.cid import _CidMixin
from api.llm_service_parts.sql_fix import _SqlFixMixin
from api.llm_service_parts.generate import _GenerateMixin
from api.llm_service_parts.review import _ReviewMixin
from api.llm_service_parts.ask import _AskMixin


class LLMQueryService(
    _AskMixin,
    _ReviewMixin,
    _GenerateMixin,
    _SqlFixMixin,
    _CidMixin,
    _PromptMixin,
    _ServiceBase,
):
    """Serviço de consulta LLM reconstruído a partir dos mixins modularizados.

    Herda (sem redefinir nada) todos os métodos do ``LLMQueryService`` original.
    A ordem das bases não altera o comportamento, pois não há métodos
    homônimos entre os mixins (verificado por introspecção).
    """


__all__ = [
    "LLMQueryService",
    "Question",
    "OutOfScopeQuestion",
    "FORA_ESCOPO_MESSAGE",
]
