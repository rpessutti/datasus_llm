""">>> Módulo gerado a partir de api/llm_service.py (original preservado) <<<

Segmento 'cid': mixin _CidMixin com o domínio de CID (descoberta, grounding,
prefixos, validação e correção determinística de filtros CID).
Copiado VERBATIM do original.
"""
from __future__ import annotations

import logging
import re

from config.constants import DIAGNOSIS_COLUMN_NAMES

from api.llm_service_parts.base import _ServiceBase

LOGGER = logging.getLogger("datasus_llm")


class _CidMixin(_ServiceBase):
    def _discover_cid_via_llm(self, question: str, request_id: str) -> str:
        """PRIMEIRA RODADA (opção A): a própria LLM consulta a tabela `cid10`.

        Em vez de o Python injetar o de-para pronto, a LLM monta um SELECT na
        tabela `cid10` do DuckDB para descobrir qual(is) código(s) CID-10
        correspondem à doença citada. O resultado é devolvido à LLM na
        SEGUNDA RODADA (geração do SQL final), como contexto.

        Retorna um bloco de texto com os CIDs encontrados (possivelmente
        vazio). Nunca derruba a geração de SQL.
        """
        if not self.client:
            return ""
        # Heurística barata: só vale a pena consultar o cid10 se a pergunta
        # menciona algum termo "de doença" (evita round-trip em perguntas de
        # agregação pura, ex.: 'total de atendimentos por mês').
        if not self._question_mentions_disease(question):
            LOGGER.info("cid discovery ignorado (sem termo de doença) request_id=%s", request_id)
            return ""

        discovery_prompt = (
            "Você é um especialista em SQL DuckDB. Existe uma tabela `cid10` no DuckDB com "
            "o seguinte schema: subcat VARCHAR (código CID-10 SEM ponto, ex.: 'A90', 'B019'), "
            "descricao VARCHAR (descrição oficial), descabrev VARCHAR (descrição curta).\n\n"
            "Escreva UMA consulta SQL DuckDB (SOMENTE o SQL, sem markdown) que liste os "
            "códigos CID-10 (coluna subcat) e suas descrições (coluna descricao) que "
            "correspondam à doença mencionada na pergunta do usuário. Use busca textual "
            "com ILIKE/upper na coluna descricao. Ordene por subcat e limite a 50 linhas.\n\n"
            f"Pergunta do usuário: {question}\n"
            "SQL:"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=200,
                timeout=min(self.settings.llm_timeout, self.settings.llm_fix_timeout),
                messages=[{"role": "user", "content": discovery_prompt}],
            )
            raw = (response.choices[0].message.content or "").strip()
        except Exception as exc:
            LOGGER.warning("cid discovery: falha na chamada ao LLM: %s request_id=%s", exc, request_id)
            return ""

        sql = self._extract_sql(raw)
        if not sql or "cid10" not in sql.lower():
            LOGGER.info("cid discovery: LLM não consultou cid10 (raw=%r) request_id=%s", raw[:200], request_id)
            return ""

        # Executa a consulta à tabela cid10 (read-only, escopo restrito).
        try:
            rows = self.cache.query_kb(sql)
        except Exception as exc:
            LOGGER.warning("cid discovery: falha ao executar SQL no cid10: %s request_id=%s", exc, request_id)
            return ""

        if not rows:
            LOGGER.info("cid discovery: nenhum CID encontrado request_id=%s sql=%s", request_id, sql[:300])
            return ""

        linhas: list[str] = []
        for row in rows[:50]:
            subcat = str(row.get("subcat") or "").strip().upper()
            descricao = str(row.get("descricao") or "").strip()
            if subcat:
                linhas.append(f"  - {subcat} = {descricao}")
        if not linhas:
            return ""
        LOGGER.info(
            "cid discovery OK request_id=%s encontrados=%d sql=%s",
            request_id, len(linhas), sql[:300],
        )
        return (
            "CIDs DESCOBERTOS PELA PRÓPRIA LLM NA TABELA cid10 (USE ESTES CÓDIGOS):\n"
            + "\n".join(linhas)
            + "\nUse EXATAMENTE um destes códigos (coluna subcat, SEM ponto) na cláusula "
            "LIKE do filtro de CID, com LIKE 'CODIGO%'."
        )

    def _question_mentions_disease(self, question: str) -> bool:
        """Heurística: True se a pergunta parece citar uma doença/agravo.

        Evita gastar um round-trip de descoberta de CID em perguntas puramente
        agregadas (ex.: 'total de atendimentos por mês')."""
        if not question:
            return False
        norm = question.casefold()
        # Marcadores de doença/agravo no enunciado.
        marcadores = ("casos de", "caso de", "diagnost", "doenca", "doença", "cid",
                      "interna", "obito", "óbito", "morte", "incidencia", "incidência")
        if any(m in norm for m in marcadores):
            return True
        # Qualquer termo de doença conhecido nos aliases.
        try:
            from db.cid_map import CID_ALIASES, _normalize
            qn = _normalize(question)
            for alias in CID_ALIASES:
                a = _normalize(alias)
                if a and a in qn:
                    return True
        except Exception:
            pass
        return False

    def _resolve_cid_grounding(self, question: str, request_id: str) -> str:
        """Resolve doença->CID usando o de-para oficial (CID_10.CSV / CidMap).

        Este é o grounding SEMÂNTICO: injeta no prompt a correspondência
        EXPLÍCITA entre a doença citada na pergunta e o(s) código(s) CID-10
        corretos, derivados do arquivo oficial ``CID_10.CSV`` (via ``CidMap``).

        Sem isso o LLM escolhe um código a esmo dentro da lista de prefixos
        reais (ex.: 'A67' para sarampo, quando o correto é 'B05').

        Retorna um bloco de texto (possivelmente vazio) para concatenar ao prompt.
        """
        try:
            from db.cid_map import get_cid_map
        except Exception as exc:  # pragma: no cover - import defensivo
            LOGGER.debug("cid_map indisponível para grounding: %s", exc)
            return ""

        try:
            cid_map = get_cid_map()
            matches = cid_map.identificar(question, max_resultados=8)
        except Exception as exc:  # nunca derruba a geração de SQL
            LOGGER.warning("falha ao resolver CID p/ pergunta (grounding): %s", exc)
            return ""

        if not matches:
            return ""

        linhas: list[str] = []
        vistos: set[str] = set()
        for entry in matches[:6]:
            codigo = entry.inicio.upper()
            # >>> SOLUÇÃO A (bug sarampo -> A67/B20): colapsa a SUBCATEGORIA de
            # 4 chars para a CATEGORIA de 3 chars (ex.: B058 -> B05). O filtro
            # SQL usa LIKE 'B05%'; injetar 'B058' fazia o LLM não reconhecer o
            # código do exemplo e cair no conhecimento paramétrico (A67/B20).
            if len(codigo) == 4:
                codigo = codigo[:3]
            if codigo in vistos:
                continue
            vistos.add(codigo)
            descricao = (entry.descricao or "").strip()
            # Remove sufixos de especificidade p/ manter a descrição da categoria.
            descricao = re.split(
                r"\s+(?:complicad[oa]s?|com |sem )", descricao, maxsplit=1
            )[0].strip() or descricao
            if len(descricao) > 90:
                descricao = descricao[:87] + "..."
            linhas.append(f"  - {codigo} = {descricao}")
        if not linhas:
            return ""

        LOGGER.info(
            "cid de-para request_id=%s codigos=%s",
            request_id,
            ",".join(vistos),
        )
        exemplo = next(iter(vistos))
        return (
            "DE-PARA DOENÇA -> CID-10 (fonte oficial CID_10.CSV; USE ESTES CÓDIGOS):\n"
            + "\n".join(linhas)
            + "\nA doença citada na pergunta deve usar EXATAMENTE um dos CIDs acima "
            f"na cláusula LIKE (ex.: LIKE '{exemplo}%', categoria de 3 caracteres, "
            "MAIÚSCULAS, sem ponto). NÃO use outros códigos para essa doença."
        )

    def _cid_map_prefixes_para(self, question: str) -> set[str]:
        """Retorna a FAMÍLIA de CIDs (3 chars) que o de-para indica p/ a pergunta.

        Usada pela validação semântica (Solução C): se a pergunta é sobre
        sarampo, o de-para devolve {'B05'} e qualquer CID fora dessa família
        (ex.: A67, B20) deve ser rejeitado — mesmo que exista nos dados.
        """
        try:
            from db.cid_map import get_cid_map
            cid_map = get_cid_map()
            # >>> CORREÇÃO 3 (precisão): usa apenas o TOPO do ranking (3) para
            # definir a família. Com max_resultados alto, termos genéricos da
            # pergunta (ex.: "diagnosticos", "muscular") puxavam famílias
            # irrelevantes (M62/R93/Y57) para a lista, tornando a validação
            # semântica permissiva. As entradas de MAIOR pontuação representam
            # a doença real (ex.: atrofia muscular espinal -> G120/G121/G129,
            # todos da família G12).
            matches = cid_map.identificar(question, max_resultados=3)
        except Exception as exc:  # pragma: no cover - import defensivo
            LOGGER.debug("falha ao resolver família CID do de-para: %s", exc)
            return set()
        familias: set[str] = set()
        for entry in matches:
            canon = entry.inicio.upper()
            familias.add(canon[:3] if len(canon) == 4 else canon)
        return {f for f in familias if len(f) == 3}

    def _fix_cid_filters_deterministic(
        self, sql: str, cids_invalidos: list[str], question: str, request_id: str
    ) -> str | None:
        """Reescreve DETERMINISTICAMENTE o(s) CID(s) errado(s) pelo do de-para.

        >>> SOLUÇÃO B: por que determinístico e não outro round-trip ao LLM?
        O log mostrou que ``_request_sql_fix`` devolvia a SQL INALTERADA no caso
        de CID (o prompt falava de "colunas inexistentes", então o LLM concluía
        que nada havia para corrigir). Como o de-para conhece a família correta
        (ex.: sarampo -> B05), podemos trocar o código na própria cláusula LIKE
        sem depender do LLM — o que garante que um código errado-mas-presente
        (B20/HIV, A67/pinta) NUNCA chegue ao DuckDB.

        Retorna a SQL corrigida, ou ``None`` se não houver de-para aplicável
        (nesse caso o chamador recorre ao LLM, agora com o prompt reformulado).
        """
        dep = self._cid_map_prefixes_para(question)
        if not dep:
            return None
        # Escolhe o código mais representativo (ordem estável: menor código).
        codigo_correto = sorted(dep)[0]
        corrigida = sql
        trocou = False
        for errado in cids_invalidos:
            # >>> CORREÇÃO 3: tolera o código errado com ou sem ponto decimal.
            # O 'errado' já vem normalizado sem ponto (ex.: 'G710'), mas a SQL
            # pode conter 'G71.0' (com ponto) ou 'G710'. Montamos as variantes
            # textuais e reescrevemos QUALQUER uma delas para a categoria correta
            # da família do de-para (ex.: 'G12'), garantindo que o filtro deixe
            # de selecionar a doença errada (distrofia G71 -> atrofia G12).
            variantes = {errado.upper()}
            if len(errado) == 4:  # 'G710' -> 'G71.0'
                variantes.add(errado[:3] + "." + errado[3])
            if len(errado) == 3:  # 'G71'
                variantes.add(errado)
            for variante in variantes:
                novo, n = re.subn(
                    r"(LIKE\s+(?:'|\"))" + re.escape(variante) + r"(%?(?:'|\"))",
                    lambda m: m.group(1) + codigo_correto + m.group(2),
                    corrigida,
                    flags=re.IGNORECASE,
                )
                if n:
                    trocou = True
                    corrigida = novo
        if not trocou:
            return None
        LOGGER.warning(
            "CID(s) %s reescrito(s) deterministicamente para '%s' (família do de-para). request_id=%s",
            cids_invalidos,
            codigo_correto,
            request_id,
        )
        return corrigida


    def _cid_map_prefixes(self, min_len: int = 2) -> set[str]:
        """Retorna os prefixos de CID conhecidos pelo de-para OFICIAL (CID_10.CSV).

        São os códigos que o sistema SABE mapear para alguma doença (via
        ``CidMap``/``CID_ALIASES``). Eles NUNCA devem ser considerados
        'inventados' pela validação de CID, mesmo que não apareçam na amostra
        de ``_known_cid_prefixes`` (ex.: B05/sarampo, A90/dengue têm baixa
        frequência e eram cortados do recorte).

        Cacheados em memória (a coleção é estável durante a execução).
        """
        cache_attr = "_cid_map_prefixes_cache"
        cached: set[str] | None = getattr(self, cache_attr, None)
        if cached is not None:
            return cached

        prefixos: set[str] = set()
        try:
            from db.cid_map import CID_ALIASES, get_cid_map
        except Exception as exc:  # pragma: no cover - import defensivo
            LOGGER.debug("cid_map indisponível p/ prefixos: %s", exc)
            setattr(self, cache_attr, prefixos)
            return prefixos

        # 1) Dos aliases coloquiais (garante as doenças mais comuns).
        for codigos in CID_ALIASES.values():
            for codigo in codigos:
                prefixos.add(codigo.upper()[:max(min_len, 2)])

        # 2) De todas as entradas do CSV, na granularidade de CATEGORIA (3 chars)
        #    — é o nível usado pelo filtro LIKE 'COD%'.
        try:
            cid_map = get_cid_map()
            if cid_map.loaded:
                for entry in cid_map.entries:
                    prefixos.add(entry.inicio.upper()[:3])
        except Exception as exc:
            LOGGER.debug("falha ao coletar prefixos do cid_map: %s", exc)

        prefixos = {p for p in prefixos if len(p) >= 2 and p[0].isalpha()}
        setattr(self, cache_attr, prefixos)
        LOGGER.info("cid_map prefixos conhecidos=%d", len(prefixos))
        return prefixos

    def _known_cid_prefixes(self, request_id: str, min_len: int = 3, limit: int = 500) -> list[str]:
        """
        Coleta os prefixos de CID (ex.: 'A90', 'C44') REALMENTE presentes nos dados.

        Isso permite "grounding" do LLM: em vez de inventar códigos a partir do
        conhecimento paramétrico (que produz códigos errados/inexistentes no
        DATASUS), passamos a lista de prefixos reais observados no cache. Também
        é usado para validar os filtros de CID gerados (ver _validate_cid_filters).

        A consulta é cacheada em memória por request_id para não repetir o scan
        várias vezes na mesma requisição. Retorna lista vazia em caso de falha
        (o chamador deve degradar sem grounding).
        """
        cache_attr = "_cid_prefixes_cache"
        cache: dict[str, list[str]] | None = getattr(self, cache_attr, None)
        if cache is None:
            cache = {}
            setattr(self, cache_attr, cache)
        if request_id in cache:
            return cache[request_id]

        prefixos: set[str] = set()

        # >>> CORREÇÃO (timing): _known_cid_prefixes é chamado DENTRO de
        # _generate_sql, ANTES de prepare_question_cache. Portanto a view
        # datasus_query_cache AINDA NÃO EXISTE neste ponto e consultá-la
        # sempre falharia (log: "cache filtrado da pergunta não foi preparado").
        # Em vez disso, lemos os CIDs diretamente das TABELAS DE GRUPO
        # (<grupo>.datasus_cache), que ficam anexadas à conexão principal desde
        # a inicialização. Em modo legado (sem grupos), usamos datasus_cache.
        relations: list[str] = []
        if getattr(self.cache, "groups", None):
            for name in self.cache.groups:
                safe = name.replace('"', '""')
                relations.append(f'"{safe}".datasus_cache')
        else:
            relations.append("datasus_cache")

        for relation in relations:
            try:
                # Descobre as colunas de diagnóstico REAIS dessa relação (evita
                # referenciar colunas inexistentes como CIDPRI num grupo sem ela).
                desc = self.cache.connection.execute(f"DESCRIBE {relation}").fetchall()
            except Exception:
                continue
            cols_upper = {str(row[0]).upper(): str(row[0]) for row in desc}
            diag_cols = [
                cols_upper[c.upper()]
                for c in DIAGNOSIS_COLUMN_NAMES
                if c.upper() in cols_upper
            ]
            if not diag_cols:
                diag_cols = [
                    real for up, real in cols_upper.items()
                    if "CID" in up or "DIAG" in up or "CAUSA" in up
                ]
            for col in diag_cols[:10]:
                esc = col.replace('"', '""')
                sql = (
                    f"SELECT DISTINCT SUBSTR(UPPER(TRIM(CAST(\"{esc}\" AS VARCHAR))), 1, {min_len}) AS p "
                    f"FROM {relation} "
                    f"WHERE \"{esc}\" IS NOT NULL AND TRIM(CAST(\"{esc}\" AS VARCHAR)) <> ''"
                )
                try:
                    for row in self.cache.connection.execute(sql).fetchmany(limit):
                        val = str(row[0] or "").upper()
                        # Um prefixo de CID válido começa com LETRA (ex.: 'A90', 'C44');
                        # descartamos ruído como '000'/'999' que não é código de doença.
                        if len(val) >= 2 and val[0].isalpha():
                            prefixos.add(val)
                except Exception:
                    continue
            if len(prefixos) >= limit:
                break

        # >>> CORREÇÃO: garante que os CIDs do de-para OFICIAL (CID_10.CSV) estejam
        # SEMPRE presentes na lista de prefixos 'conhecidos', mesmo que não tenham
        # entrado no recorte de ``limit`` (ex.: B05/sarampo e A90/dengue têm baixa
        # frequência e eram cortados pelo fetchmany(limit)). Sem isso, o grounding
        # de dados rejeitava justamente os códigos CORRETOS.
        for canon in self._cid_map_prefixes():
            prefixos.add(canon)

        # Guarda TODOS os prefixos para validação (não trunca), mas limita a
        # lista devolvida ao prompt para não inflá-lo.
        todos = sorted(prefixos)
        prefixes = todos[:max(limit, len(todos))] if len(todos) <= limit else todos
        cache[request_id] = prefixes
        LOGGER.info(
            "cid grounding request_id=%s prefixos_conhecidos=%d (com de-para)",
            request_id,
            len(prefixes),
        )
        return prefixes

    def _validate_cid_filters(
        self, sql: str, request_id: str, question: str | None = None
    ) -> list[str]:
        """
        Retorna os prefixos de CID usados no SQL que são INVÁLIDOS.

        Detecta filtros do tipo ``LIKE 'CODIGO%'`` e considera inválido um CID
        que (a) NÃO existe nos dados/de-para oficial, OU (b) — quando
        ``question`` é fornecida — está FORA da família de CIDs que o de-para
        semântico indica para a doença perguntada (Solução C).

        O critério (b) é o que pega o caso 'sarampo -> B20 (HIV)': B20 existe
        nos dados e passaria no critério (a), mas não pertence à família B05.
        """




        # >>> CORREÇÃO 3 (defesa em profundidade): extrai o código de QUALQUER
        # filtro LIKE sobre CID, normalizando o ponto decimal.
        # Ex.: LIKE 'G71.0%' -> 'G710' ; LIKE 'G71%' -> 'G71' ; LIKE 'G12.0%' -> 'G120'.
        # Sem normalizar, 'G71.0' (distrofia) e 'G710' seriam tratados como
        # códigos distintos, enfraquecendo a rejeição de CID errado.
        usados: set[str] = set()
        for _m in re.finditer(
            r"LIKE\s+'([A-Za-z]\d{2}(?:\.?\d)?)%?'", sql, flags=re.IGNORECASE
        ):
            _cod = _m.group(1).upper().replace(".", "")
            usados.add(_cod)
        if not usados:
            return []

        # >>> SOLUÇÃO C: validação SEMÂNTICA por família do de-para. <<<
        # Se a pergunta é sobre sarampo, o de-para devolve {'B05'}; qualquer CID
        # usado fora dessa família (A67, B20, ...) é rejeitado, mesmo presente.
        if question:
            dep = self._cid_map_prefixes_para(question)
            if dep:
                fora_depara = sorted(
                    c
                    for c in usados
                    if c[:3] not in dep
                    and not any(c.startswith(d) for d in dep)
                )
                if fora_depara:
                    LOGGER.warning(
                        "CID(s) fora da família do de-para %s: %s. request_id=%s",
                        sorted(dep),
                        fora_depara,
                        request_id,
                    )
                    return fora_depara

        # >>> CORREÇÃO: combinamos DUAS fontes de verdade:
        #  1) CIDs presentes nos DADOS (amostra do cache);
        #  2) CIDs conhecidos pelo DE-PARA OFICIAL (CID_10.CSV/CID_ALIASES).
        # Um código do de-para (ex.: B05/sarampo, A90/dengue) NUNCA é 'inventado',
        # mesmo que tenha ficado fora do recorte da amostra do cache.
        known = {p.upper() for p in self._known_cid_prefixes(request_id)}
        known |= self._cid_map_prefixes()
        if not known:
            # Sem grounding não é possível afirmar que o CID está errado.
            return []

        def _existe(codigo: str) -> bool:
            # Compara por FAMÍLIA de 3 chars: 'B050' -> 'B05'. Assim
            # LIKE 'B05%' e LIKE 'B050%' são equivalentes para a validação.
            familia = codigo[:3]
            return any(
                k == codigo
                or k[:3] == familia
                or codigo.startswith(k)
                or k.startswith(codigo)
                for k in known
            )

        invalidos = sorted(c for c in usados if not _existe(c))
        if invalidos:
            LOGGER.warning(
                "CID(s) fora dos dados e do de-para oficial: %s. request_id=%s",
                invalidos,
                request_id,
            )
        return invalidos

    def _expand_cid_filters_multi_column(
        self, sql: str, real_columns: list[str], request_id: str = ""
    ) -> str:
        """Expande um filtro CID de UMA coluna para TODAS as colunas de diagnostico.

        A view datasus_query_cache e o UNION ALL de varios grupos cujas colunas de
        CID tem NOMES DIFERENTES (sp.SP_CIDPRI, pa.PA_CIDPRI, rd.DIAG_PRINC,
        bi.CIDPRI, ...). Quando o LLM filtra por UMA unica coluna, os grupos que
        NAO tem essa coluna recebem NULL e o codigo se perde silenciosamente
        (ex.: dengue A90 so aparecia em grupos com SP_CIDPRI/PA_CIDPRI/DIAG_PRINC).

        Reconhece os dois formatos que o LLM costuma gerar:
          - CAST("CIDPRI" AS VARCHAR) LIKE A90% (com UPPER/TRIM opcionais)
          - "CIDPRI" LIKE A90%
        """
        if not sql or not real_columns:
            return sql

        canonical = {n.upper() for n in DIAGNOSIS_COLUMN_NAMES}
        sufixos = ("_CIDPRI", "_CIDSEC", "_CIDCAS", "_CID_C", "_CIDPRIM", "_CID10")

        def _is_diag(col: str) -> bool:
            up = col.upper()
            return up in canonical or any(up.endswith(s) for s in sufixos)

        diag_real = sorted({col for col in real_columns if _is_diag(col)})
        if not diag_real:
            diag_real = sorted(
                col for col in real_columns
                if "CID" in col.upper() or "DIAG" in col.upper() or "CAUSA" in col.upper()
            )
        if len(diag_real) < 2:
            return sql

        diag_by_upper = {c.upper(): c for c in diag_real}
        mudou = False

        def _build_or(codigo: str) -> str:
            termos = []
            for nome in diag_real:
                esc = nome.replace("\"", "\"\"")
                termos.append(
                    "UPPER(TRIM(CAST(\"" + esc + "\" AS VARCHAR))) LIKE '" + codigo + "%'"
                )
            return "(" + " OR ".join(termos) + ")"

        # Formato 1: [UPPER([TRIM(]]CAST("COL" AS VARCHAR)[)])] LIKE 'COD%'
        # (aceita aspas simples ou duplas em volta do literal do filtro)
        pat1 = re.compile(
            r"(?:UPPER\s*\(\s*)?(?:TRIM\s*\(\s*)?CAST\(\s*\"([A-Za-z0-9_]+)\"\s+AS\s+VARCHAR\s*\)\s*\)?\s*\)?\s*LIKE\s*"
            + r"['\"]" + r"([A-Za-z]\d{2}(?:\.?\d)?)%?" + r"['\"]",
            flags=re.IGNORECASE,
        )

        def _r1(m):
            nonlocal mudou
            col = m.group(1)
            if col.upper() not in diag_by_upper:
                return m.group(0)
            mudou = True
            return _build_or(m.group(2))

        novo = pat1.sub(_r1, sql)

        # Formato 2: "COL" LIKE 'COD%'
        # (aceita aspas simples ou duplas em volta do literal do filtro)
        pat2 = re.compile(
            r"\"([A-Za-z0-9_]+)\"\s+LIKE\s*"
            + r"['\"]" + r"([A-Za-z]\d{2}(?:\.?\d)?)%?" + r"['\"]",
            flags=re.IGNORECASE,
        )

        def _r2(m):
            nonlocal mudou
            col = m.group(1)
            if col.upper() not in diag_by_upper:
                return m.group(0)
            mudou = True
            return _build_or(m.group(2))

        novo = pat2.sub(_r2, novo)

        if mudou:
            LOGGER.warning(
                "filtro CID expandido para %d colunas de diagnostico (%s). request_id=%s",
                len(diag_real), ", ".join(diag_real[:8]), request_id,
            )
        return novo
