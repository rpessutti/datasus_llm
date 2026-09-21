"""
Constantes compartilhadas da aplicação DATASUS LLM.

Centraliza listas e valores que antes estavam duplicados/hardcoded
em múltiplos módulos, facilitando manutenção e expansão futura.
"""
from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Colunas de diagnóstico/CID reconhecidas nos dados DATASUS
# ---------------------------------------------------------------------------
# IMPORTANTE: Esta lista centraliza todos os nomes possíveis de colunas
# que contenham códigos de diagnóstico (CID) nos arquivos Parquet do DATASUS.
# Ao adicionar novos formatos/grupos de dados, basta incluir aqui.
#
# Estratégia de detecção: uma coluna é considerada de diagnóstico se:
#   1. Seu nome (sem case) estiver listado em DIAGNOSIS_COLUMN_NAMES, OU
#   2. Sua terminação corresponder aos padrões em DIAGNOSIS_COLUMN_SUFFIXES
DIAGNOSIS_COLUMN_NAMES: Final[tuple[str, ...]] = (
    # SIA / APAC
    "AP_CIDPRI", "AP_CIDSEC", "AP_CIDCAS",
    "AP_CID_C1", "AP_CID_C2", "AP_CID_C3", "AP_CID_C4", "AP_CID_C5",
    "AP_CID_CO",
    # SIH / AIH
    "AQ_CID10", "AR_CID10",
    # SIS / Diversos
    "CIDPRI", "CIDPRI1", "CIDPRIO",  # Aliases que aparecem em formatos legados
    "DIAG_PRINC", "DIAG_SECUN",
    "CID_NOTIF", "CID_ASSO", "CID_MORTE",
    # SIH/SP - SP (produção hospitalar)
    "SP_CIDPRI", "SP_CIDSEC",
    # PA / Produção Ambulatorial
    "PA_CIDPRI", "PA_CIDSEC", "PA_CIDCAS",
    # SIM / Sistema de Informação de Mortalidade
    "CAUSABAS", "CAUSABAS_O", "CAUSAMORTE", "CAUSA_MORTE",
    # SINASC / Nascidos Vivos
    "CO_CIDPRIM", "CO_CIDSEC",
)

# Sufixos comuns para detectar colunas que NÃO estão na lista explícita
# mas que claramente contêm diagnósticos CID (ex.: "{prefixo}_CIDPRIM")
DIAGNOSIS_COLUMN_SUFFIXES: Final[tuple[str, ...]] = (
    "_CIDPRI", "_CIDSEC", "_CIDCAS", "_CID_C",
    "_CIDPRIM",
    "_CID10", "_CID",
)

# ---------------------------------------------------------------------------
# Colunas de METADADOS DE ARQUIVO (NÃO são dados do DATASUS)
# ---------------------------------------------------------------------------
# Estas colunas são derivadas do NOME/CAMINHO do arquivo Parquet (competência
# de remessa/processamento), NÃO da data real de atendimento. Elas NUNCA devem
# ser usadas para filtrar/agrupar respostas de consultas — o recorte temporal
# é feito pelas colunas NATIVAS de período e exposto como ``atend_ano``/
# ``atend_mes`` (ver db/period_fields.py).
#
# Devem ser REMOVIDAS do dicionário de colunas para não vazarem ao prompt do
# LLM (que passava a usá-las como filtro, gerando erros como
# 'Referenced column "parquet_year" not found').
FILE_METADATA_COLUMNS: Final[tuple[str, ...]] = (
    "filename",
    "parquet_path",
    "parquet_year",
    "parquet_month",
)

# ---------------------------------------------------------------------------
# Tempo de resposta/limites padrão
# ---------------------------------------------------------------------------
DEFAULT_QUERY_LIMIT: Final[int] = 100
DEFAULT_FETCH_ROWS: Final[int] = 100
MAX_QUERY_LIMIT: Final[int] = 100

# ---------------------------------------------------------------------------
# Tipos de gráfico aceitos
# ---------------------------------------------------------------------------
CHART_TYPES: Final[tuple[str, ...]] = (
    "pizza", "donut", "horizontal", "vertical",
    "linha", "area", "dispersao", "radar",
)

# ---------------------------------------------------------------------------
# Palavras-chave para detecção automática de tipo de gráfico
# ---------------------------------------------------------------------------
CHART_TYPE_KEYWORDS: Final[dict[str, list[str]]] = {
    "donut": ["donut", "rosca", "anel", "rosquinha", "gráfico de rosca", "grafico de rosca"],
    "pizza": ["pizza", "pie", "circular", "torta"],
    "horizontal": [
        "horizontal",
        "barras horizontais",
        "barra lateral",
        "barras laterais",
        "lateral",
        "deitado",
    ],
    "vertical": ["vertical", "barras verticais", "colunas"],
    "linha": [
        "line",
        "gráfico de linha",
        "grafico de linha",
        "tendência",
        "tendencia",
        "evolução",
        "evolucao",
        "série temporal",
        "serie temporal",
        "serie",
        "série",
    ],
    "area": ["área", "area", "superfície", "superficie", "stacked area"],
    "dispersao": [
        "dispersão",
        "dispersao",
        "scatter",
        "pontos",
        "correlação",
        "correlacao",
    ],
    "radar": ["radar", "teia", "aranha", "spider"],
}