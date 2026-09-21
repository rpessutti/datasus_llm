""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'prompt': mixin _PromptMixin com a construção das seções do prompt
(_build_source_guidance, _build_concept_guidance, _build_fewshot_examples).
Copiado VERBATIM do original.
"""
from __future__ import annotations

from db.column_dictionary import CONCEPT_LABELS

from api.llm_service_parts.base import _ServiceBase


class _PromptMixin(_ServiceBase):
    @staticmethod
    def _build_source_guidance(relevant_grupos: list[str], summary_tables: list[str]) -> str:
        """Gera a seção de orientação sobre as fontes de dados disponíveis."""
        grupo_descricoes = {
            "ps": "Produção Ambulatorial (SIA) — atendimentos ambulatoriais",
            "sp": "Produção Hospitalar SIH/SP — internações hospitalares",
            "rj": "Internações hospitalares SIH",
            "rd": "Registro de dados/RAAS (similar a atendimento/internação)",
            "pa": "Produção Ambulatorial (procedimentos)",
            "ab": "Atenção Básica (SIA/AB)",
            "abo": "Atenção Básica odontológica",
            "acf": "Ações de alimentação e nutrição (ACF)",
            "ad": "Atenção Domiciliar",
            "am": "Atendimentos de média complexidade",
            "amp": "Atendimentos ambulatoriais AMP",
            "aq": "Análise/arquivo AQ (AQ_CID10)",
            "ar": "Arquivo AR (AR_CID10)",
            "atd": "Atendimentos ATD",
            "bi": "Dados BI",
            "er": "Encaminhamentos/regulação ER",
        }
        linhas = [f"  - {g}: {grupo_descricoes.get(g, 'grupo de dados DATASUS')}" for g in relevant_grupos]
        grupos_txt = "\n".join(linhas) if linhas else "  - (nenhum grupo identificado)"
        resumo_txt = ""
        if summary_tables:
            tabelas = ", ".join(summary_tables)
            resumo_txt = (
                f"\nTABELAS AGREGADAS DISPONÍVEIS (prefira-as para contagens/percentuais): {tabelas}\n"
                "  - resumo_diagnosticos(atend_ano INTEGER, atend_mes VARCHAR, sexo VARCHAR, cid VARCHAR, casos BIGINT)\n"
                "  - resumo_cid_periodo(atend_ano INTEGER, atend_mes VARCHAR, sexo VARCHAR, cid VARCHAR, casos BIGINT)\n"
                "  - resumo_anual_cid(atend_ano INTEGER, cid VARCHAR, casos BIGINT)\n"
                "  Sempre filtre resumo_* por atend_ano (e atend_mes quando a pergunta citar mês específico).\n"
                # >>> CORREÇÃO (schema por relação): reforça que os resumos_* NÃO têm
                # colunas cruas do DATASUS. A coluna de diagnóstico dos resumos é
                # 'cid' (não "AP_CIDCAS"/"SP_CIDPRI"), o sexo é 'sexo' (não "AP_SEXO")
                # e o período é 'atend_ano'/'atend_mes'. Sem isto, o LLM escolhia
                # resumo_anual_cid mas filtrava pela coluna crua "AP_CIDCAS",
                # gerando: Binder Error: Referenced column "AP_CIDCAS" not found.
                "  ATENÇÃO: nas tabelas resumo_* a coluna de DIAGNÓSTICO é 'cid' (NÃO use nomes "
                "crus como AP_CIDCAS/SP_CIDPRI/PA_CIDPRI/DIAG_PRINC), a de SEXO é 'sexo' "
                "(NÃO use AP_SEXO) e o período é atend_ano/atend_mes.\n"
            )
        return (
            "DADOS DISPONÍVEIS (FONTES DE DADOS DESTA CONSULTA):\n"
            "  - View principal: datasus_query_cache (colunas cruas detalhadas dos grupos abaixo; "
            "aqui SIM use os nomes crus de CID/sexo/ano).\n"
            "  - Tabelas agregadas resumo_* (colunas NORMALIZADAS: atend_ano, atend_mes, sexo, cid, casos).\n"
            "  - Colunas de período: atend_ano (INTEGER) e atend_mes (VARCHAR '01'..'12').\n"
            f"{grupos_txt}\n"
            f"{resumo_txt}"
        )

    @staticmethod
    def _build_concept_guidance(
        concept_map: dict[str, list[str]],
        groups_map: dict[str, list[str]] | None = None,
    ) -> str:
        """Gera a seção que mapeia conceitos da pergunta -> colunas reais.

        Quando ``groups_map`` é fornecido, cada coluna é anotada com o(s) grupo(s)
        de origem (ex.: ``"SEXOPAC" [ps]``), evitando que o LLM combine colunas
        homônimas de grupos diferentes.
        """
        if not concept_map:
            return ""

        def _fmt(col: str) -> str:
            if groups_map:
                grupos = groups_map.get(col) or groups_map.get(col.upper()) or []
                if grupos:
                    return f'"{col}" [{" ".join(grupos)}]'
            return f'"{col}"'

        # Evita redundância quando o dicionário traz conceitos genéricos e
        # específicos ao mesmo tempo: se há cid_principal/cid_secundario, não
        # emitimos a linha genérica "diagnostico"; idem para morte/obito.
        concept_map = dict(concept_map)
        if concept_map.get("cid_principal") or concept_map.get("cid_secundario"):
            concept_map.pop("diagnostico", None)
        if concept_map.get("morte"):
            concept_map.pop("obito", None)

        linhas = []
        for conceito, cols in concept_map.items():
            rotulo = CONCEPT_LABELS.get(conceito, conceito)
            cols_str = ", ".join(_fmt(c) for c in cols)
            linhas.append(f"  - {rotulo} -> use UMA destas colunas: {cols_str}")
        return (
            "MAPA DE CONCEITOS -> COLUNAS REAIS (use EXATAMENTE uma destas colunas; NUNCA invente nomes):\n"
            + "\n".join(linhas)
        )

    def _build_fewshot_examples(
        self,
        concept_map: dict[str, list[str]],
        limite: int,
        summary_tables: list[str] | None = None,
    ) -> str:
        """Gera exemplos few-shot com base nas colunas REAIS do schema atual."""
        def pick(concept: str) -> str | None:
            cols = concept_map.get(concept)
            return cols[0] if cols else None

        sexo = pick("sexo")
        cid = pick("cid_principal")
        idade = pick("idade")
        municipio = pick("municipio")

        exemplos: list[str] = []

        # >>> CORREÇÃO (schema por relação): few-shot explícito das tabelas
        # AGREGADAS resumo_*. O exemplo demonstra que, num resumo, o filtro de
        # diagnóstico usa a coluna NORMALIZADA 'cid' (e NÃO uma coluna crua como
        # "AP_CIDCAS"). Sem este exemplo, o LLM reusava o nome cru da view
        # detalhada ao consultar o resumo, causando:
        #   Binder Error: Referenced column "AP_CIDCAS" not found in FROM clause!
        # Aqui a própria LLM aprende a forma correta e re-gera a query sozinha.
        summary_tables = summary_tables or []
        if "resumo_anual_cid" in summary_tables:
            exemplos.append(
                "Pergunta: Quantos casos de uma doença (CID X99) foram registrados no total anual?\n"
                "SQL: SELECT atend_ano AS ano, SUM(casos) AS casos "
                "FROM resumo_anual_cid "
                "WHERE atend_ano = 2024 AND cid LIKE 'X99%' "
                f"GROUP BY 1 ORDER BY 1 LIMIT {limite}\n"
                "# Repare: na tabela resumo a coluna de diagnóstico é 'cid' (não \"AP_CIDCAS\"), "
                "o período é 'atend_ano' e o total já está em 'casos' (use SUM(casos), não COUNT(*))."
            )
        if "resumo_cid_periodo" in summary_tables and sexo:
            exemplos.append(
                "Pergunta: Quantos casos de uma doença (CID X99) por sexo?\n"
                "SQL: SELECT sexo, SUM(casos) AS casos "
                "FROM resumo_cid_periodo "
                "WHERE atend_ano = 2024 AND cid LIKE 'X99%' "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT {limite}\n"
                "# Repare: no resumo o sexo é a coluna 'sexo' (não \"AP_SEXO\") e o "
                "diagnóstico é 'cid'."
            )

        if sexo and cid:
            # >>> CORREÇÃO CID: NÃO usar um código de doença real (ex.: 'A90' para dengue)
            # como se fosse verdade no exemplo. Modelos memorizam o exemplo e passam a
            # "deduzir" CIDs por conta própria, gerando códigos errados/inexistentes.
            # Usamos um placeholder claramente fictício (X99) e instruímos a só usar
            # códigos presentes na lista de CIDs reais (ver cid_grounding).
            exemplos.append(
                "Pergunta: Quantos casos de uma doença (CID X99) por sexo?\n"
                f'SQL: SELECT COALESCE(NULLIF(TRIM(CAST("{sexo}" AS VARCHAR)), \'\'), \'IGNORADO\') AS sexo, '
                f'COUNT(*) AS casos FROM datasus_query_cache '
                f'WHERE UPPER(TRIM(CAST("{cid}" AS VARCHAR))) LIKE \'X99%\' '
                f'GROUP BY 1 ORDER BY 2 DESC LIMIT {limite}'
            )

        if municipio:
            exemplos.append(
                "Pergunta: Quais os 10 municípios com mais registros?\n"
                f'SQL: SELECT CAST("{municipio}" AS VARCHAR) AS municipio, COUNT(*) AS total '
                f'FROM datasus_query_cache WHERE "{municipio}" IS NOT NULL '
                f"AND TRIM(CAST(\"{municipio}\" AS VARCHAR)) != '' "
                f'GROUP BY 1 ORDER BY 2 DESC LIMIT {limite}'
            )

        # >>> CORREÇÃO E: exemplo top-N por município COM filtro de CID (caso do log),
        # reforçando UPPER(TRIM(...)) LIKE 'CODIGO%' e a exclusão de municípios nulos.
        if municipio and cid:
            exemplos.append(
                "Pergunta: Quais as 10 cidades com mais casos de uma doença (CID X99) em 2024?\n"
                f'SQL: SELECT CAST("{municipio}" AS VARCHAR) AS cidade, COUNT(*) AS total_casos '
                f'FROM datasus_query_cache '
                f"WHERE UPPER(TRIM(CAST(\"{cid}\" AS VARCHAR))) LIKE 'X99%' "
                f"AND atend_ano = 2024 "
                f'AND "{municipio}" IS NOT NULL AND TRIM(CAST("{municipio}" AS VARCHAR)) != \'\' '
                f'GROUP BY 1 ORDER BY 2 DESC LIMIT {limite}'
            )

        exemplos.append(
            "Pergunta: Qual a evolução do total de registros por ano?\n"
            "SQL: SELECT atend_ano AS ano, COUNT(*) AS total FROM datasus_query_cache "
            f"GROUP BY 1 ORDER BY 1 LIMIT {limite}"
        )

        if idade:
            exemplos.append(
                "Pergunta: Qual a idade média dos pacientes?\n"
                f'SQL: SELECT AVG(TRY_CAST("{idade}" AS DOUBLE)) AS idade_media FROM datasus_query_cache '
                f'WHERE TRY_CAST("{idade}" AS DOUBLE) IS NOT NULL LIMIT {limite}'
            )

        if not exemplos:
            return ""
        return "EXEMPLOS (siga EXATAMENTE este estilo, usando as colunas reais):\n\n" + "\n\n".join(exemplos)