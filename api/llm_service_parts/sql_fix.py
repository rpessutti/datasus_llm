""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'sql_fix': mixin _SqlFixMixin com correções determinísticas de SQL
(anos crus, funções de data do sistema, SQL incompleto, strings não
terminadas, normalização de relações e extração de SQL).
Copiado VERBATIM do original.
"""
from __future__ import annotations

import logging
import re

from api.llm_service_parts.base import _ServiceBase

LOGGER = logging.getLogger("datasus_llm")


class _SqlFixMixin(_ServiceBase):
    def _fix_incomplete_sql(self, sql: str, question: str, request_id: str, periods: set[tuple[str, str]], limite: int = 100) -> str:
        """
        Detecta e tenta corrigir SQL truncado/incompleto gerado pelo LLM.
        Se o SQL estiver incompleto, retorna uma versão corrigida.
        """
        # Verifica balanceamento de parênteses
        open_parens = sql.count("(") - sql.count(")")
        if open_parens > 0:
            LOGGER.warning("SQL incompleto: %d parênteses não fechados. request_id=%s", open_parens, request_id)
            sql = sql + ")" * open_parens

        # Verifica se o SQL termina de forma incompleta
        sql_stripped = sql.rstrip()

        # Padrões de SQL incompleto
        incomplete_endings = [
            r"LIKE\s*'?\s*$",
            r"AND\s*$",
            r"OR\s*$",
            r"=\s*$",
            r"IN\s*\(?\s*$",
            r"BETWEEN\s*$",
            r"\bWHERE\s*$",
            r"\bGROUP\s+BY\s*$",
            r"\bORDER\s+BY\s*$",
            r"\bLIMIT\s*$",
        ]

        has_incomplete_ending = any(
            re.search(pattern, sql_stripped, flags=re.IGNORECASE)
            for pattern in incomplete_endings
        )

        if has_incomplete_ending:
            LOGGER.warning(
                "SQL truncado detectado request_id=%s sql_final=%r. Corrigindo...",
                request_id,
                sql[-100:],
            )

            # Se terminou com LIKE ', completa com aspas vazias
            if re.search(r"LIKE\s*'?\s*$", sql_stripped, flags=re.IGNORECASE):
                sql = sql + "'"

            # Fecha parênteses pendentes
            open_parens = sql.count("(") - sql.count(")")
            if open_parens > 0:
                sql = sql + ")" * open_parens

            # Adiciona LIMIT 100 se não tiver
            if not re.search(r"\bLIMIT\s+\d+\b", sql, flags=re.IGNORECASE):
                sql = sql.rstrip()
                if sql.endswith(";"):
                    sql = sql[:-1]
                sql = sql + f" LIMIT {limite}"

            LOGGER.info("SQL corrigido após truncamento request_id=%s sql_length=%d", request_id, len(sql))

        # Verificação adicional: query com WITH mas sem SELECT final
        if re.search(r"\bWITH\b", sql, flags=re.IGNORECASE) and not re.search(r"\bSELECT\b", sql.split("WITH", 1)[1], flags=re.IGNORECASE):
            LOGGER.warning("SQL com WITH incompleto. request_id=%s", request_id)
            sql = f"SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT {limite}"

        # Se o SQL não contém FROM, não é válido
        if not re.search(r"\bFROM\b", sql, flags=re.IGNORECASE):
            LOGGER.warning("SQL sem FROM. Usando fallback COUNT(*). request_id=%s", request_id)
            sql = f"SELECT COUNT(*) AS total_registros FROM datasus_query_cache LIMIT {limite}"

        return sql


    @staticmethod
    def _strip_foreign_year_filters(
        sql: str, periods: set[tuple[str, str]], request_id: str
    ) -> str:
        """Remove/reescreve filtros de ano que NÃO pertencem ao escopo real do cache.

        >>> SOLUÇÃO F: bug "últimos 3 anos" trouxe dado errado.
        A view ``datasus_query_cache`` JÁ está filtrada aos períodos selecionados
        (ex.: 2024..2026). Ainda assim, ao ver "últimos 3 anos", o LLM inventou
        ``atend_ano IN (2022, 2023, 2024)`` — ancorado no seu corte de treino.
        Como 2022/2023 não existem no cache, a interseção silenciosamente
        descartava 2025/2026 e o resultado contava apenas 2024 (dado ERRADO para
        uma pergunta de 3 anos).

        Regra determinística:
          * coleta os anos REAIS do escopo (``periods``);
          * se a query cita ``atend_ano``:
              - ``= N`` / ``IN (...)`` -> reescreve para os anos do escopo;
              - descarta anos pedidos que não existem no escopo;
          * se o filtro resultar vazio, REMOVE a cláusula de ano inteira (a view
            já cobre o período).
        """
        if not periods:
            return sql
        anos_escopo = sorted({int(y) for y, _ in periods if str(y).isdigit()})
        if not anos_escopo:
            return sql
        if not re.search(r"atend_ano", sql, flags=re.IGNORECASE):
            return sql

        anos_escopo_str = ", ".join(str(a) for a in anos_escopo)
        alterou = False

        # 1) atend_ano [NOT] IN (2022, 2023, 2024) -> IN (<escopo>).
        #
        # IMPORTANTE: substituímos pelo ESCOPO COMPLETO, não pela interseção.
        # A lista de anos do LLM é ESPÚRIA (ele "chutou" os 3 últimos anos pelo
        # seu corte de treino). A view já está filtrada ao escopo real; então o
        # correto é usar TODOS os anos do escopo (2024..2026). Se mantivéssemos
        # só a interseção ({2024}), o bug original persistiria (2025/2026
        # descartados).
        def _sub_in(m: re.Match) -> str:
            nonlocal alterou
            negado = m.group(1)  # grupo 1 = o 'NOT ' opcional, se houver
            pedidos = [int(x) for x in re.findall(r"\d{4}", m.group(2))]
            alterou = True
            if negado:
                # 'NOT IN' com anos fora do escopo: como todos os anos pedidos
                # podem não existir, o predicado é sempre verdadeiro -> TRUE.
                LOGGER.warning(
                    "filtro atend_ano NOT IN (%s) neutralizado; escopo=%s request_id=%s",
                    pedidos,
                    anos_escopo,
                    request_id,
                )
                return "1=1"
            LOGGER.warning(
                "filtro atend_ano IN (%s) do LLM substituído pelo escopo real %s. request_id=%s",
                pedidos,
                anos_escopo,
                request_id,
            )
            return f"atend_ano IN ({anos_escopo_str})"

        sql = re.sub(
            r"atend_ano\s+(NOT\s+)?IN\s*\(([^)]*)\)",
            _sub_in,
            sql,
            flags=re.IGNORECASE,
        )

        # 2) atend_ano = 2022 -> atend_ano IN (<escopo>), quando o ano
        #    NÃO pertence ao escopo real (ano espúrio). Se pertencer, mantém
        #    (a pergunta pode ter citado o ano explicitamente).
        def _sub_eq(m: re.Match) -> str:
            nonlocal alterou
            ano = int(m.group(1))
            if ano in anos_escopo:
                return m.group(0)
            alterou = True
            LOGGER.warning(
                "filtro atend_ano = %d fora do escopo %s; usando escopo. request_id=%s",
                ano,
                anos_escopo,
                request_id,
            )
            return f"atend_ano IN ({anos_escopo_str})"

        sql = re.sub(
            r"\batend_ano\s*=\s*(\d{4})",
            _sub_eq,
            sql,
            flags=re.IGNORECASE,
        )
        # 3) atend_ano BETWEEN a AND b -> se algum limite estiver FORA do
        #    escopo, o intervalo é espúrio e é substituído pelo escopo inteiro.
        #    (Não intersectamos: limites menores que o escopo real cortariam
        #    anos válidos, ex.: BETWEEN 2022 AND 2024 descartaria 2025/2026.)
        def _sub_between(m: re.Match) -> str:
            nonlocal alterou
            a, b = int(m.group(1)), int(m.group(2))
            if a in anos_escopo and b in anos_escopo:
                return m.group(0)
            alterou = True
            LOGGER.warning(
                "filtro atend_ano BETWEEN %d AND %d parcialmente fora do escopo %s; "
                "usando escopo. request_id=%s",
                a,
                b,
                anos_escopo,
                request_id,
            )
            return f"atend_ano IN ({anos_escopo_str})"

        sql = re.sub(
            r"\batend_ano\s+BETWEEN\s+(\d{4})\s+AND\s+(\d{4})",
            _sub_between,
            sql,
            flags=re.IGNORECASE,
        )

        if alterou:
            LOGGER.warning(
                "filtros de atend_ano ajustados ao escopo real %s. request_id=%s",
                anos_escopo,
                request_id,
            )
        return sql

    @staticmethod
    def _fix_atend_ano_casts(sql: str, request_id: str) -> str:
        """>>> CORREÇÃO I: cast de atend_ano (VARCHAR->INTEGER) na comparação <<<

        >>> FASE 3B: ``atend_ano`` agora é INTEGER em TODOS os caminhos <<<
        (tabela física, view S1 e fallback legado em ``db/cache.py``). Portanto,
        em operação normal esta função é um NO-OP SEMÂNTICO: envolver uma coluna
        já INTEGER em ``CAST(... AS INTEGER)`` não altera o resultado.

        Ela é MANTIDA por robustez/idempotência: protege contra ambientes com
        cache intermediário ainda em VARCHAR (ex.: caches migrados pela PARTE 3
        antes da conversão VARCHAR->INTEGER) ou contra qualquer caminho futuro
        que volte a expor ``atend_ano`` como VARCHAR. Quando o LLM compara
        ``atend_ano`` com literais INTEIROS, o DuckDB abortava com:

          Binder Error: Cannot mix values of type VARCHAR and INTEGER_LITERAL
          in BETWEEN clause - an explicit cast is required
          (ou: Cannot compare values of type VARCHAR and type BIGINT)

        Correção determinística e IDEMPOTENTE: envolve ``atend_ano`` em
        ``CAST(atend_ano AS INTEGER)`` nas cláusulas em que ele é comparado com
        valores NUMÉRICOS (``=``, ``<>``, ``!=``, ``<``, ``<=``, ``>``, ``>=``,
        ``BETWEEN ... AND ...``, ``IN (numéricos)``). Comparações com STRINGS
        (ex.: ``atend_ano IN ('2024','2025')``) NÃO são alteradas.

        É chamada APÓS ``_strip_foreign_year_filters``, que reescreve os
        filtros de ano do LLM para o formato ``atend_ano IN (<anos>)`` com
        literais inteiros — que também precisa do cast.
        """
        if not sql:
            return sql
        if not re.search(r"\batend_ano\b", sql, flags=re.IGNORECASE):
            return sql

        original = sql
        # Já castado? -> não faz nada (idempotência).
        cast_prefix = "CAST(atend_ano AS INTEGER)"

        def _ja_castado(start: int) -> bool:
            prefixo = sql[:start].rstrip().upper()
            return prefixo.endswith("CAST(ATEND_ANO AS INTEGER")

        # 1) BETWEEN <num> AND <num>  ->  conjunto numérico sem string.
        between_re = re.compile(
            r"\batend_ano\b(\s*)(BETWEEN\s+|NOT\s+BETWEEN\s+)"
            r"(-?\d+(?:\.\d+)?)\s+AND\s+(-?\d+(?:\.\d+)?)",
            flags=re.IGNORECASE,
        )
        sql = between_re.sub(
            lambda m: f"{cast_prefix}{m.group(1)}{m.group(2)}{m.group(3)} AND {m.group(4)}",
            sql,
        )

        # 2) Comparadores simples com número: = <> != < <= > >=.
        compar_re = re.compile(
            r"\batend_ano\b(\s*(?:=|<>|!=|<=|>=|<|>)\s*)(-?\d+(?:\.\d+)?)\b",
            flags=re.IGNORECASE,
        )
        sql = compar_re.sub(
            lambda m: f"{cast_prefix}{m.group(1)}{m.group(2)}",
            sql,
        )

        # 3) IN (<lista>) com >=1 literal NUMÉRICO (ex.: IN (2024, 2025)).
        in_re = re.compile(
            r"\batend_ano\b(\s+(?:NOT\s+)?IN\s*\()([^)]*)\)",
            flags=re.IGNORECASE,
        )

        def _sub_in(m: re.Match) -> str:
            conteudo = m.group(2)
            # Só trata listas 100% numéricas (sem strings) para não quebrar
            # comparações legítimas com VARCHAR ('2024').
            tokens = [t.strip() for t in conteudo.split(",") if t.strip()]
            if not tokens or any("'" in t or '"' in t for t in tokens):
                return m.group(0)
            if not all(re.fullmatch(r"-?\d+(?:\.\d+)?", t) for t in tokens):
                return m.group(0)
            return f"{cast_prefix}{m.group(1)}{conteudo})"

        sql = in_re.sub(_sub_in, sql)

        if sql != original:
            LOGGER.warning(
                "cast aplicado em atend_ano (VARCHAR->INTEGER) para comparação numérica request_id=%s",
                request_id,
            )
        return sql

    # Colunas "cruas" de ano presentes nos grupos DATASUS. Como a view
    # ``datasus_query_cache`` as expõe como VARCHAR (o UNION ALL preserva o tipo
    # original do parquet), qualquer comparação/ordenação com inteiro quebra:
    #   Binder Error: Cannot compare values of type VARCHAR and type BIGINT
    # Ver cache/datasus.log request_id=a520e3247737.

    @classmethod
    def _fix_raw_year_casts(cls, sql: str, request_id: str) -> str:
        """>>> CORREÇÃO G (cast de ano cru) <<<

        O LLM frequentemente usa a coluna CRUA de ano (ex.: ``"ANO_CMPT"``) em
        vez da coluna derivada ``atend_ano`` e a compara com inteiro/expressão
        de data (ex.: ``"ANO_CMPT" >= (EXTRACT(YEAR FROM CURRENT_DATE) - 3)``).
        Como a coluna crua é VARCHAR na temp table, o DuckDB aborta com:
        ``Cannot compare values of type VARCHAR and type BIGINT``.
        Ver cache/datasus.log request_id=a520e3247737.

        Correção determinística e IDEMPOTENTE, aplicada por PREDICADO (cláusulas
        separadas por AND/OR), para não tocar em filtros alheios ao ano:
          1. Se o predicado cita a coluna crua de ano, normaliza literais de ano
             entre aspas (``'2024'`` -> ``2024``) — sem isto,
             ``CAST(col AS INTEGER) IN ('2024','2025')`` voltaria a estourar
             (INTEGER vs VARCHAR). Só converte tokens de 4 dígitos que formam
             um ano plausível (1900..2099).
          2. Envolve a coluna crua em ``CAST(... AS INTEGER)``, exceto quando
             já estiver dentro de ``CAST("..." AS INTEGER)`` (idempotência).

        Predicados sem coluna crua (ex.: ``codigo_ibge = '3550'``) ficam
        INTACTOS, eliminando o risco de reinterpretar strings numéricas não-ano.
        """
        if not sql:
            return sql

        cols_re = "|".join(re.escape(c) for c in cls._RAW_YEAR_COLUMNS)
        col_re = re.compile(r'"(%s)"' % cols_re, flags=re.IGNORECASE)
        if not col_re.search(sql):
            return sql

        original = sql

        # Divide preservando os separadores AND/OR usando re.split com grupo.
        partes = re.split(r"(AND|OR)", sql, flags=re.IGNORECASE)
        for idx, parte in enumerate(partes):
            # Só mexe em fragmentos (não nos separadores AND/OR).
            if idx % 2 == 1:
                continue
            if not col_re.search(parte):
                continue

            def _norm_year_literal(m: re.Match) -> str:
                ano = m.group(1)
                return ano if 1900 <= int(ano) <= 2099 else m.group(0)

            parte = re.sub(r"'(\d{4})'", _norm_year_literal, parte)

            # Cast da coluna crua nesta parte, sem re-wrap.
            resultado = []
            ultimo = 0
            for m in col_re.finditer(parte):
                ini, fim = m.start(), m.end()
                prefixo = parte[max(0, ini - 5):ini].upper()
                if prefixo.endswith('CAST('):
                    continue
                resultado.append((ini, fim, m.group(1)))
            if resultado:
                pedacos = []
                ul = 0
                for ini, fim, col in resultado:
                    pedacos.append(parte[ul:ini])
                    pedacos.append(f'CAST("{col}" AS INTEGER)')
                    ul = fim
                pedacos.append(parte[ul:])
                parte = "".join(pedacos)
            partes[idx] = parte

        sql = "".join(partes)

        if sql != original:
            LOGGER.warning(
                "cast aplicado em coluna(s) de ano crua (VARCHAR->INTEGER) request_id=%s",
                request_id,
            )
        return sql


    @classmethod
    def _fix_system_date_functions(
        cls, sql: str, request_id: str, system_year: int | None = None
    ) -> str:
        """>>> CORREÇÃO DATA (determinística e idempotente) <<<

        Substitui funções de data do SISTEMA (``CURRENT_DATE``, ``NOW``,
        ``CURRENT_TIMESTAMP``, ``LOCALTIMESTAMP``, etc.) pelo ANO ATUAL do
        sistema como LITERAL numérico. A base NÃO possui coluna de data: o
        recorte temporal é dado por ``atend_ano`` (INTEGER). O LLM, porém, ao
        ver "este ano"/"últimos N anos", frequentemente emite expressões como
        ``EXTRACT(YEAR FROM CURRENT_DATE) - 3`` ou ``atend_ano = YEAR(NOW())``,
        que (a) não correspondem a colunas reais e (b) usam o relógio da máquina
        (que pode divergir do escopo do cache).

        Regras (por ordem, sempre sobre o SQL INTEIRO, idempotente):
          1. ``YEAR(<função de data>)`` / ``EXTRACT(YEAR FROM <função>)`` -> ano
             literal do sistema.
          2. Demais usos da função (ex.: ``CURRENT_DATE`` cru) -> ano do sistema
             (melhor que deixar uma função proibida que quebra a execução).

        Só age se houver alguma função de data no SQL; caso contrário devolve o
        texto original sem alterações.
        """
        if not sql:
            return sql

        import datetime

        if system_year is None:
            system_year = datetime.date.today().year
        ano_txt = str(int(system_year))

        funcoes_re = "|".join(re.escape(f) for f in cls._SYSTEM_DATE_FUNCTIONS)
        # Casa a chamada com parênteses VAZIOS opcionais, SEM consumir o espaço
        # seguinte: NOW() casa 'NOW()' ; CURRENT_DATE casa apenas 'CURRENT_DATE'
        # (sem engolir o espaço antes de, por ex., 'LIMIT').
        chamada = rf"(?:{funcoes_re})(?:\(\s*\))?"
        padrao = re.compile(chamada, flags=re.IGNORECASE)
        if not padrao.search(sql):
            return sql

        original = sql

        # 1) YEAR(<função>...) e EXTRACT(YEAR FROM <função>...) -> ano literal.
        sql = re.sub(
            rf"(?:EXTRACT\s*\(\s*YEAR\s+FROM\s+{chamada}\s*\)|YEAR\s*\(\s*{chamada}\s*\))",
            ano_txt,
            sql,
            flags=re.IGNORECASE,
        )

        # 2) Qualquer uso restante da função -> ano literal do sistema.
        sql, n = padrao.subn(ano_txt, sql)

        if sql != original:
            LOGGER.warning(
                "funções de data do sistema substituídas pelo ano %s (n=%d) request_id=%s",
                ano_txt,
                n,
                request_id,
            )
        return sql

    @staticmethod
    def _fix_unterminated_strings(sql: str) -> str:
        """
        Corrige strings não terminadas em SQL gerado por LLM.
        Detecta aspas simples que não foram fechadas e tenta completá-las.
        Retorna o SQL corrigido ou o original se não conseguir corrigir.
        """
        # Verifica se o SQL tem strings balanceadas
        last_open_quote = -1
        in_string = False
        escaped = False
        
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
        
        # Há uma string não terminada começando em last_open_quote
        LOGGER.warning("SQL contém string não terminada. Tentando corrigir...")
        
        # Inclui a aspas aberta no prefix para análise
        prefix = sql[:last_open_quote]  # Tudo antes da aspas aberta
        rest = sql[last_open_quote:]  # Começa com a aspas aberta
        
        # Caso 1: Padrão LIKE 'xxx  (sem fechamento) - procura no texto antes + a aspas
        # Processa a string, que começa na posição da aspas aberta
        # rest = "'G12" ou "'" etc.
        # Depois da primeira aspas, o resto é o conteúdo
        after_quote = rest[1:] if rest.startswith("'") else rest
        
        # Caso 1a: Padrão LIKE 'xxx  (sem fechamento)
        like_before = re.search(r"(?:LIKE|ILIKE)\s*$", prefix, flags=re.IGNORECASE)
        if like_before:
            value = after_quote  # O conteúdo após a aspas
            # Se o valor parece um código válido (letras, números, %, ., -, espaço), fecha com aspas
            if value and re.match(r"^[%A-Za-z0-9._\-\s]+$", value):
                fixed_sql = sql + "'"  # Apenas adiciona aspas de fechamento no final
                if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                    fixed_sql = fixed_sql + " LIMIT 100"
                LOGGER.warning("SQL corrigido: LIKE incompleto fechado. value=%r", value)
                return fixed_sql
            if value and not value.strip():
                # String vazia após LIKE - completa com aspas vazias
                fixed_sql = sql + "'"
                if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                    fixed_sql = fixed_sql + " LIMIT 100"
                LOGGER.warning("SQL corrigido: LIKE com string vazia completado")
                return fixed_sql
        
        # Caso 2: Termina com operador de comparação (ex: = ')
        before = prefix.rstrip()
        if re.search(r"(?:=|<>|!=|<=|>=)\s*$", before, flags=re.IGNORECASE):
            # String vazia ou incompleta após operador
            # Se tem conteúdo após a aspas, fecha com aspas
            if after_quote and re.match(r"^[%A-Za-z0-9._\-\s]+$", after_quote):
                fixed_sql = sql + "'"
                if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                    fixed_sql = fixed_sql + " LIMIT 100"
                LOGGER.warning("SQL corrigido: string após operador fechada. value=%r", after_quote)
                return fixed_sql
            # Senão, string vazia - completa com aspas duplas vazias
            fixed_sql = sql[:last_open_quote] + "''" + sql[last_open_quote+1:]
            if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                fixed_sql = fixed_sql + " LIMIT 100"
            LOGGER.warning("SQL corrigido: string vazia após operador completada")
            return fixed_sql
        
        # Caso 3: Fallback - apenas fecha a string com aspas simples
        # Caso 3: Fallback - apenas fecha a string com aspas simples
        if last_open_quote >= 0:
            # Se há conteúdo válido, fecha com aspas simples
            if after_quote and re.match(r"^[%A-Za-z0-9._\-\s]+$", after_quote):
                fixed_sql = sql + "'"
                if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                    fixed_sql = fixed_sql + " LIMIT 100"
                LOGGER.warning("SQL corrigido: string não terminada fechada. value=%r", after_quote)
                return fixed_sql
            # Senão, substitui por aspas vazias
            fixed_sql = sql[:last_open_quote] + "''" + sql[last_open_quote+1:]
            if not re.search(r"\bLIMIT\s+\d+\b", fixed_sql, flags=re.IGNORECASE):
                fixed_sql = fixed_sql + " LIMIT 100"
            LOGGER.warning("SQL corrigido: string não terminada fechada com aspas vazias")
            return fixed_sql
        
        return sql


    @staticmethod
    def _normalize_relations(sql: str) -> str:
        """Reescreve relacoes nao autorizadas para a view datasus_query_cache.

        Modelos de codigo costumam gerar exemplos com placeholders como
        'sua_tabela', 'tabela', 'nome_da_tabela' etc. Como essas relacoes nao
        existem no cache DATASUS, a validacao falharia com
        'A consulta deve usar uma relacao autorizada do cache DATASUS.'.
        Aqui trocamos o alvo dessas relacoes por datasus_query_cache.
        """
        if not sql or not sql.strip():
            return sql

        allowed = (
            "datasus_query_cache",
            "resumo_diagnosticos",
            "resumo_cid_periodo",
            "resumo_anual_cid",
        )
        placeholders = {
            "sua_tabela", "tabela", "tabela_exemplo", "nome_da_tabela",
            "nome_tabela", "minha_tabela", "table_name", "your_table",
            "mytable", "dados", "dataset", "tabela_dados",
        }

        def _replace(match: re.Match) -> str:
            keyword = match.group(1)
            relation = match.group(2)
            rest = match.group(3) or ""
            bare = relation.strip().strip('"').strip("`").strip("[]").lower()
            if any(name in bare for name in allowed):
                return match.group(0)
            if bare in placeholders or bare.split(".")[-1] in placeholders:
                LOGGER.warning(
                    "relacao nao autorizada %r normalizada para datasus_query_cache", relation
                )
                return f"{keyword} datasus_query_cache{rest}"
            return match.group(0)

        sql = re.sub(
            r"\b(FROM|JOIN)\s+([A-Za-z_][\w.\"`\[\]]*)(\s+(?:AS\s+)?[A-Za-z_]\w*)?",
            _replace,
            sql,
            flags=re.IGNORECASE,
        )
        return sql

    @staticmethod
    def _extract_sql(content: str | None) -> str:
        if not content or not content.strip():
            raise ValueError("O LLM não retornou uma consulta SQL.")
        text = content.strip()
        fenced_match = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced_match:
            text = fenced_match.group(1).strip()
        else:
            statement_match = re.search(r"\b(?:SELECT|WITH)\b", text, flags=re.IGNORECASE)
            if statement_match:
                text = text[statement_match.start():].strip()
        text = text.strip().strip("`").strip()
        if text.endswith(";"):
            text = text[:-1].rstrip()
        
        # Remove texto que apareça após o fim da query SQL (ex: comentários ou explicações)
        # Procura por ponto e vírgula que termina a query principal
        semicolon_match = re.search(r";(?!\s*\d)", text)
        if semicolon_match:
            text = text[:semicolon_match.start()].rstrip()
        
        return text
