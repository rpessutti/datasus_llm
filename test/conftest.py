"""Configuração compartilhada do pytest para a pasta ``test/``.

Contexto (FASE P4)
------------------
Vários arquivos desta pasta começam com ``test_`` mas **não** são arquivos de
teste no sentido do pytest: são **scripts manuais** que executam código pesado
no nível do módulo (ingestão no DuckDB, criação de arquivos em ``/tmp``,
chamadas ao LLM/Ollama, ``sys.exit()``). Ao serem importados pelo pytest, eles
*abortam a coleta* (``INTERNALERROR``) ou **travam** (timeout), porque o import
do módulo roda tudo isso.

Esses arquivos continuam válidos para uso manual::

    python test/test_llm_grafic.py

Aqui apenas os **excluímos da coleta automática** (``collect_ignore``), sem
alterar o conteúdo deles. Os testes "de verdade" seguem sendo coletados.

Critério para entrar em ``_SCRIPTS_MANUAIS``:
  - o módulo executa lógica no nível de módulo (fora de ``def``/``class``),
    especialmente ``sys.exit()``, ingestão de cache, chamadas de rede; E
  - não define nenhuma função ``test_*`` nem classe ``Test*`` (nada a coletar).
"""

from __future__ import annotations

# Arquivos que executam código no import e não expõem testes coletáveis.
# (Mantidos como scripts manuais; ver docstring acima.)
_SCRIPTS_MANUAIS = [
    # scripts puros (executam no import / terminam com sys.exit)
    "test_attach_isolado.py",
    "test_integracao_completa.py",
    "test_llm_grafic.py",
    "test_validacao_fluxo.py",
    "test_python_d.py",
    "test_limite.py",
    # scripts auxiliares (prefixo manual_/sem testes), por segurança:
    "manual_duckdb_check.py",
    "manual_lifespan_check.py",
]

# Em pytest, ``collect_ignore`` opera relativo à pasta do conftest.
collect_ignore = list(_SCRIPTS_MANUAIS)
