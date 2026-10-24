"""Parse de periodo em linguagem natural (anos, meses, intervalos, ultimos N).

Modulo puro (sem banco): usado por DatasusCache e pelos testes.
"""
from __future__ import annotations

import re
import unicodedata


def parse_periods(
    question: str, available_periods: set[tuple[str, str]]
) -> tuple[set[str], set[str], set[tuple[str, str]] | None, bool]:
    """Interpreta a pergunta e devolve o recorte temporal (fonte unica).

    Extraido de ``select_files_for_question`` para ser reutilizado tanto no
    modo arquivos (Parquet) quanto no modo grupos (DuckDB), sem duplicar a
    logica de linguagem natural (anos, meses, intervalos, "ultimos N anos").

    Retorna (years, months, recent_period_keys, agrupar_por_mes).
    """
    year_values = [int(value) for value in re.findall(r"\b(20\d{2})\b", question)]
    years = {str(value)[-2:] for value in year_values}
    month_names = {
        "janeiro": "01", "fevereiro": "02", "marco": "03", "abril": "04",
        "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
        "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
    }
    normalized_question = "".join(
        char for char in unicodedata.normalize("NFD", question.casefold())
        if unicodedata.category(char) != "Mn"
    )
    range_match = re.search(r"\b(20\d{2})\s*(?:a|ate|até|e)\s*(20\d{2})\b", normalized_question)
    if range_match:
        first_year, last_year = map(int, range_match.groups())
        if first_year > last_year:
            first_year, last_year = last_year, first_year
        years = {str(year)[-2:] for year in range(first_year, last_year + 1)}
    # >>> CORRECAO: Interpreta expressoes com "hoje"/"atual" (ex: "2023 ate hoje") usando o
    # ano mais recente disponivel no cache local <<<
    elif "hoje" in normalized_question or "atual" in normalized_question:
        if not available_periods:
            raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
        # Detecta padrao "2023 ate hoje" ou "de 2023 a hoje"
        hoje_base = re.search(r"\b(20\d{2})\s*(?:a|ate|até|)\s*hoje\b", normalized_question)
        recent_available = sorted({int(period[0]) for period in available_periods}, reverse=True)
        if not recent_available:
            raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
        if hoje_base:
            first_hoje = int(hoje_base.group(1))
            last_hoje = recent_available[0]
            if first_hoje > last_hoje:
                first_hoje, last_hoje = last_hoje, first_hoje
            years = {str(year)[-2:] for year in range(first_hoje, last_hoje + 1)}
        else:
            # "ate hoje" sem ano inicial = usar TODOS os anos disponiveis no cache
            # para nao perder dados (o usuario provavelmente quer a serie completa)
            years = {str(year)[-2:] for year in recent_available}
    months = {month_names[name] for name in month_names if name in normalized_question}
    months.update(re.findall(r"\b(?:mes|m[eê]s)\s*(0?[1-9]|1[0-2])\b", normalized_question))
    months.update(re.findall(r"\b(0?[1-9]|1[0-2])\s*[/.-]\s*20\d{2}\b", normalized_question))
    months.update(re.findall(r"\b20\d{2}\s*[/.-]\s*(0?[1-9]|1[0-2])\b", normalized_question))
    months = {month.zfill(2) for month in months}

    # >>> CORRECAO 1: numero POR EXTENSO em periodos relativos ("dois anos").
    # Antes o regex exigia digitos (\d+), entao "nos ultimos dois anos" nao
    # casava, recent_match ficava None e a funcao levantava ValueError. <<<
    numeros_extenso = {
        "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
        "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
        "onze": 11, "doze": 12,
    }
    # >>> CORRECAO 2: "agrupe por mes"/"por mes"/"mensal" indicam AGRUPAMENTO,
    # nao selecao de um mes especifico. Marcamos a intencao para nao restringir a
    # selecao a um mes e para propagar a dica de GROUP BY atend_mes. <<<
    agrupar_por_mes = bool(
        re.search(r"\bmes(?:es)?\b", normalized_question)
        and ("agrup" in normalized_question or "por mes" in normalized_question
             or "mensal" in normalized_question or "mes a mes" in normalized_question)
    )
    recent_match = re.search(
        r"\b(?:nos?\s+)?(?:últim\w*|ultim\w*|utlim\w*)\s+?(?:(\d+|[a-z]+)\s+)?(anos?|mes(?:es)?|ano)\b",
        normalized_question,
    )
    if not recent_match:
        # Tenta novamente sem o qualificador "nos " e com variacoes ainda mais flexiveis
        recent_match = re.search(
            r"\b(?:ultim\w*|utlim\w*|últim\w*)\s+?(?:(\d+|[a-z]+)\s+)?(anos?|mes(?:es)?|ano)\b",
            normalized_question,
        )
    if recent_match:
        raw_amount = recent_match.group(1)
        if raw_amount is None:
            amount = 1
        elif raw_amount.isdigit():
            amount = int(raw_amount)
        else:
            # Numero por extenso ("dois", "tres", ...). Palavra desconhecida
            # (ex.: "alguns") assume 1 para nao interromper a consulta.
            amount = numeros_extenso.get(raw_amount, 1)
        unit = recent_match.group(2)
        if amount < 1:
            raise ValueError("O período dos últimos anos ou meses deve ser maior que zero.")
        if not available_periods:
            raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
        if unit.startswith("ano"):
            recent_years = sorted({period[0] for period in available_periods}, reverse=True)[:amount]
            if not recent_years:
                raise ValueError("Não há anos disponíveis no cache para o período solicitado.")
            years = {year[-2:] for year in recent_years}
            # >>> CORRECAO 2: so zera o filtro de mes quando NAO e agrupamento
            # mensal (no agrupamento, o mes e separado pelo GROUP BY). <<<
            if not agrupar_por_mes:
                months = set()
            recent_period_keys = None
        else:
            recent_periods = sorted(available_periods, reverse=True)[:amount]
            if not recent_periods:
                raise ValueError("Não há períodos disponíveis no cache para o período solicitado.")
            recent_period_keys = set(recent_periods)
    else:
        recent_period_keys = None
    if not years and not months:
        if recent_match and not available_periods:
            raise ValueError("Não há períodos disponíveis no cache para interpretar a pergunta.")
        if not recent_match:
            raise ValueError("Informe na pergunta um ano, mês ou período relativo válido para selecionar os Parquets.")
    return years, months, recent_period_keys, agrupar_por_mes


