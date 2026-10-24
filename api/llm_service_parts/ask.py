""">>> Modulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'ask': mixin _AskMixin com o fluxo principal de pergunta (ask),
SQL heuristico local e formatacao de resposta local.
Copiado VERBATIM do original.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.logging_config import log_calls

from api.llm_service_parts._constants import FORA_ESCOPO_MESSAGE, OutOfScopeQuestion
from api.llm_service_parts.base import _ServiceBase

LOGGER = logging.getLogger("datasus_llm")


class _AskMixin(_ServiceBase):
    def _question_scope(self, question: str) -> dict[str, Any]:
        """Resume os periodos que a pergunta seleciona.

        Args:
            question: pergunta do usuario.

        Returns:
            dict[str, Any]: ``{"files": n, "names": [], "periods": {(ano, mes)}}``.
        """
        try:
            selecionados = self.cache.periods_for_question(question)
        except Exception as exc:
            LOGGER.warning("periods_for_question falhou: %s", exc)
            selecionados = []
        periodos = {(str(ano), str(mes)) for _, ano, mes in selecionados}
        return {"files": len(periodos), "names": [], "periods": periodos}

    @log_calls
    def ask(
        self,
        question: str,
        request_id: str,
        tipo_grafico: str | None = None,
    ) -> dict[str, Any]:
        """Responde a pergunta via LLM, com fallback heuristico local."""
        started = time.perf_counter()
        LOGGER.info(
            "question start request_id=%s question=%r llm_enabled=%s tipo_grafico=%s",
            request_id,
            question,
            self.llm_enabled,
            tipo_grafico,
        )

        limite_usuario = self._extract_limit_from_question(question)
        LOGGER.info("limite extraído da pergunta request_id=%s limite=%d", request_id, limite_usuario)
        if self.client:
            question_scope = self._question_scope(question)

            # >>> NOVO: Trata perguntas fora do escopo DATASUS de forma amigavel <<<
            # Se o LLM recusar (pergunta nao relacionada a dados do DATASUS/SUS),
            # retorna uma resposta padrao amigavel em vez de propagar erro (HTTP 500).
            try:
                sql = self._generate_sql(question, request_id, question_scope["periods"], limite_usuario)
            except OutOfScopeQuestion as fora_escopo:
                LOGGER.info(
                    "pergunta fora do escopo DATASUS request_id=%s motivo=%s question=%r",
                    request_id,
                    fora_escopo,
                    question,
                )
                return {
                    "answer": FORA_ESCOPO_MESSAGE,
                    "results": [],
                    "sql": None,
                    "source": "fora do escopo",
                    "cache": question_scope,
                    "chart": None,
                    "tipo_grafico": tipo_grafico,
                }
            if re.search(r"\bdatasus_query_cache\b", sql, flags=re.IGNORECASE):
                try:
                    # Passa a SQL ao preparo do cache para habilitar a PROJECAO
                    # MINIMA (so as colunas usadas pela consulta) - reduz o
                    # volume materializado pelo UNION ALL dos 16 grupos e o pico
                    # de memoria (evita OOM), sem alterar o resultado.
                    cache_scope = self.cache.prepare_question_cache(question, request_id, sql=sql)
                except (RuntimeError, ValueError) as cache_err:
                    LOGGER.error("prepare_question_cache falhou (%s): %s",
                                 type(cache_err).__name__, cache_err)
                    raise
            else:
                # >>> FASE 1 (S4): ATALHO DOS RESUMOS <<<
                # A SQL do LLM usa SOMENTE as tabelas agregadas resumo_* e NAO
                # referencia datasus_query_cache. Nesse caso PULAMOS a
                # materializacao do UNION ALL dos grupos (etapa mais cara de
                # prepare_question_cache) e respondemos direto dos resumos, que
                # ja estao agregados. Isso reduz perguntas de contagem
                # (por CID/sexo/ano) de minutos para milissegundos.
                cache_scope = question_scope
                LOGGER.info(
                    "question cache pulado (S4: SQL usa apenas resumo_*) request_id=%s sql=%s",
                    request_id, sql[:200],
                )
            try:
                rows = self.cache.execute(sql, request_id, limite=limite_usuario)
            except Exception as exec_err:
                # >>> MELHORIA: Se a execucao falhar no DuckDB, reenvia ao LLM para refazer a query <<<
                LOGGER.warning(
                    "execução SQL falhou no DuckDB: %s. Reenviando query ao LLM para refazer. request_id=%s",
                    exec_err,
                    request_id,
                )
                # Em modo grupos, schema_context("datasus") fica vazio: fazemos
                # fallback para a uniao das colunas de todos os grupos para que
                # o LLM receba o schema real ao refazer a query.
                contexto_schema = self.cache.schema_context("datasus", include_examples=False)
                real_cols_exec = [item["column"] for item in contexto_schema.get("columns", [])]
                if not real_cols_exec:
                    _seen_exec: set[str] = set()
                    for _cols in self.cache.get_grupo_columns().values():
                        for _col in _cols:
                            _key = str(_col.get("column", "")).upper()
                            if _key and _key not in _seen_exec:
                                _seen_exec.add(_key)
                                real_cols_exec.append(_col["column"])
                sql_refeita = self._request_sql_refazer(
                    question=question,
                    sql_atual=sql,
                    request_id=request_id,
                    periods=cache_scope.get("periods", set()),
                    limite=limite_usuario,
                    real_columns=real_cols_exec,
                    motivo=str(exec_err),
                )
                # Aplica as correcoes pos-processamento
                sql_refeita = self._fix_unterminated_strings(sql_refeita)
                sql_refeita = self._fix_incomplete_sql(sql_refeita, question, request_id, cache_scope.get("periods", set()), limite_usuario)
                try:
                    self._validate_sql(sql_refeita)
                except ValueError:
                    LOGGER.error("SQL refeita ainda é inválida. Usando fallback. request_id=%s", request_id)
                    raise
                # >>> CORRECAO A (colunas ausentes na temp table) <<<
                # A SQL refeita pode referenciar colunas DIFERENTES das que
                # foram materializadas na primeira chamada de
                # prepare_question_cache (ex.: trocar CIDASSOC por DIAG_PRINC).
                # Sem repreparar, a temp table nao teria a nova coluna e o
                # DuckDB falharia com:
                #   Binder Error: Referenced column "DIAG_PRINC" not found
                #   Candidate bindings: "ANO_CMPT", "CIDASSOC"
                # (ver cache/datasus.log request_id=a520e3247737).
                if re.search(r"\bdatasus_query_cache\b", sql_refeita, flags=re.IGNORECASE):
                    try:
                        self.cache.prepare_question_cache(
                            question, request_id, sql=sql_refeita
                        )
                    except Exception as prep_err:
                        LOGGER.warning(
                            "falha ao repreparar cache para a SQL refeita (%s); "
                            "mantendo cache atual. request_id=%s",
                            prep_err, request_id,
                        )
                # >>> CORRECAO G: cast de colunas CRUAS de ano na SQL refeita. <<<
                sql_refeita = self._fix_raw_year_casts(sql_refeita, request_id)
                # >>> CORRECAO I: cast de atend_ano (VARCHAR->INTEGER) em
                # comparacoes numericas (BETWEEN, =, IN com inteiros...). <<<
                sql_refeita = self._fix_atend_ano_casts(sql_refeita, request_id)
                # >>> CORRECAO DATA: remove funcoes de data do sistema na refeita. <<<
                sql_refeita = self._fix_system_date_functions(sql_refeita, request_id)
                rows = self.cache.execute(sql_refeita, request_id, limite=limite_usuario)
                sql = sql_refeita
            if not rows:
                # >>> CORRECAO B: um resultado vazio e resposta LEGITIMA. Falhas na
                # revisao semantica (ex.: APITimeoutError do Ollama) NAO devem
                # derrubar a requisicao com HTTP 500; seguimos com 0 linhas.
                try:
                    sql, rows = self._review_empty_result(question, sql, request_id)
                except Exception as review_err:
                    LOGGER.warning(
                        "revisão de resultado vazio falhou (%s); seguindo com 0 linhas. request_id=%s",
                        type(review_err).__name__,
                        request_id,
                    )
                    rows = []
            answer = self._summarize(question, sql, rows, request_id)
            # Segunda interacao: usar o modulo llm_grafic para gerar grafico dinamico via LLM
            chart = self.llm_grafic.gerar_grafico_llm(rows, question, tipo_grafico=tipo_grafico)
            if chart is None:
                LOGGER.info("llm_grafic retornou None; usando gráfico padrão")
                from graficos import gerar_grafico_barras
                chart = gerar_grafico_barras(rows, limite=limite_usuario, periodos=cache_scope.get("periods"))
            LOGGER.info("question complete request_id=%s source=llm rows=%d duration_ms=%.2f", request_id, len(rows), (time.perf_counter() - started) * 1000)
            return {"answer": answer, "results": rows, "sql": sql, "source": "LLM + DuckDB", "cache": cache_scope, "chart": chart, "tipo_grafico": tipo_grafico}
        cache_scope = self._question_scope(question)
        sql = self._heuristic_sql(question, limite_usuario)
        if re.search(r"\bdatasus_query_cache\b", sql, flags=re.IGNORECASE):
            cache_scope = self.cache.prepare_question_cache(question, request_id, sql=sql)
        rows = self.cache.execute(sql, request_id, limite=limite_usuario)
        answer = self._format_local_answer(question, rows)
        # Segunda interacao: usar o modulo llm_grafic para gerar grafico dinamico via LLM
        chart = self.llm_grafic.gerar_grafico_llm(rows, question, tipo_grafico=tipo_grafico) if self.llm_grafic else None
        if chart is None:
            from graficos import gerar_grafico_barras
            chart = gerar_grafico_barras(rows, limite=limite_usuario, periodos=cache_scope.get("periods"))
        LOGGER.info("question complete request_id=%s source=local rows=%d duration_ms=%.2f", request_id, len(rows), (time.perf_counter() - started) * 1000)
        return {"answer": answer, "results": rows, "sql": sql, "source": "consulta local DuckDB", "cache": cache_scope, "chart": chart, "tipo_grafico": tipo_grafico}


    def _heuristic_sql(self, question: str, limite: int = 100) -> str:
        """Monta uma SQL de contagem simples sem LLM (fallback local)."""
        schema = self.cache.schema_context("datasus_query_cache", include_examples=False)["columns"]
        columns = {item["column"].upper(): item["column"] for item in schema}
        normalized_question = question.casefold()
        if "ame" in normalized_question and "2025" in normalized_question and "sexo" in normalized_question:
            sex_column = columns.get("AP_SEXO")
            month_column = columns.get("AP_CMP")
            diagnosis_columns = [columns[name] for name in ("AP_CIDPRI", "AP_CIDSEC") if name in columns]
            if sex_column and month_column and diagnosis_columns:
                diagnosis_filter = " OR ".join(
                    f'UPPER(CAST("{column.replace(chr(34), chr(34) * 2)}" AS VARCHAR)) LIKE \'%G12.1%\''
                    for column in diagnosis_columns
                )
                return (
                    f'SELECT COALESCE(NULLIF(TRIM(CAST("{sex_column}" AS VARCHAR)), \'\'), \'IGNORADO\') AS sexo, '
                    f'COUNT(*) AS casos FROM datasus_query_cache '
                    f'WHERE CAST("{month_column}" AS VARCHAR) LIKE \'2025%\' AND ({diagnosis_filter}) '
                    f'GROUP BY 1 ORDER BY 1 LIMIT {limite}'
                )
        text_columns = [
            item["column"] for item in schema
            if any(token in item["type"].upper() for token in ("VARCHAR", "TEXT", "STRING"))
            and any(token in item["column"].upper() for token in ("CID", "DIAG", "DESC", "NOME", "TIP", "PROC"))
        ]
        terms = [
            term for term in re.findall(r"[\wÀ-ÿ]+", question.lower())
            if len(term) >= 4 and term not in {
                "para", "quais", "anos", "ano", "doença", "casos", "número", "numero",
                "dados", "quantos", "total", "qual", "atendimentos", "sobre", "registros",
                "quantas", "doencas", "existem",
            }
        ]
        if not text_columns or not terms:
            LOGGER.info("heuristic sql fallback text_columns=%d terms=%d", len(text_columns), len(terms))
            return f"SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT {limite}"
        term = terms[0].replace("'", "''")
        LOGGER.info("heuristic sql generated term=%s searchable_columns=%d", term, len(text_columns))
        predicates = " OR ".join(
            f'UPPER(CAST("{column}" AS VARCHAR)) LIKE \'%{term.upper()}%\''
            for column in text_columns
        )
        return f"SELECT COUNT(*) AS registros_encontrados FROM datasus_query_cache WHERE {predicates}"

    def _format_local_answer(self, question: str, rows: list[dict[str, Any]]) -> str:
        """Formata as linhas do fallback local em texto legivel."""
        if not rows:
            return "Nenhum resultado foi encontrado no cache local."
        return (
            f"Resultado para '{question}': "
            + "; ".join(", ".join(f"{key}={value}" for key, value in row.items()) for row in rows)
        )
