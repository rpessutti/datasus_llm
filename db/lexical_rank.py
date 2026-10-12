"""P0-B: priorização lexical determinística de grupos e colunas para o prompt.

Objetivo: em vez de o ``generate.py`` cortar grupos pela ORDEM ALFABÉTICA
(``sorted(grupos)``) e colunas pelas PRIMEIRAS 25 (``cols[:25]``), ordenar por
RELEVÂNCIA LEXICAL à pergunta. Isso torna o prompt **enxuto e informativo**:
as colunas/grupos mais prováveis entram primeiro e sobrevivem ao corte.

Princípios (do plano ``PLANO_MELHORIA_PROMPT_CID.md``):
  * SEM dependências novas (nenhum ONNX/embedding): só casamento lexical;
  * DETERMINÍSTICO e auditável (mesma pergunta -> mesma ordem);
  * CONSERVADOR: NÃO remove grupos que hoje entrariam por período — apenas
    REORDENA (o corte continua sendo ``MAX_GRUPOS_LABELED``/``MAX_COLS_POR_GRUPO``);
  * degrada com elegância: sem informação, mantém a ordem original.

Reutiliza:
  * ``db.cid_map._normalize`` (normalização sem acento/caixa) e a ideia de
    variantes singular/plural;
  * ``db.column_dictionary.CONCEPT_TOKENS``/``CONCEPT_LABELS`` (conceitos de
    negócio -> tokens de nome de coluna).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

try:
    from db.cid_map import _normalize, _STOPWORDS
except Exception:  # pragma: no cover - degradação segura
    def _normalize(text: str) -> str:  # type: ignore[misc]
        return (text or "").casefold().strip()

    _STOPWORDS = frozenset()  # type: ignore[assignment]

try:
    from db.column_dictionary import CONCEPT_TOKENS
except Exception:  # pragma: no cover
    CONCEPT_TOKENS = {}  # type: ignore[assignment]


# Tokens genéricos que NÃO devem orientar o ranking (evitam "casar tudo").
# Complementa os STOPWORDS de cid_map com termos típicos de perguntas DATASUS.
_EXTRA_STOPWORDS: frozenset[str] = frozenset({
    "lista", "listar", "mostra", "mostre", "exiba", "exibir", "quero",
    "gostaria", "favor", "consulta", "consultar", "media", "média", "soma",
    "somar", "contar", "contagem", "quantidade", "ranking", "ranquear",
    "ordenar", "ordem", "agrupe", "agrupado", "agrupar", "agrupamento",
    "filtre", "filtrar", "comparar", "compare", "resultado", "resultados",
    "informacao", "informação", "informacoes", "informações", "dado", "dados",
    "tabela", "tabelas", "coluna", "colunas", "campo", "campos", "menor",
    "maior", "primeiro", "primeiros", "ultimo", "último", "ultimos", "últimos",
})


def _tokenize(question: str, min_len: int = 3) -> List[str]:
    """Tokens relevantes da pergunta (normalizados, sem stopwords).

    ``min_len`` é 3 (e não 4 como em ``cid_map``) porque nomes de colunas do
    DATASUS usam siglas curtas (ex.: ``UF``, ``CNES``, ``PESO``); termos muito
    curtos ainda ajudam a priorizar.
    """
    norm = _normalize(question)
    if not norm:
        return []
    stop = set(_STOPWORDS) | set(_EXTRA_STOPWORDS)
    out: List[str] = []
    for tok in norm.split():
        if len(tok) < min_len or tok in stop:
            continue
        out.append(tok)
    return out


def _raw_tokens(question: str, min_len: int = 3) -> List[str]:
    """Tokens da pergunta SEM filtro de stopwords (apenas tamanho mínimo).

    Usado no casamento de CONCEITOS: muitos nomes de conceito (``sexo``,
    ``municipio``, ``idade``, ``mes``, ``ano``) ESTÃO nos stopwords (para não
    virar palavra-chave de CID), mas continuam sendo sinais válidos para
    priorizar COLUNAS. Aqui não os removemos.
    """
    norm = _normalize(question)
    if not norm:
        return []
    return [tok for tok in norm.split() if len(tok) >= min_len]


def _variantes(termo: str) -> set[str]:
    """Variantes simples singular/plural (mesma lógica de ``cid_map``)."""
    v: set[str] = {termo}
    if termo.endswith("ais") and len(termo) > 4:   # espinais -> espinal
        v.add(termo[:-2] + "l")
    if termo.endswith("is") and len(termo) > 4:
        v.add(termo[:-2] + "l")
    if termo.endswith("as") and len(termo) > 4:    # atrofias -> atrofia
        v.add(termo[:-1])
    if termo.endswith("os") and len(termo) > 4:    # nervosos -> nervoso
        v.add(termo[:-1])
    if termo.endswith("es") and len(termo) > 4:
        v.add(termo[:-2])
    if termo.endswith("s") and len(termo) > 4:     # municipios -> municipio
        v.add(termo[:-1])
    return {x for x in v if len(x) >= 3}


def _tokens_and_variants(question: str) -> set[str]:
    """Conjunto achatado dos tokens da pergunta + suas variantes."""
    out: set[str] = set()
    for tok in _tokenize(question):
        out |= _variantes(tok)
    return out


def _text_score(key: str, variants: set[str], weight: int) -> int:
    """Pontua ``key`` (normalizada) contra as variantes da pergunta.

    Casamento forte (a variante É a chave ou segmento inteiro) vale mais que
    casamento por substring. Retorna 0 se não houver casamento.
    """
    if not key:
        return 0
    best = 0
    segments = [s for s in re.split(r"[^a-z0-9]+", key) if s]
    for var in variants:
        if len(var) < 3:
            continue
        if key == var:
            best = max(best, weight * 4)
        elif var in segments:
            best = max(best, weight * 3)
        elif key.startswith(var) or key.endswith(var):
            best = max(best, weight * 2)
        elif var in key:
            best = max(best, weight)
    return best


def _concept_tokens_for_question(question: str) -> Dict[str, set[str]]:
    """Mapeia conceitos (de ``CONCEPT_TOKENS``) aos tokens da pergunta.

    Ex.: pergunta "quantos por sexo" -> {"sexo": {"sexo"}}. É a "ponte" entre a
    linguagem da pergunta e os tokens de nome de coluna dos conceitos.

    Usa ``_raw_tokens`` (sem stopwords) porque nomes de conceito como ``sexo``/
    ``municipio``/``idade`` são stopwords de ``cid_map`` mas são sinais válidos
    para priorizar colunas.
    """
    variants: set[str] = set()
    for tok in _raw_tokens(question):
        variants |= _variantes(tok)
    matched: Dict[str, set[str]] = {}
    for concept, tokens in (CONCEPT_TOKENS or {}).items():
        concept_norm = _normalize(concept)
        concept_variants = _variantes(concept_norm)
        # A pergunta menciona o NOME do conceito (ex.: "sexo", "municipio")?
        if concept_variants & variants:
            matched[concept] = {t.upper() for t in tokens}
    return matched


@dataclass(frozen=True)
class _QuestionProfile:
    """Sinais da pergunta pré-calculados UMA vez (evita recomputar em loop).

    Antes, ``score_column`` recomputava ``_tokens_and_variants`` e
    ``_concept_tokens_for_question`` a CADA coluna — com ~16 grupos × ~65 colunas
    isso é ~1000 tokenizações redundantes por pergunta. O perfil elimina isso:
    os tokens/variantes/conceitos são calculados uma vez e reutilizados.
    """
    variants: frozenset  # tokens da pergunta + variantes (casamento por texto)
    concept_tokens: tuple  # tokens_upper dos conceitos casados (deduplicados)
    has_signal: bool


def _build_profile(question: str) -> _QuestionProfile:
    variants = _tokens_and_variants(question)
    concepts = _concept_tokens_for_question(question)
    pairs: dict[str, int] = {}
    for _concept, toks in concepts.items():
        for tok in toks:
            # Guarda o token; os pesos (exato/substring) são aplicados em
            # ``_column_score`` para manter o MESMO comportamento do score
            # original (exato = 15; substring = 8).
            pairs.setdefault(tok, 0)
    return _QuestionProfile(
        variants=frozenset(variants),
        concept_tokens=tuple(sorted(pairs.keys())),
        has_signal=bool(variants or concepts),
    )


def _column_score(column: str, comment: str, profile: _QuestionProfile) -> int:
    """Score de uma coluna usando o ``profile`` pré-calculado."""
    base = _text_score(_normalize(column), profile.variants, weight=10)
    if comment:
        base += _text_score(_normalize(comment), profile.variants, weight=4)
    up = column.upper()
    for tok in profile.concept_tokens:
        if up == tok:
            base += 15  # token de conceito igual ao nome da coluna
        elif tok in up:
            base += 8   # token de conceito contido no nome da coluna
    return base


def score_column(column: str, question: str, comment: str = "") -> int:
    """Score lexical de UMA coluna em relação à pergunta.

    Combina:
      * casamento dos tokens da pergunta no NOME da coluna (peso alto);
      * casamento no COMENTÁRIO da coluna (peso médio — P0-A já o carregou);
      * tokens de CONCEITO casados (peso médio-alto).
    """
    return _column_score(column, comment, _build_profile(question))


def _has_signal(question: str) -> bool:
    """True se a pergunta tem QUALQUER sinal para o ranking.

    Sinal = tokens de conteúdo (``_tokenize``) OU menção a um conceito conhecido
    (ex.: pergunta "casos por sexo" só tem stopwords, mas "sexo" é um conceito).
    Sem este cheque, perguntas puramente conceituais fariam o ranking desistir.
    """
    return bool(_tokens_and_variants(question) or _concept_tokens_for_question(question))


def rank_columns(
    question: str,
    columns: Sequence[str],
    comment_for_column=None,
) -> List[str]:
    """Ordena ``columns`` por relevância lexical à pergunta (estável).

    Colunas sem casamento mantêm a ORDEM ORIGINAL (``sorted`` estável), de modo
    que o ranking só "puxa para frente" o que casa — nunca bagunça o resto de
    forma imprevisível. ``comment_for_column`` é opcional (callable coluna->str).
    """
    if not columns:
        return []
    profile = _build_profile(question)
    if not profile.has_signal:
        return list(columns)

    commenter = comment_for_column if callable(comment_for_column) else (lambda _c: "")

    def _score(col: str) -> int:
        try:
            comment = commenter(col) or ""
        except Exception:
            comment = ""
        return _column_score(col, comment, profile)

    # ``sorted`` é estável: empates preservam a ordem original.
    return sorted(columns, key=lambda c: -_score(c))


def score_group(
    question: str,
    grupo: str,
    column_dict,
) -> int:
    """Score lexical de um GRUPO = melhor/alguma evidência das suas colunas.

    Usa o ``ColumnDictionary`` para obter as colunas do grupo e seus comentários.
    Considera:
      * o MELHOR score de coluna do grupo (evidência forte -> peso cheio);
      * um bônus pequeno pela SOMA (grupos com VÁRIAS colunas relevantes);
      * o nome do próprio grupo (ex.: pergunta cita "internação").
    """
    profile = _build_profile(question)
    try:
        cols = column_dict.columns_for_group(grupo)
    except Exception:
        cols = []
    if not cols:
        return _text_score(_normalize(grupo), profile.variants, weight=5)

    commenter = getattr(column_dict, "comment_for_column", None)
    scores = [
        _column_score(c, commenter(c) if callable(commenter) else "", profile)
        for c in cols
    ]
    if not scores:
        return 0
    melhor = max(scores)
    soma = sum(s for s in scores if s > 0)
    base = melhor * 2 + soma  # melhor evidência domina; soma desempata
    base += _text_score(_normalize(grupo), profile.variants, weight=5)
    return base


def rank_groups(
    question: str,
    grupos: Sequence[str],
    column_dict,
) -> List[str]:
    """Ordena ``grupos`` por relevância lexical (estável; NÃO remove nenhum).

    A seleção de QUAIS grupos entram continua vindo de
    ``_determine_relevant_grupos`` (período/arquivo). Aqui só REORDENAMOS para
    que, ao aplicar ``MAX_GRUPOS_LABELED``, os mais prováveis sobrevivam.
    """
    if not grupos:
        return []
    lista = list(grupos)
    if len(lista) <= 1:
        return lista
    # Perfil calculado UMA vez e reutilizado para TODOS os grupos (eficiência).
    profile = _build_profile(question)
    if not profile.has_signal:
        return lista

    commenter = getattr(column_dict, "comment_for_column", None)

    def _group_score(grupo: str) -> int:
        try:
            cols = column_dict.columns_for_group(grupo)
        except Exception:
            cols = []
        if not cols:
            return _text_score(_normalize(grupo), profile.variants, weight=5)
        scores = [
            _column_score(c, commenter(c) if callable(commenter) else "", profile)
            for c in cols
        ]
        if not scores:
            return 0
        melhor = max(scores)
        soma = sum(s for s in scores if s > 0)
        return melhor * 2 + soma + _text_score(_normalize(grupo), profile.variants, weight=5)

    scores = {g: _group_score(g) for g in lista}
    # Só reordena se HOUVE algum sinal (evita bagunçar com todos-zero).
    if not any(s > 0 for s in scores.values()):
        return lista
    # Estável: empates preservam a ordem original de ``_determine_relevant_grupos``.
    return sorted(lista, key=lambda g: -scores[g])