def wants_monthly_grouping(question: str) -> bool:
    """True quando a pergunta pede AGRUPAMENTO por mes (ex.: 'agrupe por mes').

    Reconhece variacoes como 'agrupe por mes', 'por mes', 'mensal' e
    'mes a mes'. E usado pelo LLMQueryService para instruir o SQL a agrupar
    pela coluna JA EXISTENTE ``atend_mes`` da view ``datasus_query_cache``
    (sem inventar colunas de mes).
    """
    normalized_question = "".join(
        char for char in unicodedata.normalize("NFD", question.casefold())
        if unicodedata.category(char) != "Mn"
    )
    # Referencia a MES em qualquer forma: 'mes'/'meses', 'mensal',
    # 'mensalmente' etc. Usamos PREFIXO (nao limite de palavra) porque
    # 'mensalmente'/'mensal' NAO contem o token isolado 'mes'.
    menciona_mes = bool(
        re.search(r"\bmes(?:es)?\b", normalized_question)
        or "mensal" in normalized_question
    )
    if not menciona_mes:
        return False
    # Indicios EXPLICITOS de agrupamento (mesmo sem 'mes' isolado):
    # 'agrupe/agrupado/agrupamento' + referencia a mes -> agrupamento.
    if "agrup" in normalized_question:
        return True
    # 'mensal'/'mensalmente' sozinhos ja indicam agrupamento por mes.
    if "mensal" in normalized_question:
        return True
    # Expressoes que pedem quebra por mes.
    return bool(
        "por mes" in normalized_question
        or "mes a mes" in normalized_question
        or "ao mes" in normalized_question
        or "cada mes" in normalized_question
    )
