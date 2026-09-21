"""De-para de doenças -> CID-10 a partir do arquivo ``CID_10.CSV``.

O arquivo ``CID_10.CSV`` fica na RAIZ do projeto e contém as SUBCATEGORIAS
oficiais da CID-10 no formato REAL (separador ``;``)::

    SUBCAT;CLASSIF;RESTRSEXO;CAUSAOBITO;DESCRICAO;DESCRABREV;REFER;EXCLUIDOS;
    A000;;;;Cólera devida a Vibrio cholerae 01, biótipo cholerae;A00.0 Colera ...;;;
    A90;;;;Dengue [dengue clássico];A90   Dengue;;;
    A150;;;;Tuberculose pulmonar, com confirmação por exame ...;A15.0 Tuberc ...;;;

Cada linha é UMA subcategoria (``SUBCAT``), em dois formatos:
  - 3 caracteres (ex.: ``A90``)   -> categoria       (equivale a ``A90``);
  - 4 caracteres (ex.: ``A150``)  -> subcategoria    (equivale a ``A15.0``),
    isto é, o 4º dígito é a casa decimal SEM o ponto.

IMPORTANTE: os dados DATASUS armazenam o CID exatamente no formato ``SUBCAT``
(sem ponto), com 3 ou 4 caracteres (ex.: ``A90``, ``F102``, ``C161``). Portanto
o de-para e os filtros SQL DEVEM usar esse mesmo formato, com filtro no
próprio código: ``LIKE 'A90%'`` para uma categoria ou ``LIKE 'A150%'`` para
uma subcategoria. NÃO usar ``LIKE 'A15%'`` quando se quer a subcategoria.

O objetivo deste módulo é permitir que o sistema identifique o CÓDIGO CID
correto de uma doença ANTES de montar a consulta ao DATASUS, evitando que o
LLM "invente" códigos a partir do conhecimento paramétrico.

Pipeline de uso (ver ``api/llm_service.py``):

    1. ``get_cid_map().identificar(doença)`` -> devolve candidatos CID-10
       (ex.: "dengue" -> ["A90", "A91"]; "tuberculose" -> ["A150", ...]).
    2. Os códigos encontrados são injetados no prompt como "de-para" e o LLM
       só pode usar esses códigos.
    3. Só então a busca pela doença é feita no cache DATASUS.

O módulo é resiliente: se o CSV estiver ausente/corrompido, degrada para os
``CID_ALIASES`` (subconjunto coloquial) e o LLM volta a operar com o grounding
baseado nos dados (``_known_cid_prefixes``).
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import PROJECT_DIR

LOGGER = logging.getLogger("datasus_llm")

# Caminho padrão do arquivo CID-10 na raiz do projeto (fallback de referência).
CID_CSV_PATH = PROJECT_DIR / "CID_10.CSV"

# >>> NOVO: conexão DuckDB registrada pelo DatasusCache. A tabela ``cid10``
# (carregada UMA única vez no DuckDB a partir do CID_10.CSV) é a fonte de
# verdade do de-para. O Python NÃO lê mais o CSV diretamente.
_CID_CONNECTION: Any = None
_CID_CONNECTION_LOCK = threading.Lock()


def set_cid_connection(connection: Any) -> None:
    """Registra a conexão DuckDB usada para consultar a tabela ``cid10``.

    Chamado uma única vez pelo ``DatasusCache.__init__``. A partir daí, todo
    carregamento do `CidMap` consulta a tabela ``cid10`` no DuckDB.
    """
    global _CID_CONNECTION
    with _CID_CONNECTION_LOCK:
        _CID_CONNECTION = connection
    LOGGER.info("cid_map: conexão DuckDB registrada (tabela cid10)")


def get_cid_connection() -> Any:
    """Retorna a conexão DuckDB registrada (ou None)."""
    return _CID_CONNECTION

# Sinônimos/aliases de doenças comuns que NÃO aparecem literalmente na
# descrição oficial da categoria, mas são frequentemente usados nas perguntas.
# O mapa associa o termo coloquial ao(s) prefixo(s) de CID-10 esperados.
# IMPORTANTE: mantenha os códigos alinhados à CID-10 oficial.
CID_ALIASES: dict[str, tuple[str, ...]] = {
    "dengue": ("A90", "A91"),
    "dengue grave": ("A91",),
    "zika": ("A92",),
    "chikungunya": ("A92",),
    "febre amarela": ("A95",),
    "tuberculose": ("A15", "A16", "A17", "A18", "A19"),
    "hanseniase": ("A30",),
    "meningite": ("A39", "G00", "G01", "G02", "G03"),
    "aids": ("B20", "B21", "B22", "B23", "B24"),
    "hiv": ("B20", "B21", "B22", "B23", "B24"),
    "hepatite": ("B15", "B16", "B17", "B18", "B19"),
    "hepatite a": ("B15",),
    "hepatite b": ("B16",),
    "hepatite c": ("B17",),
    "covid": ("U07",),
    "covid-19": ("U07",),
    "sarampo": ("B05",),
    "rubéola": ("B06",),
    "rubeola": ("B06",),
    "varicela": ("B01",),
    "caxumba": ("B26",),
    "coqueluche": ("A37",),
    "tétano": ("A33", "A34", "A35"),
    "tetano": ("A33", "A34", "A35"),
    "raiva": ("A82",),
    "leishmaniose": ("B55",),
    "malaria": ("B50", "B51", "B52", "B53", "B54"),
    "malária": ("B50", "B51", "B52", "B53", "B54"),
    "chagas": ("B57",),
    "esquistossomose": ("B65",),
    "diabetes": ("E10", "E11", "E12", "E13", "E14"),
    "desnutricao": ("E40", "E41", "E42", "E43", "E44", "E45", "E46"),
    "desnutrição": ("E40", "E41", "E42", "E43", "E44", "E45", "E46"),
    "infarto": ("I21", "I22", "I23"),
    "avc": ("I60", "I61", "I62", "I63", "I64"),
    "derrame": ("I60", "I61", "I62", "I63", "I64"),
    "hipertensao": ("I10", "I11", "I12", "I13", "I15"),
    "hipertensão": ("I10", "I11", "I12", "I13", "I15"),
    "pneumonia": ("J12", "J13", "J14", "J15", "J16", "J17", "J18"),
    "asma": ("J45", "J46"),
    "bronquite": ("J20", "J21", "J40", "J41", "J42"),
    "gripe": ("J09", "J10", "J11"),
    "influenza": ("J09", "J10", "J11"),
    "diarreia": ("A00", "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09"),
    "diarréia": ("A00", "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09"),
    "cancer": ("C00", "C97"),
    "câncer": ("C00", "C97"),
    "neoplasia": ("C00", "C97"),
    "cancer de mama": ("C50",),
    "câncer de mama": ("C50",),
    "cancer de pulmao": ("C33", "C34"),
    "câncer de pulmão": ("C33", "C34"),
    "cancer de prostata": ("C61",),
    "câncer de próstata": ("C61",),
    "cancer de colo do utero": ("C53",),
    "câncer de colo do útero": ("C53",),
    "cancer de pele": ("C43", "C44"),
    "câncer de pele": ("C43", "C44"),
    "cancer de estomago": ("C16",),
    "câncer de estômago": ("C16",),
    "cancer de colon": ("C18",),
    "câncer de cólon": ("C18",),
    "leucemia": ("C91", "C92", "C93", "C94", "C95"),
    "anemia": ("D50", "D51", "D52", "D53", "D55", "D56", "D57", "D58", "D59", "D60", "D61", "D62", "D63", "D64"),
    "anemia falciforme": ("D57",),
    "depressao": ("F32", "F33"),
    "depressão": ("F32", "F33"),
    "alzheimer": ("G30",),
    "epilepsia": ("G40", "G41"),
    "parkinson": ("G20",),
    "cegueira": ("H54",),
    "otite": ("H65", "H66", "H67"),
    "obesidade": ("E65", "E66", "E67", "E68"),
}

# Termos genéricos/ruído que NÃO devem ser usados como palavra-chave de busca.
_STOPWORDS: frozenset[str] = frozenset({
    "casos", "caso", "quantos", "quantas", "qual", "quais", "total", "numero",
    "número", "registros", "registro", "por", "de", "da", "do", "das", "dos",
    "em", "no", "na", "nos", "nas", "com", "sem", "para", "sobre", "ano",
    "anos", "mes", "mês", "meses", "sexo", "idade", "municipio", "município",
    "municipios", "municípios", "cidade", "cidades", "estado", "estados",
    "uf", "ufs", "periodo", "período", "datasus", "sus", "dados", "doenca",
    "doença", "doencas", "doenças", "saude", "saúde", "internacao",
    "internação", "internacoes", "internações", "atendimento", "atendimentos",
    "obitos", "óbitos", "obito", "óbito", "morte", "mortes", "taxa", "taxas",
    "evolucao", "evolução", "tendencia", "tendência", "distribuicao",
    "distribuição", "evolucao", "grafico", "gráfico", "top", "mais", "menos",
    "cid", "cid10", "codigo", "código",
})


def _normalize(text: str) -> str:
    """Normaliza texto: sem acentos, minúsculo, espaços colapsados."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class CidEntry:
    """Uma subcategoria/categoria da CID-10 (código PONTUAL).

    ``inicio`` = ``fim`` = o código exatamente como aparece em ``SUBCAT``
    (3 ou 4 caracteres, sem ponto): ex.: "A90" (categoria), "A150"
    (subcategoria, exibida como "A15.0").
    """
    inicio: str          # ex.: "A90" ou "A150"
    fim: str             # igual a inicio (código pontual)
    descricao: str       # ex.: "Sarampo complicado por encefalite"
    descricao_abrev: str
    termos: tuple[str, ...]  # termos normalizados para busca

    def prefixos(self) -> tuple[str, ...]:
        """Retorna o prefixo de CID utilizável no filtro LIKE.

        Como ``SUBCAT`` já é o código no formato armazenado nos dados, o
        filtro é simplesmente ``LIKE '<codigo>%'``:
          - categoria (3 chars, ex.: "A90")  -> LIKE 'A90%';
          - subcategoria (4 chars, ex.: "A150") -> LIKE 'A150%'.
        """
        return (self.inicio,)


