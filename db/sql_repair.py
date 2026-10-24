"""Reparos estaticos de SQL gerado pelo LLM.

Modulo puro (sem banco).
"""
from __future__ import annotations

import re


def fix_unterminated_strings(sql: str) -> str:
    """
    Corrige strings SQL nao terminadas (como LIKE ' sem fechamento).
    Retorna o SQL corrigido, ou o original se nao conseguir corrigir.
    """
    # Detecta strings nao balanceadas em aspas simples
    in_string = False
    escaped = False
    last_open_quote = -1

    for i, char in enumerate(sql):
        if char == "'":
            if escaped:
                escaped = False
                continue
            if in_string:
                in_string = False
            else:
                in_string = True
                last_open_quote = i
        elif char == "\\" and in_string:
            escaped = True
        else:
            escaped = False

    if not in_string:
        return sql  # Strings balanceadas, nada a corrigir

    LOGGER.warning("SQL contém string não terminada. Tentando corrigir no execute...")

    prefix = sql[:last_open_quote]  # Tudo antes da aspas aberta
    rest = sql[last_open_quote:]   # Comeca com a aspas aberta
    after_quote = rest[1:] if rest.startswith("'") else rest

    # Caso 1: Padrao LIKE 'xxx  (sem fechamento)
    like_before = re.search(r"(?:LIKE|ILIKE)\s*$", prefix, flags=re.IGNORECASE)
    if like_before:
        value = after_quote
        if value and re.match(r"^[%A-Za-z0-9._\-\s]+$", value):
            fixed_sql = sql + "'"
            LOGGER.warning("SQL: LIKE incompleto fechado. value=%r", value)
            return fixed_sql
        if value and not value.strip():
            fixed_sql = sql + "'"
            LOGGER.warning("SQL: LIKE com string vazia completado")
            return fixed_sql

    # Caso 2: Termina com operador de comparacao (ex: = ')
    before = prefix.rstrip()
    if re.search(r"(?:=|<>|!=|<=|>=)\s*$", before, flags=re.IGNORECASE):
        if after_quote and re.match(r"^[%A-Za-z0-9._\-\s]+$", after_quote):
            fixed_sql = sql + "'"
            LOGGER.warning("SQL: string após operador fechada. value=%r", after_quote)
            return fixed_sql
        fixed_sql = sql[:last_open_quote] + "''" + sql[last_open_quote+1:]
        LOGGER.warning("SQL: string vazia após operador completada")
        return fixed_sql

    # Caso 3: Fallback - apenas fecha a string com aspas simples
    if last_open_quote >= 0:
        if after_quote and re.match(r"^[%A-Za-z0-9._\-\s]+$", after_quote):
            fixed_sql = sql + "'"
            if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                fixed_sql = fixed_sql + " LIMIT 100"
            LOGGER.warning("SQL: string não terminada fechada. value=%r", after_quote)
            return fixed_sql
        fixed_sql = sql[:last_open_quote] + "''" + sql[last_open_quote+1:]
        if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
            fixed_sql = fixed_sql + " LIMIT 100"
        LOGGER.warning("SQL: string não terminada fechada com aspas vazias")
        return fixed_sql

    return sql


def fix_incomplete_sql(sql: str) -> str:
    """
    Corrige SQL truncado/incompleto (parenteses nao fechados, falta de clausulas).
    Versao estatica que tenta completar o SQL de forma segura.
    """
    result = sql
    # Balanceia parenteses
    open_parens = result.count("(") - result.count(")")
    if open_parens > 0:
        LOGGER.warning("SQL com %d parênteses não fechados no execute. Fechando...", open_parens)
        result = result + ")" * open_parens

    # Se terminar com LIKE ' sem fechamento, completa
    if re.search(r"LIKE\s*'?\s*$", result, flags=re.IGNORECASE) or \
       re.search(r"(?:=|<>|!=)\s*'?\s*$", result, flags=re.IGNORECASE):
        result = result + "'"

    # Se nao tiver LIMIT, adiciona
    if not re.search(r"\bLIMIT\s+\d+\b", result, flags=re.IGNORECASE):
        result = result.rstrip()
        if result.endswith(";"):
            result = result[:-1]
        result = result + " LIMIT 100"

    # Se nao tiver FROM, usa fallback
    if not re.search(r"\bFROM\b", result, flags=re.IGNORECASE) and \
       re.search(r"\bSELECT\b", result, flags=re.IGNORECASE):
        result = "SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT 100"
        LOGGER.warning("SQL sem FROM, usando fallback COUNT(*)")

    return result
