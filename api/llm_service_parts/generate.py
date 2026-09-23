""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'generate': mixin _GenerateMixin com a geração e validação do SQL
(geração via LLM, validação somente-leitura/escopo e verificação/substituição
de colunas inexistentes).
Copiado VERBATIM do original.
"""
from __future__ import annotations

import json
import logging
import re
import time

from core.logging_config import log_calls

from db.period_fields import ATEND_ANO, ATEND_MES
from api.llm_service_parts._constants import OutOfScopeQuestion
from api.llm_service_parts.base import _ServiceBase

LOGGER = logging.getLogger("datasus_llm")


class _GenerateMixin(_ServiceBase):
    @log_calls
    def _generate_sql(self, question: str, request_id: str, periods: set[tuple[str, str]], limite: int = 100) -> str:
        started = time.perf_counter()
        LOGGER.info("llm sql request start request_id=%s model=%s", request_id, self.model)

        # >>> CORREÇÃO DATA: ano atual do sistema, informado EXPLICITAMENTE no
        # prompt para o LLM usar como LITERAL em filtros de "este ano"/"últimos
        # N anos" (a base não tem coluna de data; o recorte é atend_ano).
        import datetime as _dt
        system_year = _dt.date.today().year

        # >>> MODIFICAÇÃO: Usa o dicionário consolidado de colunas (cache local DuckDB) <<<
        # O dicionário é construído UMA vez por conjunto de grupos (store em
        # memória indexado por assinatura) e reutilizado em cada requisição,
        # evitando recomputar união/dedup de colunas a cada pergunta.
        relevant_grupos = self._determine_relevant_grupos(question, periods)

        column_dict = self.cache.get_column_dictionary()

        if relevant_grupos:
            # Colunas apenas dos grupos relevantes, deduplicadas por nome (upper).
            seen: set[str] = set()
            unique_columns: list[dict[str, str]] = []
            tipo_por_upper = column_dict.type_by_upper
            for grupo in relevant_grupos:
                for nome in column_dict.columns_for_group(grupo):
                    key = nome.upper()
                    if key in seen:
                        continue
                    seen.add(key)

                    unique_columns.append({"column": nome, "type": tipo_por_upper.get(key, "")})

            # Mapa coluna -> grupos de origem (para rotular colunas homônimas).
            groups_map: dict[str, list[str]] = {
                col["column"]: column_dict.groups_for_column(col["column"])
                for col in unique_columns
            }
            # Colunas rotuladas por grupo (ex.: "SEXOPAC" [ps]) — evita que o LLM
            # misture colunas homônimas de grupos diferentes.
            #
            # >>> CORREÇÃO 1 (prompt): quando há MUITOS grupos (ex.: 14, caso do
            # log d868fb02c2ef com 462 colunas), a lista rotulada por grupo sozinha
            # inflava o prompt para ~29.6k chars, estourando o teto de prefill do
            # qwen3-coder:30b (APITimeoutError em 112s). Limita-se a lista aos
            # grupos mais prováveis e a um número de colunas por grupo.
            MAX_GRUPOS_LABELED = 6
            MAX_COLS_POR_GRUPO = 25
            if len(relevant_grupos) > MAX_GRUPOS_LABELED:
                grupos_labeled = relevant_grupos[:MAX_GRUPOS_LABELED]
                LOGGER.info(
                    "labeled_hint reduzido para %d de %d grupos (CORREÇÃO 1)",
                    len(grupos_labeled),
                    len(relevant_grupos),
                )
            else:
                grupos_labeled = relevant_grupos
            labeled_hint = column_dict.labeled_columns_sql_hint(
                grupos_labeled, limit_per_group=MAX_COLS_POR_GRUPO
            )
            schema = json.dumps({"columns": unique_columns}, ensure_ascii=False, default=str)
            LOGGER.info(
                "grupos relevantes: %s | colunas únicas: %d",
                relevant_grupos,
                len(unique_columns),
            )
        else:
            # Em modo grupos, a view "datasus" legada não existe; os dados vivem
            # nos bancos anexados (<grupo>.datasus_cache). Usamos a UNIÃO das
            # colunas de todos os grupos (já consolidada no dicionário) como
            # fallback para dar contexto real ao LLM.
            schema_ctx = self.cache.schema_context("datasus", include_examples=False)
            if schema_ctx.get("columns"):
                unique_columns = schema_ctx.get("columns", [])
            else:
                tipo_por_upper = column_dict.type_by_upper
                unique_columns = [
                    {"column": nome, "type": tipo_por_upper.get(nome.upper(), "")}
                    for nome in column_dict.unique_columns
                ]
                if unique_columns:
                    LOGGER.info(
                        "schema_context('datasus') vazio; usando uniao de %d colunas do dicionario como fallback.",
                        len(unique_columns),
                    )

            groups_map = {
                col["column"]: column_dict.groups_for_column(col["column"])
                for col in unique_columns
            }
            # Fallback (nenhum grupo identificado): limita o rótulo por grupo
            # para não inflar o prompt com centenas de entradas.
            labeled_hint = column_dict.labeled_columns_sql_hint(limit_per_group=40)
            schema = json.dumps({"columns": unique_columns}, ensure_ascii=False, default=str)
        
        summary_tables = self.cache._summary_tables()
        summary_schema_parts = []
        if "resumo_diagnosticos" in summary_tables:
            summary_schema_parts.append("resumo_diagnosticos(atend_ano INTEGER, atend_mes VARCHAR, sexo VARCHAR, cid VARCHAR, casos BIGINT)")
        if "resumo_cid_periodo" in summary_tables:
            summary_schema_parts.append("resumo_cid_periodo(atend_ano INTEGER, atend_mes VARCHAR, sexo VARCHAR, cid VARCHAR, casos BIGINT)")
        if "resumo_anual_cid" in summary_tables:
            summary_schema_parts.append("resumo_anual_cid(atend_ano INTEGER, cid VARCHAR, casos BIGINT)")
        
        if summary_schema_parts:
            summary_schema = "; ".join(summary_schema_parts)
            # Constrói a lista de anos e meses do período selecionado
            period_years_list = sorted({int(year) for year, _ in periods}) if periods else []
            period_months_list = sorted({month for _, month in periods}) if periods else []
            period_filter_hint = ""
            if period_years_list:
                years_str = ", ".join(str(y) for y in period_years_list)
                period_filter_hint += f"Ao usar as tabelas resumo_*, SEMPRE adicione o filtro 'WHERE atend_ano IN ({years_str})' (ou a condição equivalente) para restringir apenas aos anos selecionados. "
            if period_months_list:
                months_str = ", ".join(f"'{m}'" for m in period_months_list)
                period_filter_hint += f"Se a pergunta pedir meses específicos, adicione também 'AND atend_mes IN ({months_str})'. "
            # >>> CORREÇÃO (schema por relação): deixa EXPLÍCITO que as tabelas
            # agregadas resumo_* NÃO possuem as colunas CRUAS do DATASUS (ex.:
            # "AP_CIDCAS", "SP_CIDPRI"). Elas só expõem as colunas NORMALIZADAS
            # (atend_ano, atend_mes, sexo, cid, casos). Sem este aviso, o LLM
            # escolhia resumo_anual_cid (para contagem por CID/ano) mas continuava
            # filtrando pela coluna crua "AP_CIDCAS", gerando:
            #   Binder Error: Referenced column "AP_CIDCAS" not found in FROM clause!
            #   Candidate bindings: "parquet_year"
            # Aqui dizemos QUAL coluna usar em cada tabela para NÃO precisar de
            # reescrita local (a própria LLM re-gera a query correta na resposta).
            resumos_colunas_hint = (
                "ATENÇÃO (NOMES DE COLUNA POR RELAÇÃO): as tabelas resumo_* usam APENAS "
                "as colunas NORMALIZADAS listadas no schema abaixo (atend_ano, atend_mes, "
                "sexo, cid, casos). Elas NÃO possuem as colunas CRUAS do DATASUS "
                "(ex.: AP_CIDCAS, SP_CIDPRI, PA_CIDPRI, DIAG_PRINC, AP_SEXO, ANO_CMPT). "
                "Portanto: o DIAGNÓSTICO/CID, nas tabelas resumo_*, deve ser filtrado "
                "SEMPRE pela coluna 'cid' (ex.: WHERE cid IN ('A90','A900') ou "
                "cid LIKE 'A90%'), NUNCA por um nome cru; o SEXO, pela coluna 'sexo'; "
                "e o PERÍODO, por 'atend_ano'/'atend_mes'. "
                "Se você precisar de alguma coluna CRUA do DATASUS que não exista no "
                "resumo_*, use a view datasus_query_cache (que tem todas as colunas cruas). "
            )
            summary_instruction = (
                f"Tabelas agregadas disponíveis: {summary_tables}. "
                f"Schema dos resumos: {summary_schema}. "
                f"{resumos_colunas_hint}"
                f"ATENÇÃO: {period_filter_hint}"
                "PREFIRA SEMPRE as tabelas resumo_* para CONTAGENS/agregações por CID, período e sexo "
                "(ex.: 'quantos casos de X por sexo/ano'): consultá-las é MUITO mais rápido do que "
                "varrer datasus_query_cache. Use datasus_query_cache SOMENTE quando a pergunta exigir "
                "colunas detalhadas que os resumos não têm (município, idade, procedimento, listagens). "
                "para totais anuais use resumo_anual_cid. Essas tabelas já estão agregadas e devem ser preferidas para respostas estatísticas. "
                "IMPORTANTE: Ao usar qualquer tabela resumo_*, NUNCA se esqueça de filtrar por atend_ano (e atend_mes quando aplicável) conforme o período solicitado na pergunta. "
            )
        else:
            summary_instruction = (
                "NÃO existem tabelas agregadas disponíveis no momento. Use SOMENTE a view datasus_query_cache "
                "com as colunas detalhadas para responder a pergunta. Não referencie tabelas como resumo_anual_cid, "
                "resumo_diagnosticos ou resumo_cid_periodo, pois elas NÃO existem no banco. "
            )

        real_columns = [item["column"] for item in unique_columns]
        # Adiciona as colunas derivadas de periodo (atend_ano/atend_mes), que
        # existem na view datasus_query_cache mas nao vem do dicionario de
        # colunas. Sem isso, a validacao as renomeava para ANO e quebrava o filtro.
        for _derived in (ATEND_ANO, ATEND_MES):
            if _derived not in real_columns and _derived.upper() not in {c.upper() for c in real_columns}:
                real_columns.append(_derived)

        # >>> MELHORIA 1: mapa de conceitos -> colunas reais vem do dicionário
        # consolidado (construído uma única vez, indexado por assinatura).
        # Filtra o mapa do dicionário para as colunas efetivamente em escopo.
        real_upper = {col.upper() for col in real_columns}
        concept_map: dict[str, list[str]] = {}
        for conceito, cols in column_dict.concept_map.items():
            filtradas = [c for c in cols if c.upper() in real_upper]
            if filtradas:
                concept_map[conceito] = filtradas

        # Diagnóstico: considere apenas colunas que realmente são de CID/diagnóstico
        # (exclui "PROC*", que são procedimentos e não diagnósticos).
        diagnosis_columns = [
            col for col in column_dict.diagnosis_columns if col.upper() in real_upper
        ]
        if not diagnosis_columns:
            # Fallback para escopos cujas colunas de diagnóstico não estejam
            # marcadas no dicionário: aplica o filtro local por tokens.
            diagnosis_columns = [
                col for col in real_columns
                if "CID" in col.upper() or "DIAG" in col.upper() or "CAUSA" in col.upper()
            ]
        # >>> OTIMIZAÇÃO DE PROMPT: o bloco de diagnóstico agora é CURTO e apenas
        # sinaliza QUAIS colunas são de CID (sem repetir a sintaxe do filtro, que já
        # está em 'REGRAS DE COLUNAS'). A lista completa dessas colunas já aparece no
        # bloco rotulado por grupo (labeled_hint) e no mapa de conceitos abaixo.
        if diagnosis_columns:
            diagnosis_columns_str = ", ".join(f'"{col}"' for col in diagnosis_columns)
            diagnosis_hint = (
                f"COLUNAS DE DIAGNÓSTICO/CID (use APENAS estas para doença/CID): {diagnosis_columns_str}."
            )
        else:
            diagnosis_hint = (
                "NÃO há colunas de diagnóstico/CID neste escopo. Se a pergunta pedir uma doença "
                "específica, gere SQL que retorne 0 linhas de forma limpa (ex.: WHERE 1=0)."
            )

        # >>> CORREÇÃO H: evita repetir a lista COMPLETA de colunas (reduz o prompt em
        # ~3-5k chars, acelerando o prefill do qwen3-coder:30b e reduzindo timeout).
        # Quando há rótulos por grupo (labeled_hint), eles JÁ listam os nomes das
        # colunas; então omitimos a duplicação. A lista completa só entra no fallback.
        # Deduplica preservando a ordem (nomes homônimos entre grupos aparecem 1x aqui).
        seen_cols: set[str] = set()
        dedup_columns: list[str] = []
        for col in real_columns:
            up = col.upper()
            if up in seen_cols:
                continue
            seen_cols.add(up)
            dedup_columns.append(col)

        # >>> OTIMIZAÇÃO DE PROMPT: conceito_guidance e diagnosis_hint foram
        # INCORPORADOS ao schema_hint. Antes, as mesmas colunas eram listadas 3x
        # (labeled_hint + concept_guidance + diagnosis_hint), inflando o prompt em
        # ~4-6k chars sem acrescentar informação. Agora há UMA seção de esquema com:
        # (1) lista rotulada por grupo, (2) mapa de conceito -> coluna, (3) colunas
        # de CID. Isso reduz o prefill do qwen3-coder:30b (ver CORREÇÃO D/I/2).
        concept_guidance = self._build_concept_guidance(concept_map, groups_map)

        if labeled_hint:
            schema_hint = (
                "COLUNAS REAIS DA VIEW datasus_query_cache (lista COMPLETA rotulada por grupo; "
                "não invente outras). ATENÇÃO: alguns nomes podem conter ESPAÇOS "
                "(ex.: \"TIPPRE  \"); use SEMPRE aspas duplas com o nome EXATO.\n"
                "COLUNAS POR GRUPO DE ORIGEM (use uma coluna APENAS do grupo que faz sentido "
                "para a pergunta; NÃO misture colunas homônimas de grupos diferentes): "
                f"{labeled_hint}."
            )
        else:
            all_columns_str = ", ".join(f'"{col}"' for col in dedup_columns)
            schema_hint = (
                f"COLUNAS REAIS DA VIEW datasus_query_cache (lista COMPLETA; não invente outras): {all_columns_str}.\n"
                "ATENÇÃO: alguns nomes de coluna podem conter ESPAÇOS (ex.: \"TIPPRE  \"). "
                "Ao referenciar qualquer coluna, use SEMPRE aspas duplas com o nome EXATO."
            )

        if concept_guidance:
            schema_hint = schema_hint + "\n" + concept_guidance
        if diagnosis_hint:
            schema_hint = schema_hint + "\n" + diagnosis_hint
        
        # >>> MELHORIA 2: fontes e exemplos estruturados <<<
        # (concept_guidance e diagnosis_hint já foram incorporados ao schema_hint acima)
        source_guidance = self._build_source_guidance(relevant_grupos, summary_tables)

        # >>> CORREÇÃO (schema por relação): passa as tabelas resumo_* para que os
        # few-shot incluam exemplos EXPLÍCITOS de como consultar os resumos com as
        # colunas normalizadas ('cid', 'sexo', 'atend_ano', 'casos') — evitando que
        # a LLM reutilize nomes crus ("AP_CIDCAS") nessas tabelas.
        fewshot = self._build_fewshot_examples(concept_map, limite, summary_tables)

        # >>> CORREÇÃO CID: grounding dos códigos CID reais presentes nos dados. <<<
        # Sem isso, o LLM inventa códigos de doença a partir do conhecimento
        # paramétrico (ex.: 'C44' para câncer de pele), que frequentemente não
        # existem/não correspondem ao que há no DATASUS. Passamos a lista de
        # PREFIXOS REAIS coletados do cache e exigimos que o filtro use um deles.
        # 1) DE-PARA SEMÂNTICO (doença -> CID) do arquivo OFICIAL CID_10.CSV.
        #    É a fonte PRIMÁRIA: diz explicitamente qual o código correto da
        #    doença citada. AGORA a própria LLM DESCOBRE o CID consultando a
        #    tabela `cid10` no DuckDB (opção A: 1ª rodada), em vez de o Python
        #    injetar o de-para pronto. O bloco abaixo traz o resultado dessa
        #    consulta auto-iniciada pela LLM (pode ser vazio).
        # >>> PRECEDÊNCIA (FASE 4.1): CID explícito na pergunta tem prioridade
        # ABSOLUTA. Nesse caso PULAMOS tanto a descoberta via LLM quanto o
        # de-para por doença: o bloco autoritativo (com os códigos informados)
        # vem de _resolve_cid_grounding, que detecta o CID explícito.
        cid_expl = self._extract_explicit_cid(question)
        if cid_expl:
            LOGGER.info(
                "CID explícito na pergunta=%s; pulando descoberta/de-para de doença. request_id=%s",
                cid_expl, request_id,
            )
            cid_discovery = self._resolve_cid_grounding(question, request_id)
        else:
            cid_discovery = self._discover_cid_via_llm(question, request_id)

            # >>> FALLBACK DETERMINÍSTICO (reativa _resolve_cid_grounding): se a
            # descoberta via LLM falhou/não achou nada (ex.: timeout, tabela cid10
            # indisponível, doença fora dos aliases), usamos o de-para OFICIAL
            # (CID_10.CSV/CidMap) como fonte. Assim mantemos o grounding SEMÂNTICO
            # sem depender do round-trip ao LLM — e sem duplicar blocos quando a
            # descoberta já trouxe resultados.
            if not cid_discovery:
                cid_discovery = self._resolve_cid_grounding(question, request_id)

        # 2) Lista dos prefixos REAIS presentes nos dados (validação de escopo).
        known_cids = self._known_cid_prefixes(request_id)
        if known_cids:
            cid_grounding = (
                "PREFIXOS DE CID REAIS PRESENTES NOS DADOS (referência de escopo; "
                "NÃO invente códigos): "
                + ", ".join(known_cids[:60])
                + ".\nSe a doença citada na pergunta NÃO tiver um código correspondente, "
                "NÃO gere um CID inventado: retorne uma consulta que resulte em 0 "
                "linhas de forma limpa (ex.: adicione 'AND 1=0' ao WHERE)."
            )
        else:
            cid_grounding = (
                "ATENÇÃO: não foi possível listar os CIDs presentes nos dados. Se você NÃO "
                "tiver CERTEZA do código CID correto para a doença citada, NÃO invente: "
                "gere uma consulta que retorne 0 linhas de forma limpa (ex.: 'WHERE 1=0')."
            )




        # >>> OTIMIZAÇÃO DE PROMPT (unificação dos blocos de CID): antes havia 4
        # blocos repetindo "não invente CID" (schema do cid10, resultado da
        # descoberta, prefixos reais e a regra fixa). Agora são MUTUAMENTE
        # EXCLUSIVOS quando possível: se a descoberta retornou CIDs, ela é a
        # fonte primária e o schema do cid10 (que só serve para orientar a
        # consulta) NÃO é repetido. O bloco de prefixos reais permanece como
        # referência de escopo (curto, valores reais).
        if cid_discovery:
            cid_grounding = cid_discovery + "\n\n" + cid_grounding
        else:
            # Só injeta o schema do cid10 quando a descoberta não trouxe nada
            # (útil para a LLM saber que pode consultá-lo, se necessário).
            cid_grounding = self.CID10_SCHEMA_HINT + "\n\n" + cid_grounding

        # >>> MELHORIA 3: dica de período explícita (evita filtros errados) <<<
        period_years_all = sorted({int(y) for y, _ in periods}) if periods else []
        period_months_all = sorted({m for _, m in periods}) if periods else []
        if period_years_all:
            anos_txt = ", ".join(str(a) for a in period_years_all)
            period_hint = (
                f"PERÍODO JÁ SELECIONADO (a view datasus_query_cache JÁ está filtrada): "
                f"anos DISPONÍVEIS = [{anos_txt}]"
                + (f", meses={period_months_all}." if period_months_all else ".")
                + " NUNCA adicione um filtro 'atend_ano IN (...)' próprio: a view já "
                "aplica o recorte. Em especial, expressões como 'últimos N anos' NÃO "
                "devem virar atend_ano IN (...): o escopo já foi resolvido "
                f"(anos reais = [{anos_txt}]) e qualquer ano fora disso NÃO EXISTE nos "
                "dados — incluí-lo faria a consulta retornar MENOS dados que o correto. "
                "Se (e somente se) a pergunta citar um ANO EXPLÍCITO de 4 dígitos contido "
                f"em [{anos_txt}], use exatamente esse ano; caso contrário, NÃO filtre por ano."
            )
        else:
            period_hint = (
                "PERÍODO: use atend_ano (INTEGER) e atend_mes (VARCHAR '01'..'12') "
                "para recortes de tempo quando a pergunta citar ano ou mês."
            )

        # >>> CORREÇÃO 2: dica de AGRUPAMENTO MENSAL. Quando a pergunta pede
        # "agrupe por mês"/"por mês"/"mensal", instruímos o LLM a agrupar pela
        # coluna JÁ EXISTENTE atend_mes, sem inventar colunas de mês e sem
        # tratar o pedido como filtro de um mês específico. <<<
        if self.cache.question_wants_monthly_grouping(question):
            monthly_group_hint = (
                "AGRUPAMENTO POR MÊS (OBRIGATÓRIO): a pergunta pede resultados por mês. "
                "Use a coluna JÁ EXISTENTE atend_mes (VARCHAR '01'..'12') no SELECT e no "
                "GROUP BY, junto de atend_ano. NÃO invente colunas de mês (ex.: mes, month) "
                "e NÃO restrinja a um único mês quando a pergunta apenas pedir o agrupamento. "
                "Exemplo: SELECT atend_ano AS ano, atend_mes AS mes, COUNT(*) AS casos "
                "FROM datasus_query_cache WHERE <filtro> "
                "GROUP BY atend_ano, atend_mes ORDER BY atend_ano, atend_mes."
            )
        else:
            monthly_group_hint = ""

        # >>> PROMPT REESTRUTURADO (otimizado para qwen3-coder:30b) <<<
        prompt = (
            "Você é um especialista em SQL DuckDB para dados de saúde pública do DATASUS/SUS.\n"
            "Sua ÚNICA tarefa: responder à pergunta do usuário com UMA query SQL DuckDB válida.\n\n"
            "### REGRAS DE SAÍDA (OBRIGATÓRIAS)\n"
            "- Retorne SOMENTE a consulta SQL. SEM markdown, SEM crases, SEM comentários, SEM explicações.\n"
            "- Comece com SELECT ou WITH. Termine a query com LIMIT.\n"
            # >>> PARTE 7 (#10): LIMIT sem ORDER BY e NAO-DETERMINISTICO <<<
            # Como o DuckDB roda com preserve_insertion_order=false, escolher
            # 'as N primeiras linhas' sem ORDER BY varia entre execucoes. Para
            # respostas reproduziveis, TODO LIMIT precisa de ORDER BY explicito.
            "- Se a query usar LIMIT, ela OBRIGATORIAMENTE deve ter um ORDER BY "
            "explicito antes do LIMIT (ex.: ORDER BY casos DESC, cid ASC). Sem "
            "ORDER BY, o resultado do LIMIT e NAO-deterministico e muda entre "
            "execucoes. Ao ordenar por uma agregacao, ordene pela coluna de saida "
            "(ou pelo indice/alias), ex.: 'ORDER BY COUNT(*) DESC' ou 'ORDER BY 2 DESC'.\n"
            "- Use UMA única instrução. Nunca use INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/COPY/ATTACH.\n"
            "- A query deve estar COMPLETA (parênteses e aspas fechados).\n\n"
            "### REGRAS DE COLUNAS (CRÍTICAS)\n"
            "- Use APENAS as colunas listadas em 'COLUNAS REAIS' abaixo. NUNCA invente nomes.\n"
            "- Referencie colunas com aspas duplas usando o nome EXATO (ex.: \"SP_CIDPRI\", \"SEXOPAC\").\n"
            "- Converta tipos quando comparar/categorizar: CAST(\"Coluna\" AS VARCHAR) ou TRY_CAST(... AS DOUBLE).\n"
            "- Para agrupar/contar, use COUNT(*); para tratar nulos, IS NOT NULL / NULLIF / COALESCE.\n"
            "- FILTRO DE CID: use SEMPRE UPPER(TRIM(CAST(\"ColunaCID\" AS VARCHAR))) LIKE 'CODIGO%' "
            "com o código em MAIÚSCULAS (ex.: 'C44%'). NUNCA use '=' com o código cru, "
            "pois os códigos DATASUS vêm com zeros/espaços à direita e caixa variável.\n"
            # >>> CORREÇÃO (parquet_year/parquet_month): proíbe metadados de arquivo. <<<
            "- PROIBIDO usar as colunas parquet_year, parquet_month, filename ou "
            "parquet_path em QUALQUER parte da consulta (SELECT, WHERE, GROUP BY...): "
            "são metadados do ARQUIVO (competência de remessa), NÃO dados de "
            "atendimento, e NÃO existem na view datasus_query_cache. Para recorte "
            "temporal use SOMENTE atend_ano (INTEGER) e atend_mes (VARCHAR '01'..'12').\n"
            # >>> CORREÇÃO DATA: proíbe funções de data e informa o ano do sistema. <<<
            "- NUNCA use funções de data (CURRENT_DATE, CURRENT_TIMESTAMP, NOW, "
            "LOCALTIME, LOCALTIMESTAMP) nem EXTRACT(YEAR FROM ...): NÃO existe "
            "coluna de data na base. Para 'este ano'/'últimos N anos', use a coluna "
            "atend_ano (INTEGER) com o ANO ATUAL DO SISTEMA como LITERAL numérico.\n"
            f"- ANO ATUAL DO SISTEMA = {system_year}. Use este literal quando a "
            "pergunta citar 'este ano'/'ano atual' (ex.: atend_ano = "
            f"{system_year}) e NUNCA funções de data.\n"
            f"- {cid_grounding}\n\n"
            "### VALIDAÇÃO DE ESCOPO\n"
            "- Se a pergunta NÃO for sobre dados de saúde pública do DATASUS/SUS "
            "(futebol, economia, clima etc.), NÃO gere SQL. "
            "Responda apenas com uma linha começando por 'FORA_ESCOPO:' e uma frase curta.\n\n"
            "### REGRA DE PERÍODO\n"
            f"- {period_hint}\n"
            + (f"- {monthly_group_hint}\n" if monthly_group_hint else "")
            + "\n"
            "### ESQUEMA E FONTES\n"
            f"{source_guidance}\n"
            f"{summary_instruction}\n"
            f"{schema_hint}\n\n"
            f"{fewshot}\n\n"
            "### TAREFA\n"
            f"Pergunta: {question}\n"
            f"Limite de linhas: LIMIT {limite}\n"
            "SQL:"
        )
        LOGGER.info("llm sql prompt request_id=%s chars=%d periods=%d grupos=%s", request_id, len(prompt), len(periods), relevant_grupos)



        # >>> CORREÇÃO D/I/2: teto de tempo POR CHAMADA para a geração de SQL.
        # Evita que uma única chamada prenda a requisição por todo o LLM_TIMEOUT.
        # O prefill do qwen3-coder:30b é proporcional ao prompt: com ~15k chars
        # levava ~26s, mas com ~30k chars (caso do log d868fb02c2ef) ultrapassou
        # o antigo teto de 110s (APITimeoutError em 112054ms). Usa-se agora um
        # teto ESPECÍFICO e configurável (LLM_SQL_TIMEOUT), além da redução de
        # prompt da CORREÇÃO 1. Os round-trips usam teto menor (LLM_FIX_TIMEOUT).
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.settings.llm_sql_temperature,
            max_tokens=self.settings.llm_sql_max_tokens,  # Configurado via LLM_SQL_MAX_TOKENS

            timeout=min(self.settings.llm_timeout, self.settings.llm_sql_timeout),
            messages=[{"role": "user", "content": prompt}],
        )
        llm_response = response.choices[0].message.content or ""
        
        # >>> LOG: Resposta CRUA do LLM SEMPRE logada para debug (Opção E) <<<
        LOGGER.info(
            "llm_raw_response request_id=%s llm_enabled=True response_raw_len=%d response_raw_preview=%s",
            request_id,
            len(llm_response),
            repr(llm_response[:3000]),
        )
        
        # >>> NOVO: Detecta recusa do LLM (pergunta fora do escopo DATASUS) <<<
        # Quando a pergunta não é sobre dados de saúde do DATASUS/SUS, o LLM responde
        # com texto amigável em vez de SQL. Nesse caso NÃO tratamos como erro fatal:
        # levantamos OutOfScopeQuestion para que o fluxo retorne uma resposta padrão.
        if self._is_out_of_scope_response(llm_response):
            LOGGER.info(
                "LLM recusou a pergunta por estar fora do escopo DATASUS. request_id=%s resposta=%r",
                request_id,
                llm_response[:300],
            )
            raise OutOfScopeQuestion(llm_response.strip()[:500])
        
        sql = self._extract_sql(llm_response)
        # Normaliza relações: modelos de código podem gerar placeholders
        # (ex.: "sua_tabela") em vez da view autorizada. Reescreve para
        # datasus_query_cache antes da validação.
        sql = self._normalize_relations(sql)
        # >>> TELEMETRIA: mede se a PRIMEIRA tentativa do LLM já passou na validação <<<
        # Objetivo: acompanhar o ganho de acerto na primeira tentativa após a
        # reescrita estruturada do prompt (meta: reduzir refazer/correções).
        self._first_attempt_ok = True
        try:
            self._validate_sql(sql)
        except ValueError as first_validate_err:
            self._first_attempt_ok = False
            LOGGER.warning(
                "validacao inicial falhou (%s); reenviando ao LLM para refazer. request_id=%s sql_preview=%r",
                first_validate_err,
                request_id,
                sql[:300],
            )
            sql = self._request_sql_refazer(
                question=question,
                sql_atual=sql,
                request_id=request_id,
                periods=periods,
                limite=limite,
                real_columns=real_columns,
                motivo=str(first_validate_err),
            )
            sql = self._normalize_relations(sql)
            self._validate_sql(sql)
        # >>> MODIFICADO: _validate_and_fix_columns agora retorna tupla (sql, unresolved_columns) <<<
        sql, unresolved_columns = self._validate_and_fix_columns(sql, real_columns)
        # >>> NOVO: Se houve colunas não resolvidas, envia a SQL ao LLM para correção <<<
        if unresolved_columns:
            # >>> SOLUÇÃO D: correção pós-geração invalida a 'primeira tentativa'. <<<
            self._first_attempt_ok = False
            LOGGER.warning(
                "colunas não resolvidas na validação automática: %s. Enviando SQL ao LLM para correção. request_id=%s",
                unresolved_columns,
                request_id,
            )
            sql = self._request_sql_fix(
                question=question,
                sql_atual=sql,
                request_id=request_id,
                periods=periods,
                limite=limite,
                real_columns=real_columns,
                problemas=unresolved_columns,
            )
        # >>> CORREÇÃO: corrige strings não terminadas (ex: LIKE ' sem fechamento) <<<
        sql = self._fix_unterminated_strings(sql)
        # >>> NOVO: Verifica e corrige SQL truncado/incompleto <<<
        sql = self._fix_incomplete_sql(sql, question, request_id, periods, limite)
        
        # >>> CORREÇÃO CID: valida os códigos de CID usados no SQL contra os
        # prefixos REAIS presentes nos dados. Códigos inventados pelo LLM
        # (ex.: 'A90' de dengue, 'C44' de câncer de pele) que não existem no
        # cache são enviados de volta ao LLM para correção.
        cids_invalidos = self._validate_cid_filters(sql, request_id, question=question)
        if cids_invalidos and self._is_explicit_cid_question(question):
            # >>> PRECEDÊNCIA (FASE 4.2, Decisões A1/D): o usuário informou o CID
            # explicitamente. Preservamos SEMPRE o CID do usuário: não reescrevemos,
            # não trocamos pelo de-para e NUNCA forçamos 'WHERE 1=0'. Apenas
            # registramos a divergência no log (ex.: LLM adicionou/omitiu código).
            self._first_attempt_ok = False
            explicitos = self._extract_explicit_cid(question)
            LOGGER.warning(
                "CID(s) divergentes do informado pelo usuário %s: %s. "
                "PRESERVANDO o CID do usuário (não reescreve). request_id=%s",
                explicitos,
                cids_invalidos,
                request_id,
            )
            # Best-effort ÚNICO: pede ao LLM para alinhar a query aos CIDs
            # informados (A1). NÃO usa de-para, NÃO reescreve determinísticamente
            # e, em caso de falha, PRESERVA a SQL com o CID do usuário (Decisão D).
            try:
                sql_ajustada = self._request_sql_fix(
                    question=question,
                    sql_atual=sql,
                    request_id=request_id,
                    periods=periods,
                    limite=limite,
                    real_columns=real_columns,
                    problemas=[
                        f"O usuário informou explicitamente o(s) CID(s) {explicitos}; "
                        "o SQL deve usar EXATAMENTE e SOMENTE esses códigos."
                    ],
                    cids_corretos=explicitos,
                )
                # Só aceita o ajuste se ele respeitar a regra A1; caso contrário,
                # mantém a SQL anterior (preserva o CID do usuário).
                candidata = self._normalize_relations(sql_ajustada)
                candidata = self._fix_unterminated_strings(candidata)
                candidata = self._fix_incomplete_sql(
                    candidata, question, request_id, periods, limite
                )
                if not self._validate_cid_filters(
                    candidata, request_id, question=question
                ):
                    sql = candidata
                else:
                    LOGGER.warning(
                        "ajuste do LLM ainda diverge do CID do usuário; "
                        "mantendo SQL original (CID preservado). request_id=%s",
                        request_id,
                    )
            except Exception as fix_err:
                LOGGER.warning(
                    "tentativa de alinhar CID ao informado falhou (%s); "
                    "preservando SQL com o CID do usuário. request_id=%s",
                    type(fix_err).__name__,
                    request_id,
                )
        elif cids_invalidos:
            # >>> SOLUÇÃO D: uma correção de CID invalida a 'primeira tentativa'. <<<
            self._first_attempt_ok = False
            LOGGER.warning(
                "CID(s) fora dos dados na SQL: %s. Corrigindo CID. request_id=%s",
                cids_invalidos,
                request_id,
            )
            # >>> SOLUÇÃO B: 1ª opção DETERMINÍSTICA, usando a família do de-para.
            # Evita o round-trip inútil ao LLM (que devolvia a SQL inalterada).
            sql_deterministico = self._fix_cid_filters_deterministic(
                sql, cids_invalidos, question, request_id
            )
            if sql_deterministico is not None:
                sql = sql_deterministico
            else:
                # Fallback: só quando NÃO há de-para aplicável. Agora com o
                # prompt reformulado p/ falar de CÓDIGO CID (não de coluna) e
                # informando o(s) CID(s) correto(s) quando conhecidos.
                cids_corretos = sorted(self._cid_map_prefixes_para(question)) or None
                sql = self._request_sql_fix(
                    question=question,
                    sql_atual=sql,
                    request_id=request_id,
                    periods=periods,
                    limite=limite,
                    real_columns=real_columns,
                    problemas=[f"CID incorreto para a doença: {c}" for c in cids_invalidos],
                    cids_corretos=cids_corretos,
                )
            sql = self._normalize_relations(sql)
            sql = self._fix_unterminated_strings(sql)
            sql = self._fix_incomplete_sql(sql, question, request_id, periods, limite)
            # >>> SOLUÇÃO B (robustez): revalida o CID APÓS a correção. Se ainda
            # houver CID inválido (ex.: o LLM não cooperou no fallback), tenta
            # uma última reescrita determinística; se o de-para não existir,
            # neutraliza o filtro com 'AND 1=0' para NUNCA entregar dados de
            # outra doença (ex.: HIV/B20 para uma pergunta de sarampo).
            restantes = self._validate_cid_filters(sql, request_id, question=question)
            if restantes:
                corrigida = self._fix_cid_filters_deterministic(
                    sql, restantes, question, request_id
                )
                if corrigida is not None:
                    sql = corrigida
                elif self._cid_map_prefixes_para(question):
                    # Há de-para mas a reescrita por token falhou (formato
                    # inesperado do filtro). Preferimos 0 linhas a dados errados.
                    LOGGER.warning(
                        "CID ainda inválido (%s) e reescrita falhou; forçando 0 linhas. request_id=%s",
                        restantes,
                        request_id,
                    )
                    sql = re.sub(
                        r"\bWHERE\b",
                        "WHERE 1=0 AND",
                        sql,
                        count=1,
                        flags=re.IGNORECASE,
                    ) if re.search(r"\bWHERE\b", sql, flags=re.IGNORECASE) else sql

        # >>> BUG (lista de CIDs em IN retornava 0 linhas): o LLM as vezes
        # gera ``... IN ('M05%', 'M06%', 'M10%')``, que faz IGUALDADE EXATA
        # contra strings com o coringa dentro -- nenhum codigo real casa, logo
        # 0 linhas. Reescreve para disjuncao de LIKE ANTES da expansao para
        # todas as colunas (o LIKE resultante e entao replicado abaixo).
        sql = self._normalize_cid_in_filters(sql, request_id)

        # >>> CORRECAO H (multi-coluna CID): expande QUALQUER filtro CID que
        # use UMA coluna de diagnostico para TODAS as colunas de diagnostico
        # do escopo (SP_CIDPRI, PA_CIDPRI, DIAG_PRINC, CIDPRI, ...).
        sql = self._expand_cid_filters_multi_column(sql, real_columns, request_id)

        # >>> SOLUÇÃO F: corrige filtros de ANO inventados pelo LLM ("últimos 3
        # anos" -> atend_ano IN (2022,2023,2024), quando o escopo real é
        # 2024..2026). Sem isto, a interseção descarta silenciosamente 2025/2026
        # e a pergunta de 3 anos retorna dados de 1 ano só. Determinístico.
        sql = self._strip_foreign_year_filters(sql, periods, request_id)
        # >>> CORREÇÃO G: cast de colunas CRUAS de ano (VARCHAR->INTEGER). <<<
        sql = self._fix_raw_year_casts(sql, request_id)
        # >>> CORREÇÃO I: cast de atend_ano (VARCHAR->INTEGER) em comparações
        # numéricas (BETWEEN, =, IN com inteiros...). A view expõe atend_ano
        # como VARCHAR; comparar com literal inteiro gera
        # "Cannot mix values of type VARCHAR and INTEGER_LITERAL in BETWEEN". <<<
        # FASE 3B: atend_ano agora é INTEGER em todos os caminhos (tabela/view
        # S1/fallback legado). Este cast é, em operação normal, um no-op
        # semântico (INTEGER->INTEGER); mantido por robustez contra caches
        # intermediários ainda em VARCHAR. <<<
        sql = self._fix_atend_ano_casts(sql, request_id)
        # >>> CORREÇÃO DATA: remove funções de data do sistema (CURRENT_DATE,
        # NOW, EXTRACT(YEAR FROM CURRENT_DATE)...) trocando-as pelo ano atual
        # do sistema como LITERAL, pois a base não tem coluna de data. <<<
        sql = self._fix_system_date_functions(sql, request_id)

        try:
            self._validate_sql(sql)  # Revalida após correção
        except ValueError as validate_err:
            # >>> MELHORIA: Se a validação falhar, reenvia ao LLM para refazer a query <<<
            # >>> SOLUÇÃO D: refazer via LLM também invalida a 'primeira tentativa'. <<<
            self._first_attempt_ok = False
            LOGGER.warning(
                "validação falhou após correções locais: %s. Reenviando query ao LLM para refazer. request_id=%s",
                validate_err,
                request_id,
            )
            sql = self._request_sql_refazer(
                question=question,
                sql_atual=sql,
                request_id=request_id,
                periods=periods,
                limite=limite,
                real_columns=real_columns,
                motivo=str(validate_err),
            )
            # Pós-correção da nova query
        sql = self._fix_unterminated_strings(sql)
        sql = self._fix_incomplete_sql(sql, question, request_id, periods, limite)
        self._validate_sql(sql)  # Validação final
        
        LOGGER.info(
            "llm sql response complete request_id=%s sql_length=%d sql=%s duration_ms=%.2f",
            request_id,
            len(sql),
            sql,
            (time.perf_counter() - started) * 1000,
        )
        # >>> TELEMETRIA: registra se a PRIMEIRA tentativa do LLM já era válida. <<<
        # Após a reescrita estruturada do prompt, esperamos que este valor seja
        # True na grande maioria dos casos (reduzindo "refazer" e "corrigir").
        LOGGER.info(
            "llm sql first_attempt request_id=%s first_attempt_ok=%s grupos=%s colunas=%d",
            request_id,
            self._first_attempt_ok,
            relevant_grupos,
            len(real_columns),
        )
        return sql

    @staticmethod
    @log_calls
    def _validate_sql(sql: str) -> None:
        normalized = sql.strip().lower()
        if not re.match(r"^(select|with)\b", normalized) or ";" in normalized.rstrip(";"):
            raise ValueError("O LLM retornou uma consulta que não é um SELECT único.")

        if re.search(r"\b(insert|update|delete|drop|alter|create|copy|attach|install|pragma|call)\b", normalized):
            raise ValueError("Consulta bloqueada por conter uma operação não somente leitura.")
        allowed_relations = ("datasus_query_cache", "resumo_diagnosticos", "resumo_cid_periodo", "resumo_anual_cid")
        if not any(re.search(rf"\b{relation}\b", normalized) for relation in allowed_relations):
            raise ValueError("A consulta deve usar uma relação autorizada do cache DATASUS.")


    def _validate_and_fix_columns(self, sql: str, real_columns: list[str]) -> tuple[str, list[str]]:
        """
        Valida se as colunas referenciadas no SQL existem no schema real.
        Se alguma coluna não existir, tenta encontrar uma coluna similar e substituir.
        Retorna uma tupla (sql_corrigido, colunas_nao_resolvidas).
        """
        if not real_columns:
            # Sem schema real não é possível validar colunas; não sinaliza nada
            # como problema para não corromper SQL válido (ex.: quando o cache de
            # colunas por grupo está indisponível).
            LOGGER.warning("real_columns vazio; pulando validação de colunas do SQL")
            return sql, []

        all_real_upper = {col.upper(): col for col in real_columns}
        
        # >>> CORREÇÃO (colunas normalizadas das tabelas resumo_*) <<<
        # As tabelas agregadas resumo_* NÃO têm as colunas CRUAS do DATASUS; elas
        # expõem apenas colunas NORMALIZADAS (atend_ano, atend_mes, sexo, cid,
        # casos). Quando a SQL referencia uma tabela resumo_*, essas colunas são
        # VÁLIDAS mesmo que não constem em real_columns (que vem do dicionário
        # dos grupos, sem 'cid').
        #
        # Sem isto, a validação considerava 'cid' inexistente e a substituía por
        # uma coluna crua (ex.: 'AP_CIDCAS'), gerando:
        #   Binder Error: Referenced column "AP_CIDCAS" not found in FROM clause!
        #   Candidate bindings: "atend_ano", "cid", "casos", ...
        _sql_lower = sql.lower()
        if re.search(r"\bresumo_(diagnosticos|cid_periodo|anual_cid)\b", _sql_lower):
            for _norm in ("atend_ano", "atend_mes", "sexo", "cid", "casos"):
                all_real_upper.setdefault(_norm.upper(), _norm)

        columns_referenced = set(re.findall(r'"([^"]+)"', sql))
        
        sql_without_strings = re.sub(r"'(?:\\.|[^'])*'", '', sql)
        bare_columns = set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\b', sql_without_strings))
        
        # >>> CORREÇÃO C: ignora ALIASES de saída (ex.: COUNT(*) AS total_casos).
        # Sem isso, aliases como "total_casos" eram tratados como colunas
        # inexistentes, disparando um round-trip desnecessário ao LLM
        # (_request_sql_fix) em toda consulta com COUNT(*) AS <nome>.
        aliases = {
            a.upper()
            for a in re.findall(r'\bAS\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', sql, flags=re.IGNORECASE)
        }

        problems = []
        for col in columns_referenced | bare_columns:
            col_upper = col.upper()
            if col_upper in all_real_upper:
                continue
            # >>> CORREÇÃO C: alias de saída nunca é problema de coluna. <<<
            if col_upper in aliases:
                continue
            if col_upper in {"SELECT", "FROM", "WHERE", "LIKE", "AND", "OR", "GROUP", "ORDER", "BY",
                            "LIMIT", "AS", "COUNT", "SUM", "CAST", "UPPER", "TRIM", "NULLIF",
                            "COALESCE", "DESC", "ASC", "JOIN", "LEFT", "ON", "INNER", "OUTER",
                            "CASE", "WHEN", "THEN", "ELSE", "END", "BETWEEN", "IN", "IS", "NOT",
                            "EXISTS", "WITH", "UNION", "ALL", "DISTINCT",
                            "INTEGER", "VARCHAR", "NULL", "TRUE", "FALSE", "EXTRACT", "YEAR",
                            "MONTH", "ROUND", "AVG", "MAX", "MIN", "FILENAME", "PARQUET_PATH",
                            "PARQUET_YEAR", "PARQUET_MONTH",
                            # >>> CORREÇÃO DATA: funções/expressões de data NÃO são colunas. <<<
                            "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "CURRENT",
                            "NOW", "LOCALTIME", "LOCALTIMESTAMP", "GETDATE", "SYSDATE", "TODAY",
                            "DATE_TRUNC", "DATE_PART", "EPOCH", "STRFTIME", "DATEPART",
                            "DAY", "WEEK", "QUARTER", "INTERVAL", "DATE", "TIMESTAMP",
                            "DATASUS_QUERY_CACHE", "RESUMO_DIAGNOSTICOS", "RESUMO_CID_PERIODO",
                            "RESUMO_ANUAL_CID", "DATASUS_QUERY_FILES", "DATASUS",
                            "ILIKE", "NOT", "IS", "NULL", "TRUE", "FALSE",
                            "DISTINCT", "CAST", "COUNT", "SUM", "COALESCE", "NULLIF",
                            "CONCAT", "SUBSTR", "LENGTH", "REPLACE"}:
                continue
            problems.append(col)
        
        if not problems:

            return sql, []
        
        LOGGER.warning("colunas não encontradas no schema: %s. Tentando corrigir...", problems)
        
        fixed_sql = sql
        unresolved: list[str] = []
        for problematic in problems:
            similar = [real for real in real_columns if problematic.upper() in real.upper()]
            if not similar:
                for real in real_columns:
                    if problematic.upper().replace("_", "") in real.upper().replace("_", "") or \
                       real.upper().replace("_", "") in problematic.upper().replace("_", ""):
                        similar.append(real)
            
            if similar:
                replacement = similar[0]
                fixed_sql = re.sub(
                    rf'"({re.escape(problematic)})"',
                    f'"{replacement}"',
                    fixed_sql,
                    flags=re.IGNORECASE,
                )
                fixed_sql = re.sub(
                    rf'\b{re.escape(problematic)}\b',
                    f'"{replacement}"',
                    fixed_sql,
                    flags=re.IGNORECASE,
                )
                LOGGER.warning("coluna %s corrigida para %s no SQL", problematic, replacement)
            else:
                LOGGER.error("não foi possível encontrar coluna equivalente para %s", problematic)
                unresolved.append(problematic)
        

        return fixed_sql, unresolved