class CidMap:
    """Carrega ``CID_10.CSV`` e resolve nomes de doenças -> códigos CID-10."""

    def __init__(self, csv_path: Path = CID_CSV_PATH) -> None:
        # ``csv_path`` mantido por compatibilidade; a FONTE de dados agora é a
        # tabela ``cid10`` no DuckDB (carregada a partir deste CSV uma única vez).
        self.csv_path = Path(csv_path)
        self._entries: list[CidEntry] = []
        self._by_norm_desc: dict[str, CidEntry] = {}
        self._loaded = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Carregamento
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._entries = self._read_duckdb()
            self._by_norm_desc = {}
            for entry in self._entries:
                for termo in entry.termos:
                    # Mantém a primeira ocorrência (arquivos CID listam a
                    # categoria mais específica antes das genéricas).
                    self._by_norm_desc.setdefault(termo, entry)
            self._loaded = True
            LOGGER.info(
                "cid_map carregado fonte=duckdb.cid10 entradas=%d termos=%d",
                len(self._entries),
                len(self._by_norm_desc),
            )

    def _read_duckdb(self) -> list[CidEntry]:
        """Carrega o de-para consultando a tabela ``cid10`` do DuckDB.

        A tabela ``cid10`` é carregada UMA única vez pelo ``DatasusCache`` a
        partir do ``CID_10.CSV``. Aqui apenas a LEITURA é feita via SQL, sem
        o Python reler o CSV.

        Se a conexão não estiver disponível ou a tabela não existir, degrada
        para lista vazia (o ``codigos_para`` ainda complementa com aliases).
        """
        conn = get_cid_connection()
        if conn is None:
            LOGGER.warning(
                "cid_map: conexão DuckDB não registrada; tabela cid10 indisponível."
            )
            return []
        try:
            cursor = conn.execute(
                "SELECT subcat, descricao, descabrev FROM cid10 "
                "WHERE subcat IS NOT NULL AND descricao IS NOT NULL"
            )
            rows = cursor.fetchall()
        except Exception as exc:
            LOGGER.warning("cid_map: falha ao consultar a tabela cid10: %s", exc)
            return []
        return self._parse_rows(rows)

    @staticmethod
    def _parse_rows(rows: list[tuple]) -> list[CidEntry]:
        """Monta as entradas a partir das linhas ``(subcat, descricao, descabrev)``
        retornadas pela tabela ``cid10`` do DuckDB.

        Cada ``SUBCAT`` é um código PONTUAL (categoria de 3 chars ou
        subcategoria de 4 chars), então ``inicio == fim``. O desempate por
        especificidade (3 vs 4 chars) é feito no ranking de ``identificar``.
        """
        entries: list[CidEntry] = []
        vistos: set[str] = set()
        for row in rows:
            if not row:
                continue
            subcat = (row[0] or "").strip().upper() if len(row) > 0 else ""
            descricao = (row[1] or "").strip() if len(row) > 1 else ""
            if not subcat or not descricao:
                continue
            # Aceita apenas LETRA + 2/3 dígitos (ex.: A90, A150, F102, C161).
            if not re.fullmatch(r"[A-Z]\d{2,3}", subcat):
                continue
            if subcat in vistos:
                continue
            vistos.add(subcat)

            descricao_abrev = (row[2] or "").strip() if len(row) > 2 else ""
            # DESCRABREV costuma começar com o código pontuado (ex.: "A00.0 Colera...").
            # Remove esse prefixo para não poluir os termos de busca.
            descricao_abrev = re.sub(r"^[A-Z]\d{2}(?:\.\d)?\s+", "", descricao_abrev).strip()
            if not descricao_abrev:
                descricao_abrev = descricao

            termos = CidMap._build_terms(descricao, descricao_abrev)
            if not termos:
                continue
            entries.append(
                CidEntry(
                    inicio=subcat,
                    fim=subcat,
                    descricao=descricao,
                    descricao_abrev=descricao_abrev,
                    termos=termos,
                )
            )
        return entries

    @staticmethod
    def _build_terms(descricao: str, descricao_abrev: str) -> tuple[str, ...]:
        """Gera termos normalizados pesquisáveis a partir das descrições."""
        termos: set[str] = set()
        for fonte in (descricao, descricao_abrev):
            norm = _normalize(fonte)
            if not norm:
                continue
            termos.add(norm)
            # Termos por segmento (quebra por vírgula, parênteses, barras).
            for parte in re.split(r"[,/()\[\]]", norm):
                parte = parte.strip()
                if len(parte) >= 4 and parte not in _STOPWORDS:
                    termos.add(parte)
        return tuple(sorted(termos))

    # ------------------------------------------------------------------
    # Consulta pública
    # ------------------------------------------------------------------
    def identificar(self, pergunta: str, max_resultados: int = 5) -> list[CidEntry]:
        """Identifica categorias CID-10 candidatas para a pergunta/doença.

        Estratégia (em ordem de prioridade):
          1. Código CID explícito na pergunta (ex.: "A90", "C50.9").
          2. Aliases conhecidos (ex.: "dengue" -> A90/A91).
          3. Casamento de termos da descrição oficial com os termos da pergunta.

        As correspondências são PONTUADAS para priorizar as mais ESPECÍFICAS.
        Isso evita que um alias genérico (ex.: "cancer" -> C00/C97) ofusque um
        alias específico (ex.: "cancer de mama" -> C50): o alias mais longo
        recebe pontuação maior, e a categoria mais específica (menor intervalo)
        desempata a favor.
        """
        self._load()
        if not self._entries:
            return []

        pergunta_norm = _normalize(pergunta)
        if not pergunta_norm:
            return []


        # Pontuação ACUMULADA: quanto MAIOR, mais relevante/específica.
        # >>> CORREÇÃO 2 (bug "atrofia muscular espinal" -> ruído): antes
        # guardávamos apenas a MAIOR pontuação por entrada. Isso fazia um único
        # termo genérico da pergunta (ex.: 'diagnosticos' casando com
        # "Achados anormais de exames diagnósticos...") superar a doença, pois
        # os termos da doença (atrofia/muscular/espinal) casavam com entradas
        # DIFERENTES e não somavam. Agora SOMAMOS os pontos por entrada: uma
        # entrada que casa por VÁRIOS termos da doença (evidência forte) sobe
        # acima de qualquer casamento isolado acidental.
        pontuados: dict[str, tuple[int, CidEntry]] = {}

        def _score(entry: CidEntry, pontos: int) -> None:
            atual = pontuados.get(entry.inicio)

            if atual is None:
                pontuados[entry.inicio] = (pontos, entry)
            else:
                pontuados[entry.inicio] = (atual[0] + pontos, entry)

        # 1) Código CID explícito na pergunta (prioridade máxima).
        # Aceita tanto 'A90' quanto o formato pontuado 'A15.0' -> 'A150'.
        for match in re.finditer(r"\b([A-Z]\d{2})(?:\.(\d))?\b", pergunta.upper()):
            canon = match.group(1) + (match.group(2) or "")
            for entry in self._entries:
                if entry.inicio == canon:
                    _score(entry, 10_000)
                    break

        # 2) Aliases coloquiais -- o alias mais LONGO tem prioridade
        # (ex.: "cancer de mama" vence "cancer").
        for alias, codigos in CID_ALIASES.items():
            alias_norm = _normalize(alias)
            if not alias_norm or alias_norm not in pergunta_norm:
                continue
            base = 5_000 + len(alias_norm)
            for codigo in codigos:
                canon = self._canonical_code(codigo)
                for entry in self._entries:
                    # Correspondência exata (ex.: B05 -> B05) OU o alias é uma
                    # CATEGORIA de 3 chars (ex.: C50) e a entrada é uma
                    # SUBCATEGORIA dela (ex.: C500..C509). Nesse caso a
                    # subcategoria herda o score do alias — é o CID real que
                    # aparece nos dados DATASUS.
                    if entry.inicio == canon:
                        _score(entry, base)
                    elif len(canon) == 3 and len(entry.inicio) == 4 and entry.inicio.startswith(canon):
                        _score(entry, base)

        # 3) Casamento de termos da descrição (com tolerância a plural/concordância)
        termos_pergunta = [
            t for t in pergunta_norm.split()
            if len(t) >= 4 and t not in _STOPWORDS
        ]

        def _variantes(termo: str) -> set[str]:
            """Gera variantes simples (singular/plural) de um termo.

            Motivo (bug "atrofia muscular espinal" -> G71/distrofia):
            a pergunta traz "atrofia" (singular) e "espinal", mas as descrições
            oficiais usam "atrofias" (plural) e "espinais". Sem tolerância de
            concordância, o casamento por termo exato falhava e o de-para
            retornava VAZIO — levando o LLM a "inventar" o CID errado
            (G71.0/distrofia em vez de G12).
            """
            v: set[str] = {termo}
            if termo.endswith("ais"):                     # espinais -> espinal
                v.add(termo[:-2] + "l")
            if termo.endswith("is"):                      # general is
                v.add(termo[:-2] + "l")
            if termo.endswith("as") and len(termo) > 4:   # atrofias -> atrofia
                v.add(termo[:-1])
            if termo.endswith("os") and len(termo) > 4:   # nervosos -> nervoso
                v.add(termo[:-1])
            if termo.endswith("es") and len(termo) > 4:   # + es -> base
                v.add(termo[:-2])
            if termo.endswith("s") and len(termo) > 4:    # musculares -> musculare
                v.add(termo[:-1])
            return {x for x in v if len(x) >= 4}

        for termo in termos_pergunta:
            for variante in _variantes(termo):
                # (a) termo (=variante) exato no índice de descrições.
                entry_exato = self._by_norm_desc.get(variante)
                if entry_exato is not None:
                    _score(entry_exato, 1_000 + len(variante))
                # (b) o termo da pergunta é SUBSTRING de alguma descrição
                # oficial (ex.: "atrofia" em "atrofia muscular espinal nao
                # especificada", "espinal" em "...espinais hereditarias").
                # Antes só se testava o inverso (descrição dentro do termo),
                # o que nunca casava nesse caso.
                for desc_norm, entry_sub in self._by_norm_desc.items():
                    if len(variante) >= 5 and variante in desc_norm:
                        _score(entry_sub, 800 + len(variante))

        # Ordena por pontuação (desc) e desempata pelo código MAIS específico.
        # Como ``inicio == fim`` (código pontual), a especificidade é dada pelo
        # comprimento: subcategoria de 4 chars (ex.: A150) vence a categoria de
        # 3 chars (ex.: A15).
        def _especificidade(entry: CidEntry) -> int:
            return len(entry.inicio)

        ordenados = sorted(
            pontuados.values(),
            key=lambda item: (item[0], _especificidade(item[1])),
            reverse=True,
        )
        return [entry for _, entry in ordenados[:max_resultados]]

    def codigos_para(self, pergunta: str, max_resultados: int = 5) -> list[str]:
        """Atalho que devolve apenas os prefixos de CID (ex.: ['A90', 'A91']).

        Inclui tanto os códigos derivados do ``CID_10.CSV`` quanto os códigos
        de ALIASES conhecidos -- MESMO quando a categoria não está presente no
        CSV (ex.: ``U07`` para COVID-19). A ordem preserva a prioridade de
        especificidade devolvida por :meth:`identificar`.
        """
        codigos: list[str] = []
        vistos: set[str] = set()

        for entry in self.identificar(pergunta, max_resultados=max_resultados):
            for prefixo in entry.prefixos():
                if prefixo not in vistos:
                    vistos.add(prefixo)
                    codigos.append(prefixo)

        # Complementa com códigos de aliases que podem NÃO estar no CSV
        # (ex.: U07 = COVID-19). Aliases mais longos primeiro (mais específicos).
        pergunta_norm = _normalize(pergunta)
        aliases_match = sorted(
            (
                (alias, codigos_alias)
                for alias, codigos_alias in CID_ALIASES.items()
                if _normalize(alias) and _normalize(alias) in pergunta_norm
            ),
            key=lambda item: len(_normalize(item[0])),
            reverse=True,
        )
        for _, codigos_alias in aliases_match:
            for codigo in codigos_alias:
                if codigo not in vistos:
                    vistos.add(codigo)
                    codigos.append(codigo)

        return codigos

    @staticmethod
    def _canonical_code(codigo: str) -> str:
        """Normaliza um CID para o formato ``SUBCAT`` (sem ponto).

        Aceita entrada com ou sem ponto decimal (ex.: "A15.0" -> "A150",
        "A90" -> "A90") e devolve o código em maiúsculas.
        """
        codigo = (codigo or "").strip().upper()
        codigo = codigo.replace(".", "")
        return codigo

    def descricao_de(self, codigo: str) -> str | None:
        """Retorna a descrição oficial de um código CID-10.

        Aceita tanto o formato ``SUBCAT`` (ex.: "A150") quanto o pontuado
        (ex.: "A15.0"). Se o código exato não existir, tenta o pai de 3 chars
        (ex.: "A15"). Retorna ``None`` se o código não estiver na tabela.
        """
        self._load()
        canon = self._canonical_code(codigo)
        if not canon:
            return None
        # 1) Código exato (ex.: "A150", "A90").
        for entry in self._entries:
            if entry.inicio == canon:
                return entry.descricao
        # 2) Subcategoria (4 chars) -> categoria pai de 3 chars (ex.: A150 -> A15).
        if len(canon) == 4:
            pai = canon[:3]
            for entry in self._entries:
                if entry.inicio == pai:
                    return entry.descricao
        # 3) Categoria de 3 chars que só existe como filhos (ex.: B05 existe
        #    apenas como B050..B059): devolve a descrição do PRIMEIRO filho,
        #    que representa a categoria.
        if len(canon) == 3:
            for entry in sorted(self._entries, key=lambda e: e.inicio):
                if len(entry.inicio) == 4 and entry.inicio.startswith(canon):
                    return entry.descricao
        return None

    @property
    def entries(self) -> list[CidEntry]:
        self._load()
        return list(self._entries)

    @property
    def loaded(self) -> bool:
        self._load()
        return bool(self._entries)


# Instância única (lazy) reutilizada pela aplicação.
_CID_MAP_SINGLETON: CidMap | None = None
_SINGLETON_LOCK = threading.Lock()


def get_cid_map() -> CidMap:
    """Retorna a instância única (lazy) do mapa CID-10."""
    global _CID_MAP_SINGLETON
    if _CID_MAP_SINGLETON is None:
        with _SINGLETON_LOCK:
            if _CID_MAP_SINGLETON is None:
                _CID_MAP_SINGLETON = CidMap()
    return _CID_MAP_SINGLETON
