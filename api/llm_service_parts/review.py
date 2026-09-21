""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'review': mixin _ReviewMixin com correção/refação de SQL via LLM,
detecção de resposta fora de escopo, sumarização, tradução de linhas
para resposta e revisão de resultado vazio.
Copiado VERBATIM do original.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from core.logging_config import log_calls
from db.column_dictionary import translate_value_for_column

from api.llm_service_parts.base import _ServiceBase

LOGGER = logging.getLogger("datasus_llm")


class _ReviewMixin(_ServiceBase):
    @log_calls
    def _request_sql_fix(
        self,
        question: str,
        sql_atual: str,
        request_id: str,
        periods: set[tuple[str, str]],
        limite: int,
        real_columns: list[str],
        problemas: list[str],
        cids_corretos: list[str] | None = None,
    ) -> str:
        """Solicita ao LLM que corrija problemas na query SQL.

        Originalmente tratava apenas de colunas inexistentes; agora também cobre
        CID incorreto (via ``cids_corretos``) — caso em que o prompt é
        reformulado para falar explicitamente de CÓDIGO CID, e não de coluna
        (o framing anterior fazia o LLM devolver a SQL inalterada, pois as
        colunas existiam e ele concluía que nada havia para corrigir).
        """
        schema = json.dumps({"columns": [{"column": col} for col in real_columns]}, ensure_ascii=False, default=str)
        if cids_corretos:
            cid_txt = ", ".join(f"'{c}%'" for c in cids_corretos)
            prompt = (
                "Você precisa corrigir uma consulta SQL DuckDB que usa um CÓDIGO CID-10 "
                "INCORRETO para a doença citada na pergunta.\n"
                f"Problemas encontrados: {problemas}\n"
                f"CID(s) CORRETO(S) para esta doença (use EXATAMENTE um destes): {cid_txt}\n"
                f"Colunas reais disponíveis: {schema}\n"
                f"Consulta SQL atual (com CID errado): {sql_atual}\n"
                "Reescreva a consulta TROCANDO o código errado na cláusula "
                "UPPER(TRIM(CAST(\"ColunaCID\" AS VARCHAR))) LIKE '<CODIGO>%' pelo código "
                "correto acima. Mantenha todo o resto da consulta igual. "
                "Retorne SOMENTE a SQL corrigida, sem markdown e sem explicações.\n"
                f"Pergunta original: {question}\n"
                f"Limite: {limite}"
            )
        else:
            prompt = (
                "Você precisa corrigir uma consulta SQL DuckDB que contém referências a colunas que não existem.\n"
                f"Problemas encontrados (colunas inexistentes): {problemas}\n"
                f"Colunas reais disponíveis: {schema}\n"
                f"Consulta SQL atual (inválida): {sql_atual}\n"
                "Reescreva a consulta substituindo as colunas inexistentes pelas colunas reais mais adequadas. "
                "Retorne SOMENTE a SQL corrigida, sem markdown e sem explicações.\n"
                f"Pergunta original: {question}\n"
                f"Limite: {limite}"
            )
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.settings.llm_sql_temperature,
            max_tokens=self.settings.llm_sql_max_tokens,
            timeout=min(self.settings.llm_timeout, self.settings.llm_fix_timeout),  # >>> teto por chamada <<<
            messages=[{"role": "user", "content": prompt}],
        )
        llm_response = response.choices[0].message.content or ""
        sql_corrigido = self._extract_sql(llm_response)
        LOGGER.info("solicitação de correção de SQL concluída request_id=%s", request_id)
        return sql_corrigido

    @log_calls
    def _request_sql_refazer(
        self,
        question: str,
        sql_atual: str,
        request_id: str,
        periods: set[tuple[str, str]],
        limite: int,
        real_columns: list[str],
        motivo: str,
    ) -> str:
        """Solicita ao LLM que refaça completamente a query SQL, fornecendo o motivo da falha anterior."""
        schema = json.dumps({"columns": [{"column": col} for col in real_columns]}, ensure_ascii=False, default=str)
        prompt = (
            "A consulta SQL anterior falhou ao ser executada ou validada no DuckDB.\n"
            f"Motivo da falha: {motivo}\n"
            f"Consulta SQL que falhou: {sql_atual}\n"
            f"Colunas reais disponíveis: {schema}\n"
            f"Períodos selecionados: {sorted(periods)}\n"
            "Escreva uma NOVA consulta SQL completa e válida que responda à pergunta do usuário, "
            "corrigindo o problema descrito no motivo da falha. "
            "Regras: "
            "- Use somente SELECT/WITH (nada de INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, COPY, etc.) "
            "- Use somente a view datasus_query_cache (ou tabelas resumo_* se aplicável) "
            "- Use somente as colunas listadas no schema acima, com aspas duplas quando necessário "
            "- A query deve ser COMPLETA e VÁLIDA, sem truncamentos "
            "- Retorne SOMENTE a SQL corrigida, sem markdown, sem texto adicional "
            f"\nPergunta original: {question}\n"
            f"Limite de linhas: {limite}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.settings.llm_sql_temperature,
            max_tokens=self.settings.llm_sql_max_tokens,
            timeout=min(self.settings.llm_timeout, self.settings.llm_fix_timeout),  # >>> teto por chamada <<<
            messages=[{"role": "user", "content": prompt}],
        )
        llm_response = response.choices[0].message.content or ""
        sql_corrigido = self._extract_sql(llm_response)
        LOGGER.info("solicitação de refazer SQL concluída request_id=%s sql_length=%d", request_id, len(sql_corrigido))
        return sql_corrigido

    @staticmethod
    def _is_out_of_scope_response(content: str | None) -> bool:
        """
        Detecta se a resposta do LLM é uma recusa em responder (pergunta fora do escopo DATASUS)
        em vez de uma consulta SQL.

        Regras:
          - Resposta vazia NÃO é considerada fora do escopo (é tratada como erro de SQL).
          - Se houver uma declaração SQL (SELECT/WITH) em bloco de código ```sql``` ou iniciando
            a resposta, NÃO é fora do escopo.
          - Caso contrário, se houver indicadores textuais de recusa (padrões abaixo),
            considera fora do escopo.
        """
        if not content or not content.strip():
            return False

        text = content.strip()

        # >>> NOVO: prefixo explícito recomendado no prompt <<<
        # Quando o LLM segue a instrução, ele inicia a resposta com 'FORA_ESCOPO:'.
        if re.match(r"^fora[_\s]?escopo\s*:", text, flags=re.IGNORECASE):
            return True

        # Se houver um bloco de código com SQL, não é recusa.
        if re.search(r"```\s*(?:sql)?\s*(?:select|with)\b", text, flags=re.IGNORECASE):
            return False

        # Se a resposta (após limpeza de markdown) começa com SELECT/WITH, não é recusa.
        cleaned = text.strip("`").lstrip()
        if re.match(r"^(?:select|with)\b", cleaned, flags=re.IGNORECASE):
            return False

        # Se houver qualquer SELECT/WITH na resposta, tratamos como tentativa de SQL.
        if re.search(r"\b(?:select|with)\b", text, flags=re.IGNORECASE):
            return False

        # Indicadores típicos de recusa por escopo.
        recusa_markers = (
            "só posso ajudar",
            "so posso ajudar",
            "somente posso ajudar",
            "não posso ajudar",
            "nao posso ajudar",
            "não posso responder",
            "nao posso responder",
            "não sou capaz",
            "nao sou capaz",
            "não é possível responder",
            "nao e possivel responder",
            "não está relacionada",
            "nao esta relacionada",
            "não está relacionado",
            "nao esta relacionado",
            "fora do escopo",
            "dados de saúde",
            "dados de saude",
            "datasus/sus",
            "datasus",
            "não consigo responder",
            "nao consigo responder",
            "sou especializado",
            "focada em saúde",
            "focada em saude",
        )
        text_lower = text.casefold()
        if any(marker in text_lower for marker in recusa_markers):
            return True

        # Sem SQL e sem indicadores claros de recusa: não classificamos como fora do escopo.
        return False

    @staticmethod
    def _translate_rows_for_answer(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Anexa rótulos legíveis (ex.: sexo '2' -> 'Feminino') às linhas.

        Mantém as chaves originais intactas e adiciona ``"<coluna>_label"``
        apenas quando há tradução disponível e diferente do valor original.
        """
        if not rows:
            return rows
        traduzidas: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                traduzidas.append(row)
                continue
            nova = dict(row)
            for col, valor in row.items():
                label = translate_value_for_column(col, valor)
                if label is not None and label != valor:
                    nova[f"{col}_label"] = label
            traduzidas.append(nova)
        return traduzidas

    def _summarize(self, question: str, sql: str, rows: list[dict[str, Any]], request_id: str) -> str:
        started = time.perf_counter()
        LOGGER.info("llm answer request start request_id=%s rows=%d", request_id, len(rows))

        rows_para_resposta = self._translate_rows_for_answer(rows)

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.settings.llm_answer_temperature,
            max_tokens=self.settings.llm_answer_max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Responda em português, de forma objetiva, usando somente estes resultados DATASUS. Pergunta: "
                        f"{question}\nSQL: {sql}\nResultados: "
                        f"{json.dumps(rows_para_resposta, default=str, ensure_ascii=False)}"
                    ),
                }
            ],
        )
        answer = response.choices[0].message.content.strip()
        LOGGER.info(
            "llm answer response complete request_id=%s answer_length=%d duration_ms=%.2f",
            request_id, len(answer), (time.perf_counter() - started) * 1000,
        )
        return answer

    def _review_empty_result(self, question: str, sql: str, request_id: str) -> tuple[str, list[dict[str, Any]]]:
        """Revisa semanticamente uma consulta que retornou 0 linhas.

        >>> CORREÇÃO A: a revisão é OPCIONAL e NUNCA deve derrubar a requisição.
        Um "zero linhas" é uma resposta válida (doença/período sem registros), então
        qualquer falha aqui (timeout, JSON inválido, erro de execução) resulta em
        manter a SQL original com 0 linhas.
        """
        LOGGER.warning("empty query result request_id=%s; requesting semantic review", request_id)

        try:
            _cids = re.findall(r"LIKE\s+'([A-Za-z]\d{2}(?:\.?\d)?)%?'", sql, flags=re.IGNORECASE)
            if _cids:
                try:
                    _diag_cols: list[str] = []
                    for _grupo_cols in self.cache.get_grupo_columns().values():
                        for _col in _grupo_cols:
                            _nome = str(_col.get("column", ""))
                            if any(_tok in _nome.upper() for _tok in ("CID", "DIAG", "CAUSA")) and _nome not in _diag_cols:
                                _diag_cols.append(_nome)
                except Exception:
                    _diag_cols = []
                if _diag_cols:
                    _preds = " OR ".join(
                        'UPPER(TRIM(CAST("' + _c + '" AS VARCHAR))) LIKE \'' + _cod.upper() + '%\''
                        for _cod in _cids
                        for _c in _diag_cols
                    )
                    _check = "SELECT COUNT(*) AS n FROM datasus_query_cache WHERE " + _preds
                    try:
                        _n = self.cache.execute(_check, request_id)
                        if _n and int(_n[0].get("n", 0)) == 0:
                            LOGGER.info(
                                "zero legítimo: nenhum registro do CID %s no recorte; mantendo 0 linhas sem revisão. request_id=%s",
                                _cids, request_id,
                            )
                            return sql, []
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            schema = json.dumps(
                self.cache.schema_context("datasus_query_cache", include_examples=False),
                default=str,
                ensure_ascii=False,
            )
        except Exception:
            schema = "{}"

        prompt = (
            "Revise uma consulta DATASUS que retornou zero linhas. Responda somente JSON válido com as chaves action e sql. Use action=keep se zero for compatível com a pergunta; use action=retry se algum filtro ou coluna estiver inadequado. Em retry, forneça uma SQL corrigida usando somente SELECT/WITH e a view datasus_query_cache. Não invente colunas.\nPergunta: "
            f"{question}\nSQL: {sql}\nSchema: {schema}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=self.settings.llm_fix_max_tokens,
            timeout=min(self.settings.llm_timeout, self.settings.llm_fix_timeout),
            messages=[{"role": "user", "content": prompt}],
        )
        raw_review = (response.choices[0].message.content or "").strip().strip("`").strip()
        if raw_review.casefold().startswith("json"):
            raw_review = raw_review[4:].lstrip()
        try:
            review = json.loads(raw_review)
        except json.JSONDecodeError:
            LOGGER.warning("empty result review invalid_json request_id=%s", request_id)
            return sql, []
        action = review.get("action")
        corrected_sql = review.get("sql")
        LOGGER.info("empty result review request_id=%s action=%s", request_id, action)
        if action != "retry" or not isinstance(corrected_sql, str) or not corrected_sql.strip():
            return sql, []
        self._validate_sql(corrected_sql)
        if re.search(r"\bdatasus_query_cache\b", corrected_sql, flags=re.IGNORECASE):
            self.cache.prepare_question_cache(question, request_id)
        rows = self.cache.execute(corrected_sql, request_id)
        LOGGER.info("empty result retry complete request_id=%s rows=%d", request_id, len(rows))
        return corrected_sql, rows

