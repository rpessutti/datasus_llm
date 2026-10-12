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
    # >>> CID EXPLÍCITO NA PERGUNTA (precedência absoluta) <<<
    # Regex estrita para detectar um código CID-10 informado diretamente pelo
    # usuário na pergunta. Regras:
    #   - letra A-Z seguida de 2 ou 3 dígitos (ex.: 'A90', 'A150');
    #   - opcionalmente um separador decimal '.' ou ',' e 1 dígito (ex.: 'A15.0');
    #   - lookbehind/lookahead impedem casar dentro de tokens maiores (ex.: 'B2B',
    #     'COVID19', 'ABC123'), evitando falsos positivos.
    # Decisão (C): os separadores de LISTA aceitos no enunciado são vírgula e
    # ponto e vírgula; a regex abaixo captura cada código individualmente.
    _EXPLICIT_CID_RE = re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z]\d{2,3})(?:[.,](\d))?(?![A-Za-z0-9])"
    )

    def _extract_explicit_cid(self, question: str) -> list[str]:
        """Extrai os CID(s)-10 informados EXPLICITAMENTE na pergunta.

        Retorna uma lista de códigos normalizados no formato SUBCAT (sem ponto,
        MAIÚSCULAS) e SEM duplicatas, preservando a ordem de aparição. Ex.:
          "casos de A90"                 -> ['A90']
          "A90, B05 em 2024"             -> ['A90', 'B05']
          "compare A15.0; A15,1"         -> ['A150', 'A151']
          "dengue em 2024"               -> []

        Aceita os separadores de lista vírgula e ponto e vírgula (Decisão C).
        É a base da regra de precedência: quando há CID explícito, a descoberta
        automática (LLM/de-para) é PULADA.
        """
        if not question:
            return []
        codigos: list[str] = []
        vistos: set[str] = set()
        for match in self._EXPLICIT_CID_RE.finditer(question):
            base = match.group(1).upper()
            dec = match.group(2) or ""
            canon = base + dec  # 'A15.0' -> 'A150' ; 'A90' -> 'A90'
            if canon in vistos:
                continue
            vistos.add(canon)
            codigos.append(canon)
        return codigos

    def _is_explicit_cid_question(self, question: str) -> bool:
        """True se a pergunta informa ao menos um CID-10 explicitamente."""
        return bool(self._extract_explicit_cid(question))

    def _explicit_cid_grounding_block(self, cid_exp: list[str], request_id: str) -> str:
        """Bloco do prompt para quando o USUÁRIO informou o(s) CID(s).

        Devolve uma instrução AUTORITATIVA para a LLM usar EXATAMENTE os códigos
        informados (Decisão A1), sem descobrir/substituir. Cada código é
        anotado com a descrição oficial (quando disponível) para dar contexto.
        """
        linhas: list[str] = []
        descricoes: dict[str, str | None] = {}
        try:
            from db.cid_map import get_cid_map
            cid_map = get_cid_map()
            for codigo in cid_exp:
                try:
                    descricoes[codigo] = cid_map.descricao_de(codigo)
                except Exception:
                    descricoes[codigo] = None
        except Exception:
            descricoes = {c: None for c in cid_exp}

        for codigo in cid_exp:
            desc = descricoes.get(codigo)
            if desc:
                desc = desc.strip()
                if len(desc) > 90:
                    desc = desc[:87] + "..."
                linhas.append(f"  - {codigo} = {desc}")
            else:
                linhas.append(f"  - {codigo}")

        lista = ", ".join(cid_exp)
        exemplo = cid_exp[0]
        LOGGER.info(
            "cid EXPLÍCITO na pergunta request_id=%s codigos=%s (grounding autoritativo)",
            request_id, ",".join(cid_exp),
        )
        return (
            "CID(S) FORNECIDO(S) PELO USUÁRIO NA PERGUNTA (USE EXATAMENTE ESTES CÓDIGOS):\n"
            + "\n".join(linhas)
            + f"\nA pergunta já especifica o(s) CID(s): {lista}. NÃO descubra, substitua "
            "ou acrescente outros códigos. Use TODOS e SOMENTE estes na(s) cláusula(s) "
            f"de filtro, no formato LIKE 'CODIGO%' (MAIÚSCULAS, SEM ponto). "
            f"Ex.: UPPER(TRIM(CAST(\"ColunaCID\" AS VARCHAR))) LIKE '{exemplo}%'"
            + (" (ou combine com OR para mais de um CID)." if len(cid_exp) > 1 else ".")
        )

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
        # >>> PRECEDÊNCIA (FASE 2.1): se a pergunta informa o CID explicitamente,
        # NÃO há o que "descobrir" — pulamos o round-trip ao LLM (economia de
        # latência e evita que a descoberta sobreponha o CID do usuário).
        cid_exp = self._extract_explicit_cid(question)
        if cid_exp:
            LOGGER.info(
                "cid discovery PULADO (CID explícito na pergunta=%s) request_id=%s",
                cid_exp, request_id,
            )
            return ""
        # Heurística barata: só vale a pena consultar o cid10 se a pergunta
        # menciona algum termo "de doença" (evita round-trip em perguntas de
        # agregação pura, ex.: 'total de atendimentos por mês').
        if not self._question_mentions_disease(question):
            LOGGER.info("cid discovery ignorado (sem termo de doença) request_id=%s", request_id)
            return ""

        # >>> P2 (2.1): DESCOBERTA DETERMINÍSTICA COMPLETA, sem LLM e sem
        # truncar. É a fonte PRIMÁRIA: resolve todos os CIDs da doença a partir
        # do de-para oficial + tabela cid10, com anti-falso-positivo.
        try:
            codes_full = self._discover_cid_full(question, request_id)
        except Exception as exc:  # best-effort
            LOGGER.warning("cid discovery FULL falhou (%s); usando via LLM. request_id=%s", exc, request_id)
            codes_full = []
        if codes_full:
            return self._cid_discovery_block(codes_full, request_id)

        # >>> FALLBACK (P2): a via "LLM monta o SELECT" só é usada quando a
        # descoberta determinística NÃO achou nada (ex.: doença fora dos aliases
        # e sem termos longos). Mantida, mas com pós-processamento que NÃO
        # trunca (retorna TODAS as linhas) e sem LIMIT artificial na instrução.
        LOGGER.info(
            "cid discovery determinística vazia; caindo na via LLM. request_id=%s",
            request_id,
        )
        discovery_prompt = (
            "Você é um especialista em SQL DuckDB. Existe uma tabela `cid10` no DuckDB com "
            "o seguinte schema: subcat VARCHAR (código CID-10 SEM ponto, ex.: 'A90', 'B019'), "
            "descricao VARCHAR (descrição oficial), descabrev VARCHAR (descrição curta).\n\n"
            "Escreva UMA consulta SQL DuckDB (SOMENTE o SQL, sem markdown) que liste os "
            "códigos CID-10 (coluna subcat) e suas descrições (coluna descricao) que "
            "correspondam à doença mencionada na pergunta do usuário. Use busca textual "
            "com ILIKE/upper na coluna descricao. Retorne TODAS as correspondências "
            "(NÃO use LIMIT): ordene por subcat.\n\n"
            f"Pergunta do usuário: {question}\n"
            "SQL:"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=600,
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

        # >>> P2: NÃO trunca (antes: rows[:50]). Coleta TODAS as linhas e
        # prioriza pelos presentes nos dados; o teto de sanidade é o mesmo.
        try:
            reais = {p.upper() for p in self._known_cid_prefixes(request_id)}
        except Exception:
            reais = set()

        def _presente(cod: str) -> bool:
            return any(cod[:3] == r[:3] or cod.startswith(r) or r.startswith(cod) for r in reais)

        codes: list[str] = []
        vistos: set[str] = set()
        for row in rows:
            subcat = str(row.get("subcat") or "").strip().upper()
            if not subcat or subcat in vistos:
                continue
            vistos.add(subcat)
            codes.append(subcat)
        codes.sort(key=lambda c: (0 if _presente(c) else 1, len(c), c))
        teto = int(getattr(self.settings, "cid_discovery_max", 200) or 200)
        if len(codes) > teto:
            LOGGER.warning(
                "cid discovery via LLM excedeu teto=%d (achados=%d); truncando. request_id=%s",
                teto, len(codes), request_id,
            )
            codes = codes[:teto]

        linhas: list[str] = []
        desc_por_cod: dict[str, str] = {}
        for row in rows:
            subcat = str(row.get("subcat") or "").strip().upper()
            descricao = str(row.get("descricao") or "").strip()
            if subcat and subcat not in desc_por_cod and descricao:
                desc_por_cod[subcat] = descricao
        for cod in codes[:60]:
            desc = desc_por_cod.get(cod, "")
            linhas.append(f"  - {cod} = {desc}" if desc else f"  - {cod}")
        if len(codes) > 60:
            linhas.append(f"  ... (+{len(codes) - 60} códigos da mesma doença)")
        if not linhas:
            return ""
        LOGGER.info(
            "cid discovery OK (via LLM) request_id=%s encontrados=%d sql=%s",
            request_id, len(codes), sql[:300],
        )
        cats = sorted({c[:3] for c in codes})
        exemplo = cats[0] if cats else codes[0]
        return (
            "CIDs DESCOBERTOS PELA PRÓPRIA LLM NA TABELA cid10 (USE ESTES CÓDIGOS):\n"
            + "\n".join(linhas)
            + "\nUse EXATAMENTE um destes códigos (coluna subcat, SEM ponto) na cláusula "
            "LIKE 'CODIGO%'. PREFIRA a categoria de 3 caracteres para cobrir as "
            f"subcategorias (ex.: LIKE '{exemplo}%')."
        )

    def _discover_cid_full(self, question: str, request_id: str) -> list[str]:
        """P2 (2.1/2.2): retorna TODOS os CIDs da doença, SEM truncar.

        Substitui a descoberta truncada (``LIMIT 50``/``rows[:50]``) por uma
        resolução DETERMINÍSTICA contra a tabela ``cid10`` (fonte de verdade):

          1. **Âncora por alias curado** (``CidMap.identificacao_de_alias``):
             doenças com alias (ex.: tuberculose -> A15–A19; câncer de pele ->
             C43/C44) definem as FAMÍLIAS corretas. Só entram códigos dessas
             famílias (anti-falso-positivo, regra 2.2-1).
          2. **Descoberta por termos** (``CidMap.entries_por_termos``): para
             doenças SEM alias, casa os termos específicos por SEGMENTO na
             descrição oficial, exigindo a INTERSEÇÃO (todos os termos) — evita
             tanto substring solta (2.2-2) quanto over-matching por união.
          3. **Priorização (não descarte)** pelos prefixos REAIS presentes nos
             dados (2.2-5) — uma doença válida pode ter 0 casos no recorte.
          4. **Teto de sanidade** ``settings.cid_discovery_max`` (default 200),
             nunca usado para cortar recall de doenças com muitas subcategorias.

        Retorna a lista de códigos (formato SUBCAT, sem ponto), ordenada por:
        (a) presentes nos dados primeiro; (b) categoria de 3 chars antes das
        subcategorias; (c) ordem alfabética. Lista vazia se não houver doença.
        """
        if not question:
            return []
        try:
            from db.cid_map import get_cid_map
            cid_map = get_cid_map()
        except Exception as exc:  # pragma: no cover - import defensivo
            LOGGER.debug("cid_map indisponível p/ descoberta completa: %s", exc)
            return []

        # 1) Âncora por alias curado (famílias completas), se houver.
        familias = cid_map.identificacao_de_alias(question)
        codes: set[str] = set()
        if familias:
            for entry in cid_map.familia_completa(familias):
                codes.add(entry.inicio.upper())
            # >>> ROBUSTEZ (tabela cid10 ausente): se o catálogo ``cid10`` não
            # estiver carregado (ex.: ambiente sem a tabela), ``familia_completa``
            # vem VAZIA. Ainda assim o alias curado JÁ SABE as famílias corretas
            # (ex.: tuberculose -> A15–A19). Caímos para essas CATEGORIAS de 3
            # chars, que cobrem as subcategorias via LIKE 'COD%'. Melhor a
            # família do alias que nada.
            if not codes:
                codes.update(f.upper()[:3] for f in familias if len(f) >= 3)
        # 2) Descoberta por termos (reforça/estende quando não há alias).
        if not familias:
            termos = cid_map.termos_doenca(question)
            for entry in cid_map.entries_por_termos(termos):
                codes.add(entry.inicio.upper())

        if not codes:
            return []

        # >>> P2 (2.1) — AGREGAÇÃO DE CATEGORIAS: para cada SUBCATEGORIA de 4
        # chars encontrada, ADICIONA a CATEGORIA-pai de 3 chars. Motivo: o filtro
        # SQL é LIKE 'COD%'; recomendar a categoria (ex.: 'B05') cobre TODAS as
        # subcategorias ('B050'..'B059'). Sem isto, uma doença cujo cid10 só tem
        # subcategorias (ex.: sarampo -> B050..B059, tuberculosis -> A150..)
        # ficaria sem a categoria que melhor a representa no LIKE.
        codes |= {c[:3] for c in codes if len(c) >= 4}

        # 3) Prioriza (NÃO descarta) pelos prefixos reais presentes nos dados.
        try:
            reais = {p.upper() for p in self._known_cid_prefixes(request_id)}
        except Exception:
            reais = set()

        def _presente(cod: str) -> bool:
            return any(cod[:3] == r[:3] or cod.startswith(r) or r.startswith(cod) for r in reais)

        ordenados = sorted(
            codes,
            key=lambda c: (
                0 if _presente(c) else 1,   # (a) presentes nos dados primeiro
                len(c),                      # (b) categoria (3) antes de subcat (4)
                c,                           # (c) alfabético (determinístico)
            ),
        )
        # 4) Teto de sanidade (não de recall).
        teto = int(getattr(self.settings, "cid_discovery_max", 200) or 200)
        if len(ordenados) > teto:
            LOGGER.warning(
                "cid discovery completo excedeu teto de sanidade=%d (achados=%d); "
                "truncando. request_id=%s",
                teto, len(ordenados), request_id,
            )
            ordenados = ordenados[:teto]
        LOGGER.info(
            "cid discovery FULL request_id=%s familias=%s codigos=%d",
            request_id, familias or "(por termos)", len(ordenados),
        )
        return ordenados

    def _cid_discovery_block(self, codes: list[str], request_id: str) -> str:
        """Bloco de prompt a partir da descoberta COMPLETA (P2, 2.3).

        Deixa EXPLÍCITO que todos os códigos são da MESMA doença e orienta a
        usar a CATEGORIA de 3 chars no ``LIKE`` para cobrir as subcategorias.
        Cada CATEGORIA é anotada com a descrição oficial (quando disponível),
        pois é o nível que o filtro ``LIKE 'COD%'`` cobre.
        """
        if not codes:
            return ""
        # Categorias distintas (3 chars) presentes — usadas como recomendação.
        cats = sorted({c[:3] for c in codes})
        # Descrição oficial das categorias (dá contexto à LLM: "B05 = Sarampo").
        desc_por_cat: dict[str, str] = {}
        try:
            from db.cid_map import get_cid_map
            cid_map = get_cid_map()
            for cat in cats:
                desc = cid_map.descricao_de(cat)
                if desc:
                    desc = desc.strip()
                    if len(desc) > 70:
                        desc = desc[:67] + "..."
                    desc_por_cat[cat] = desc
        except Exception:  # best-effort; sem descrição o bloco ainda é útil
            pass

        linhas: list[str] = []
        if desc_por_cat:
            for cat in cats:
                desc = desc_por_cat.get(cat, "")
                linhas.append(f"  - {cat}" + (f" = {desc}" if desc else ""))
        # Mostra até 60 códigos no prompt (o teto de sanidade é 200, mas o
        # prompt não precisa listar todos; as categorias já cobrem os filhos).
        if len(codes) <= 60:
            linhas.extend(f"  - {cod}" for cod in codes)
        else:
            linhas.extend(f"  - {cod}" for cod in codes[:60])
            linhas.append(f"  ... (+{len(codes) - 60} códigos da mesma doença)")
        exemplo = cats[0] if cats else codes[0]
        return (
            "CIDs DA DOENÇA CITADA (TODOS pertencem à MESMA doença; descubra "
            f"pela tabela cid10 — {len(codes)} código(s), categorias {', '.join(cats)}):\n"
            + "\n".join(linhas)
            + "\nUse QUALQUER UM destes códigos (todos são da doença perguntada). "
            "PREFIRA a CATEGORIA de 3 caracteres no LIKE (ex.: LIKE "
            f"'{exemplo}%') para cobrir também as subcategorias. NÃO use códigos "
            "fora desta lista para essa doença."
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
        # >>> PRECEDÊNCIA (FASE 2.2): CID explícito na pergunta tem prioridade
        # ABSOLUTA. Não usamos o de-para por doença; devolvemos um bloco
        # autoritativo ordenando o uso EXATO dos códigos informados.
        cid_exp = self._extract_explicit_cid(question)
        if cid_exp:
            return self._explicit_cid_grounding_block(cid_exp, request_id)

        try:
            from db.cid_map import get_cid_map
        except Exception as exc:  # pragma: no cover - import defensivo
            LOGGER.debug("cid_map indisponível para grounding: %s", exc)
            return ""

        try:
            cid_map = get_cid_map()
            # >>> P2 (2.3): usa a FAMÍLIA COMPLETA quando a âncora é um alias
            # curado (ex.: tuberculose -> A15–A19; câncer de pele -> C43/C44).
            # Antes, max_resultados=8 truncava e podia perder subcategorias
            # válidas da mesma doença.
            familias = cid_map.identificacao_de_alias(question)
            if familias:
                entries = cid_map.familia_completa(familias)
                # Ordena por código p/ determinismo; dedup por família (3 chars).
                entries = sorted(entries, key=lambda e: e.inicio)
                matches = entries
            else:
                matches = cid_map.identificar(question, max_resultados=8)
        except Exception as exc:  # nunca derruba a geração de SQL
            LOGGER.warning("falha ao resolver CID p/ pergunta (grounding): %s", exc)
            return ""

        if not matches:
            return ""

        linhas: list[str] = []
        vistos: set[str] = set()
        for entry in matches[:60]:
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
        # >>> PRECEDÊNCIA (FASE 2.3): se o usuário informou o CID, a "família
        # correta" passa a ser EXATAMENTE o conjunto de CIDs informados (regra
        # A1). Assim a validação não sobrescreve nem amplia o que foi pedido.
        cid_exp = self._extract_explicit_cid(question)
        if cid_exp:
            return set(cid_exp)
        try:
            from db.cid_map import get_cid_map
            cid_map = get_cid_map()
            # >>> P2 (2.3): quando a âncora é um ALIAS CURADO (ex.: tuberculose,
            # câncer de pele), retorna a FAMÍLIA INTEIRA do alias — não o top-3,
            # que truncava e enfraquecia a validação semântica. Para perguntas
            # AMBÍGUAS (sem alias), preserva o comportamento anterior (top-3),
            # que já era o mais preciso nesse caso.
            familias_alias = cid_map.identificacao_de_alias(question)
            if familias_alias:
                return {f.upper()[:3] for f in familias_alias if len(f) >= 3}
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
        # >>> PRECEDÊNCIA (FASE 3.2): quando o usuário informou o CID, NUNCA
        # reescrevemos de forma determinística — poderíamos colapsar a lista
        # informada em um único código (Decisão D). Retornamos None e deixamos o
        # chamador decidir (que, nesse cenário, também preserva o CID do usuário).
        if self._is_explicit_cid_question(question):
            LOGGER.info(
                "correção determinística de CID PULADA (CID explícito na pergunta) request_id=%s",
                request_id,
            )
            return None
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

        # >>> PARTE 6 (#7): LEITURA DA TABELA PRE-COMPUTADA cid_prefixes <<<
        # Antes, este metodo fazia DESCRIBE + SELECT DISTINCT SUBSTR por coluna
        # de diagnostico de CADA grupo A CADA REQUISICAO. Agora os prefixos sao
        # pre-computados na ingestao (tabela cid_prefixes) e aqui apenas LIDOS.
        # Fallback: se a tabela estiver vazia/ausente, seguimos com o scan atual
        # (compatibilidade e testes).
        try:
            pre = self.cache.get_cid_prefixes()
        except Exception:
            pre = []
        if pre:
            prefixos = {p.upper() for p in pre if len(p) >= 2 and p[0].isalpha()}
            for canon in self._cid_map_prefixes():
                prefixos.add(canon)
            todos = sorted(prefixos)
            cache[request_id] = todos
            LOGGER.info(
                "cid grounding request_id=%s prefixos_conhecidos=%d (cid_prefixes pre-computado)",
                request_id, len(todos),
            )
            return todos

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
        # >>> BUG (lista de CIDs em IN): o LLM tambem gera o filtro de CID como
        # ``... IN ('M05%', 'M06%', 'M10%')``. Sem extrair esses literais, a
        # validacao A1 nao enxergava os CIDs usados (retornava [] cedo) e a
        # clausula IN -- que faz igualdade EXATA contra strings com coringa e
        # logo retorna 0 linhas -- passava intacta. Extrai os codigos do IN
        # (removendo o '%' final) para a validacao/igualdade de conjuntos.
        for _m in re.finditer(
            r"IN\s*\(\s*['\"]([A-Za-z]\d{2}(?:\.?\d)?)%?['\"](?:\s*,\s*['\"]([A-Za-z]\d{2}(?:\.?\d)?)%?['\"])*\s*\)",
            sql,
            flags=re.IGNORECASE,
        ):
            for _lit in re.findall(r"['\"]([A-Za-z]\d{2}(?:\.?\d)?)%?['\"]", _m.group(0)):
                _cod = _lit.upper().replace(".", "")
                usados.add(_cod)
        if not usados:
            return []

        # >>> PRECEDÊNCIA A1 (FASE 3.1): se o usuário informou CID(s)
        # explicitamente, o SQL deve usar EXATAMENTE esses códigos — nem a mais,
        # nem a menos. Comparamos por igualdade de conjuntos (normalizados).
        if question:
            explicitos = set(self._extract_explicit_cid(question))
            if explicitos:
                faltando = explicitos - usados   # LLM omitiu algum informado
                a_mais = usados - explicitos      # LLM inventou/adicionou outro
                divergentes = sorted(faltando | a_mais)
                if divergentes:
                    LOGGER.warning(
                        "CID(s) divergentes do informado pelo usuário (A1). "
                        "esperado=%s usados=%s divergentes=%s request_id=%s",
                        sorted(explicitos), sorted(usados), divergentes, request_id,
                    )
                return divergentes

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

    def _normalize_cid_in_filters(self, sql: str, request_id: str = "") -> str:
        """Reescreve filtros CID no formato de lista ``IN ('M05%', 'M06%', ...)``.

        >>> BUG (lista de CIDs retornava 0 linhas) <<<
        Para perguntas com LISTA de CIDs (ex.: 'CID M05,M06,M10'), o LLM
        frequentemente gera uma clausula ``IN`` com literais terminados no
        caractere coringa::

            UPPER(TRIM(CAST("CIDPRI" AS VARCHAR))) IN ('M05%', 'M06%', 'M10%')

        Isso esta SEMANTICAMENTE ERRADO: o ``IN`` faz IGUALDADE EXATA contra as
        strings literais (incluindo o coringa no meio), e NENHUM codigo real e
        igual a essas strings -- logo, 0 linhas. O correto e uma disjuncao de
        ``LIKE``::

            (UPPER(...) LIKE 'M05%' OR UPPER(...) LIKE 'M06%' OR ...)

        Esta reescrita e DETERMINISTICA e IDEMPOTENTE:
          * so age quando TODOS os literais da lista parecem prefixos de CID
            (letra + 2-3 digitos, com ou sem coringa final, com ou sem ponto);
          * reescreve cada literal para ``LIKE 'COD%'`` (prefixo), preservando
            subcategorias que a igualdade curta perderia;
          * apos a reescrita, os LIKEs seguem para
            :meth:`_expand_cid_filters_multi_column`, que os replica para TODAS
            as colunas de diagnostico do escopo.
        """
        if not sql:
            return sql
        if not re.search(r"\bIN\s*\(", sql, flags=re.IGNORECASE):
            return sql

        # Literal de CID dentro do IN: aspas simples/duplas, coringa final
        # opcional e ponto decimal opcional. Ex.: 'M05%', "a90", 'G12.1%'.
        lit = r'[\'"]' + '([A-Za-z]' + r'\d{2}(?:\.?\d)?)' + r'%?[\'"]'
        # Expressao de coluna: UPPER(TRIM(CAST("COL" AS VARCHAR))) ou "COL".
        col_expr = (
            r'(?:(?:UPPER\s*\(\s*)?(?:TRIM\s*\(\s*)?CAST\(\s*"([A-Za-z0-9_]+)"\s+AS\s+VARCHAR\s*\)\s*\)?\s*\)?'
            r'|(?:"([A-Za-z0-9_]+)"))'
        )
        padrao = re.compile(
            col_expr + r'\s+(?:NOT\s+)?IN\s*\(\s*(' + lit + r'(?:\s*,\s*' + lit + r')*)\s*\)',
            flags=re.IGNORECASE,
        )

        mudou = False

        def _sub(m: "re.Match[str]") -> str:
            nonlocal mudou
            col = m.group(1) or m.group(2)
            lista = m.group(3)
            codigos = [c.upper().replace('.', '') for c in re.findall(lit, lista)]
            if not codigos:
                return m.group(0)
            termos = " OR ".join(
                'UPPER(TRIM(CAST("' + col + '" AS VARCHAR))) LIKE ' + "'" + cod + "%'"
                for cod in codigos
            )
            mudou = True
            return "(" + termos + ")"

        novo = padrao.sub(_sub, sql)
        if mudou:
            LOGGER.warning(
                "filtro CID IN(...) reescrito para disjuncao LIKE "
                "(o IN comparava literalmente strings com coringa, "
                "retornando 0 linhas). request_id=%s",
                request_id,
            )
        return novo


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
