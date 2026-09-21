""">>> Subpacote gerado a partir de api/llm_service.py (original preservado) <<<

Este pacote contém os segmentos extraídos VERBATIM de ``api/llm_service.py``
(o arquivo original permanece intocado e serve como oráculo de controle).

Cada módulo define um mixin que herda de ``_ServiceBase`` (``base.py``):

- ``_constants.py``      : símbolos de módulo (imports, ``_extract_sql_fallback``,
                           ``Question``, ``OutOfScopeQuestion`` e constantes).
- ``base.py``            : ``_ServiceBase`` (``__init__`` e helpers básicos).
- ``prompt.py``          : ``_PromptMixin`` (orientações/exemplos do prompt).
- ``cid.py``             : ``_CidMixin`` (descoberta, grounding e validação de CID).
- ``sql_fix.py``         : ``_SqlFixMixin`` (correções determinísticas de SQL).
- ``generate.py``        : ``_GenerateMixin`` (geração e validação do SQL).
- ``review.py``          : ``_ReviewMixin`` (correção/refação via LLM, sumarização).
- ``ask.py``             : ``_AskMixin`` (fluxo principal ``ask``).

A fachada ``api.llm_service_modular.LLMQueryService`` reúne todos os mixins
para reconstruir a API pública da classe original.
"""

from __future__ import annotations

__all__ = [
    "base",
    "prompt",
    "cid",
    "sql_fix",
    "generate",
    "review",
    "ask",
]