"""Dicionário consolidado de colunas do DATASUS.

Consolida, em memória, os caches de colunas por grupo (``cache/colunas_cache/*.json``)
em uma estrutura única e indexada, evitando recomputar união/dedup de colunas a
cada requisição do ``LLMQueryService``.

Recursos principais:
- Cache em memória assinado por conjunto de grupos (LRU simples por thread).
- Índices: coluna->grupo, coluna->tipo, grupo->colunas, coluna->conceito.
- Rótulos de origem por grupo, permitindo ao LLM distinguir colunas homônimas
  de grupos diferentes (ex.: ``SEXO`` em ``rd`` vs ``SEXOPAC`` em ``ps``).
- Persistência opcional em DuckDB (tabela ``column_dictionary``).
- Dicionários de tradução código->rótulo do DATASUS (sexo, raça/cor, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # constante centralizada (best-effort; se ausente, usa fallback local)
    from config.constants import DIAGNOSIS_COLUMN_NAMES, DIAGNOSIS_COLUMN_SUFFIXES
except Exception:  # pragma: no cover
    DIAGNOSIS_COLUMN_NAMES = ("CID", "DIAG", "DIAGNOSTICO", "CAUSABAS", "CIDPRI")
    DIAGNOSIS_COLUMN_SUFFIXES = ("PRINC", "SECUN", "SEC")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conceitos -> tokens de nomes de coluna
# ---------------------------------------------------------------------------

# Mapeia um conceito de negócio para fragmentos que aparecem no nome de colunas
# reais do DATASUS. Usado apenas para montar guidance/roteamento para o LLM.
CONCEPT_TOKENS: Dict[str, Tuple[str, ...]] = {
    "sexo": ("SEXO", "SEXOPAC", "SP_SEXO", "CS_SEXO"),
    "idade": ("IDADE", "IDADEPAC", "NU_IDADE", "IDADEMAE", "CO_FAIXA"),
    "raca_cor": ("RACA", "RACACOR", "CS_RACA", "RACACORMAE", "COR"),
    "municipio": ("MUNIC", "MUNICIPIO", "CO_MUNICIP", "MUNPAC", "COD_MUNICIP"),
    "uf": ("UF", "CO_UF", "SG_UF", "COD_UF"),
    "cnes": ("CNES", "CO_CNES", "ESTABELEC", "COD_ESTAB"),
    # Conceito agregado (qualquer coluna de CID/diagnóstico) — mantido para
    # compatibilidade e como fallback amplo.
    "diagnostico": ("CID", "DIAG", "CAUSABAS", "CIDPRI", "CIDASSOC", "DIAGSEC"),
    # Conceitos granulares alinhados ao que o LLMQueryService usa no prompt:
    # permitem distinguir coluna de CID principal vs secundário vs causa de morte.
    "cid_principal": ("CIDPRI", "DIAG_PRINC", "CO_CIDPRIM", "DIAGPRIN", "CID_PRINC"),
    "cid_secundario": ("CIDSEC", "CIDASSOC", "DIAG_SECUN", "CO_CIDSEC", "CID_ASSO", "DIAGSEC", "CIDCAS"),
    "cid_morte": ("CAUSABAS", "CID_MORTE", "CAUSAMORTE", "CAUSA_MORTE", "CAUSABAS_O"),
    "cid_notificacao": ("CID_NOTIF", "CID_ASSO", "CID_NOTIFIC"),
    "procedimento": ("PROC", "PROCEDIMENTO", "PROC_REA", "COD_PROC"),
    "internacao": ("INTERN", "DT_INTER", "DT_SAIDA", "DIAS_PERM"),
    "permanencia": ("DIARIAS", "DIAS_PERM", "PERMANEN", "QT_DIARIAS"),
    "morte": ("MORTE", "OBITO", "DT_OBITO", "CAUSABAS"),
    "obito": ("OBITO", "MORTE", "DT_OBITO", "CAUSABAS"),
    "valor": ("VAL", "VL_", "VALOR", "CUSTO", "PAGO"),
    "data": ("DT_", "DATA", "NU_ANO", "CO_ANO"),
    "ano": ("ANO_CMPT", "NU_ANO", "SP_AA", "AP_ANO", "CO_ANO", "ANO"),
    "mes": ("MES_CMPT", "SP_MM", "AP_CMP", "CO_MES", "MES", "NU_MES"),
    "nascimento": ("NASC", "DT_NASC", "NASCIMENTO"),
    "peso": ("PESO", "PESO_NASC", "PESOPAC"),
    "gestacao": ("GESTA", "GRAVID", "PARTO"),
    "estabelecimento": ("CNES", "ESTABELEC", "UNIDADE"),
}

CONCEPT_LABELS: Dict[str, str] = {
    "sexo": "sexo do paciente",
    "idade": "idade",
    "raca_cor": "raça/cor",
    "municipio": "município de residência/ocorrência",
    "uf": "unidade federativa (UF)",
    "cnes": "estabelecimento de saúde (CNES)",
    "diagnostico": "diagnóstico/CID (qualquer)",
    "cid_principal": "diagnóstico/CID principal",
    "cid_secundario": "diagnóstico/CID secundário",
    "cid_morte": "causa de morte (CID)",
    "cid_notificacao": "CID de notificação/associação",
    "procedimento": "procedimento realizado",
    "internacao": "internação (datas de entrada/saída)",
    "permanencia": "dias de permanência",
    "morte": "indicador/ocorrência de morte",
    "obito": "óbito",
    "valor": "valores monetários",
    "data": "datas",
    "ano": "ano de competência",
    "mes": "mês de competência",
    "nascimento": "data de nascimento",
    "peso": "peso",
    "gestacao": "gestação/parto",
    "estabelecimento": "estabelecimento de saúde",
}


# ---------------------------------------------------------------------------
# Traduções código -> rótulo (DATASUS)
# ---------------------------------------------------------------------------

DATASUS_VALUE_LABELS: Dict[str, Dict[str, str]] = {
    "sexo": {
        "0": "Ignorado",
        "1": "Masculino",
        "2": "Feminino",
        "9": "Ignorado",
        "M": "Masculino",
        "F": "Feminino",
        "I": "Ignorado",
    },
    "raca_cor": {
        "0": "Ignorado",
        "1": "Branca",
        "2": "Preta",
        "3": "Parda",
        "4": "Amarela",
        "5": "Indígena",
        "9": "Ignorado",
    },
    "sim_nao": {
        "1": "Sim",
        "2": "Não",
        "9": "Ignorado",
    },
    "uf": {
        "11": "Rondônia",
        "12": "Acre",
        "13": "Amazonas",
        "14": "Roraima",
        "15": "Pará",
        "16": "Amapá",
        "17": "Tocantins",
        "21": "Maranhão",
        "22": "Piauí",
        "23": "Ceará",
        "24": "Rio Grande do Norte",
        "25": "Paraíba",
        "26": "Pernambuco",
        "27": "Alagoas",
        "28": "Sergipe",
        "29": "Bahia",
        "31": "Minas Gerais",
        "32": "Espírito Santo",
        "33": "Rio de Janeiro",
        "35": "São Paulo",
        "41": "Paraná",
        "42": "Santa Catarina",
        "43": "Rio Grande do Sul",
        "50": "Mato Grosso do Sul",
        "51": "Mato Grosso",
        "52": "Goiás",
        "53": "Distrito Federal",
    },
}


def value_label(domain: str, code: object) -> Optional[str]:
    """Traduz um código DATASUS para seu rótulo (ex.: sexo '2' -> 'Feminino')."""
    table = DATASUS_VALUE_LABELS.get(domain)
    if not table:
        return None
    key = str(code).strip()
    return table.get(key)


# Regras nome-de-coluna -> domínio de tradução (ordem importa: a primeira regra
# cujo token aparece no nome vence). Usado para traduzir valores retornados
# pelo DuckDB (ex.: SEXOPAC='2' -> 'Feminino') na resposta ao usuário.
COLUMN_DOMAIN_RULES: Tuple[Tuple[str, str], ...] = (
    ("RACACOR", "raca_cor"),
    ("RACA_COR", "raca_cor"),
    ("CS_RACA", "raca_cor"),
    ("RACA", "raca_cor"),
    ("SEXO", "sexo"),
    ("SG_UF", "uf"),
    ("CO_UF", "uf"),
    ("UF", "uf"),
)


def column_domain(column: str) -> Optional[str]:
    """Infere o domínio de tradução a partir do nome da coluna.

    Retorna ``None`` quando a coluna não tem tradução conhecida (ex.: uma
    contagem, um código de CID ou um valor numérico livre).
    """
    if not column:
        return None
    up = column.upper()
    for token, domain in COLUMN_DOMAIN_RULES:
        if token in up:
            return domain
    return None


def translate_value_for_column(column: str, value: object) -> Optional[str]:
    """Traduz ``value`` conforme o domínio inferido de ``column``.

    Só traduz valores escalares simples (ignora listas/dicts). Retorna ``None``
    quando não há tradução aplicável, permitindo ao chamador manter o valor
    original.
    """
    if value is None or isinstance(value, (list, dict, tuple, set)):
        return None
    domain = column_domain(column)
    if not domain:
        return None
    return value_label(domain, value)


# ---------------------------------------------------------------------------
# Dicionário
# ---------------------------------------------------------------------------

def _is_diagnosis_column(name: str) -> bool:
    upper = name.upper()
    if any(upper == n or upper.startswith(n) for n in DIAGNOSIS_COLUMN_NAMES):
        return True
    return any(upper.endswith(s) for s in DIAGNOSIS_COLUMN_SUFFIXES)


def _column_signature(by_group: Mapping[str, Sequence[Mapping[str, str]]]) -> str:
    parts: List[str] = []
    for grupo in sorted(by_group.keys()):
        cols = by_group.get(grupo) or []
        names = sorted((c.get("column") or "") for c in cols)
        parts.append(f"{grupo}:{','.join(names)}")
    return "|".join(parts)


@dataclass
class ColumnDictionary:
    """Visão consolidada e indexada das colunas de um conjunto de grupos."""

    by_group: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    signature: str = ""

    # campos derivados (lazy)
    _unique_columns: Optional[List[str]] = field(default=None, repr=False)
    _columns_by_upper: Optional[Dict[str, str]] = field(default=None, repr=False)
    _groups_by_column: Optional[Dict[str, List[str]]] = field(default=None, repr=False)
    _type_by_upper: Optional[Dict[str, str]] = field(default=None, repr=False)
    _concept_map: Optional[Dict[str, List[str]]] = field(default=None, repr=False)
    _diagnosis_columns: Optional[List[str]] = field(default=None, repr=False)
    _labeled_columns: Optional[List[str]] = field(default=None, repr=False)

    # -- construção --------------------------------------------------------
    @classmethod
    def from_grupo_columns(
        cls,
        grupo_columns: Mapping[str, Sequence[Mapping[str, str]]],
        grupos: Optional[Iterable[str]] = None,
    ) -> "ColumnDictionary":
        by_group: Dict[str, List[Dict[str, str]]] = {}
        selected = set(grupos) if grupos is not None else None
        for grupo, cols in (grupo_columns or {}).items():
            if selected is not None and grupo not in selected:
                continue
            norm: List[Dict[str, str]] = []
            for c in cols or []:
                name = (c.get("column") or "").strip()
                if not name:
                    continue
                norm.append({"column": name, "type": str(c.get("type") or "")})
            by_group[grupo] = norm
        signature = _column_signature(by_group)
        return cls(by_group=by_group, signature=signature)

    def matches(self, signature: str) -> bool:
        return self.signature == signature

    # -- derivados ---------------------------------------------------------
    def _rebuild_derived(self) -> None:
        by_upper: Dict[str, str] = {}
        type_by_upper: Dict[str, str] = {}
        groups_by_column: Dict[str, List[str]] = {}
        unique_ordered: List[str] = []
        seen = set()
        for grupo in sorted(self.by_group.keys()):
            for c in self.by_group[grupo]:
                name = c["column"]
                up = name.upper()
                if up not in by_upper:
                    by_upper[up] = name
                if type_by_upper.get(up) in (None, ""):
                    type_by_upper[up] = c.get("type", "")
                groups_by_column.setdefault(up, [])
                if grupo not in groups_by_column[up]:
                    groups_by_column[up].append(grupo)
                if up not in seen:
                    seen.add(up)
                    unique_ordered.append(name)
        self._columns_by_upper = by_upper
        self._type_by_upper = type_by_upper
        self._groups_by_column = groups_by_column
        self._unique_columns = unique_ordered
        self._concept_map = self._build_concept_map()
        self._diagnosis_columns = self._build_diagnosis_columns()
        self._labeled_columns = self._build_labeled_columns()

    def _build_concept_map(self) -> Dict[str, List[str]]:
        mapping: Dict[str, List[str]] = {}
        cols = self._unique_columns or []
        for concept, tokens in CONCEPT_TOKENS.items():
            # Pontua cada coluna pelo melhor casamento de token:
            #   3 = token igual ao nome inteiro
            #   2 = nome começa com o token (prefixo forte, ex.: CIDPRI...)
            #   1 = token aparece em qualquer posição (substring fraca)
            scored: List[Tuple[int, str]] = []
            for name in cols:
                up = name.upper()
                best = 0
                for tok in tokens:
                    tok_u = tok.upper()
                    if up == tok_u:
                        score = 3
                    elif up.startswith(tok_u):
                        score = 2
                    elif tok_u in up:
                        score = 1
                    else:
                        continue
                    if score > best:
                        best = score
                if best:
                    scored.append((best, name))
            if scored:
                # Mais relevantes primeiro; mantém ordem alfabética para empates.
                scored.sort(key=lambda item: (-item[0], item[1]))
                mapping[concept] = [name for _, name in scored]
        return mapping

    def _build_diagnosis_columns(self) -> List[str]:
        return [c for c in (self._unique_columns or []) if _is_diagnosis_column(c)]

    def _build_labeled_columns(self) -> List[str]:
        labeled: List[str] = []
        for grupo in sorted(self.by_group.keys()):
            for c in self.by_group[grupo]:
                labeled.append(f'"{c["column"]}" [{grupo}]')
        return labeled

    def _ensure(self) -> None:
        if self._unique_columns is None:
            self._rebuild_derived()

    # -- API ---------------------------------------------------------------
    @property
    def unique_columns(self) -> List[str]:
        self._ensure()
        return self._unique_columns or []

    @property
    def diagnosis_columns(self) -> List[str]:
        self._ensure()
        return self._diagnosis_columns or []

    @property
    def concept_map(self) -> Dict[str, List[str]]:
        self._ensure()
        return self._concept_map or {}

    @property
    def type_by_upper(self) -> Dict[str, str]:
        self._ensure()
        return self._type_by_upper or {}

    def columns_for_group(self, grupo: str) -> List[str]:
        return [c["column"] for c in self.by_group.get(grupo, [])]

    def groups_for_column(self, column: str) -> List[str]:
        self._ensure()
        return list((self._groups_by_column or {}).get(column.upper(), []))

    def labeled_columns(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
    ) -> List[str]:
        """Colunas rotuladas com o grupo de origem (ex.: ``"SEXOPAC" [ps]``).

        Quando ``grupos`` é None, usa todos os grupos; ``limit_per_group``
        limita quantas colunas de cada grupo entram (útil no fallback para não
        inflar o prompt com centenas/milhares de entradas).
        """
        if grupos is None:
            self._ensure()
            grupos = sorted(self.by_group.keys())
        out: List[str] = []
        for grupo in sorted(set(grupos)):
            cols = self.by_group.get(grupo, [])
            if limit_per_group is not None and limit_per_group > 0:
                cols = cols[:limit_per_group]
            for c in cols:
                out.append(f'"{c["column"]}" [{grupo}]')
        return out

    def labeled_columns_sql_hint(
        self,
        grupos: Optional[Iterable[str]] = None,
        limit_per_group: Optional[int] = None,
    ) -> str:
        return ", ".join(self.labeled_columns(grupos, limit_per_group))

    def to_sql_rows(self) -> List[Tuple[str, str, str]]:
        """Linhas (grupo, coluna, tipo) para persistência em DuckDB."""
        rows: List[Tuple[str, str, str]] = []
        for grupo in sorted(self.by_group.keys()):
            for c in self.by_group[grupo]:
                rows.append((grupo, c["column"], c.get("type", "")))
        return rows

    def translate(self, domain: str, code: object) -> Optional[str]:
        return value_label(domain, code)

    def translate_row(
        self, row: Mapping[str, object]
    ) -> Dict[str, object]:
        """Retorna uma cópia de ``row`` com valores traduzíveis convertidos.

        Para cada coluna cujo nome mapeia para um domínio conhecido (sexo,
        raça/cor, UF), acrescenta uma chave ``"<coluna>_label"`` com o rótulo
        legível, mantendo o valor original intacto. Não altera a estrutura das
        chaves existentes para não quebrar gráficos/serialização.
        """
        out: Dict[str, object] = dict(row)
        for key, value in row.items():
            label = translate_value_for_column(key, value)
            if label is not None:
                out[f"{key}_label"] = label
        return out

    def translate_rows(
        self, rows: Sequence[Mapping[str, object]]
    ) -> List[Dict[str, object]]:
        return [self.translate_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Store em memória (thread-safe, 1 slot por assinatura)
# ---------------------------------------------------------------------------

class ColumnDictionaryStore:
    """Cache em memória do ``ColumnDictionary`` chaveado por assinatura de grupos."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Dict[str, ColumnDictionary] = {}
        self._last: Optional[ColumnDictionary] = None

    def get(
        self,
        grupo_columns: Mapping[str, Sequence[Mapping[str, str]]],
        grupos: Optional[Iterable[str]] = None,
    ) -> ColumnDictionary:
        dictionary = ColumnDictionary.from_grupo_columns(grupo_columns, grupos)
        sig = dictionary.signature
        with self._lock:
            cached = self._cache.get(sig)
            if cached is not None:
                self._last = cached
                return cached
            self._cache[sig] = dictionary
            self._last = dictionary
            # limita o cache para evitar crescimento indefinido
            if len(self._cache) > 32:
                # remove a assinatura mais antiga que não seja a última
                for key in list(self._cache.keys()):
                    if key != sig:
                        self._cache.pop(key, None)
                        break
            return dictionary

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            self._last = None


# ---------------------------------------------------------------------------
# Persistência em DuckDB
# ---------------------------------------------------------------------------

def persist_to_duckdb(connection, dictionary: ColumnDictionary) -> bool:
    """Grava o dicionário consolidado na tabela DuckDB ``column_dictionary``.

    Best-effort: retorna ``False`` em caso de erro, sem propagar exceção.
    """
    if connection is None or dictionary is None:
        return False
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS column_dictionary (
                grupo VARCHAR,
                column_name VARCHAR,
                column_type VARCHAR,
                signature VARCHAR
            )
            """
        )
        connection.execute("DELETE FROM column_dictionary")
        rows = [
            (grupo, col, ctype, dictionary.signature)
            for (grupo, col, ctype) in dictionary.to_sql_rows()
        ]
        if rows:
            connection.executemany(
                "INSERT INTO column_dictionary VALUES (?, ?, ?, ?)", rows
            )
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("Falha ao persistir column_dictionary: %s", exc)
        return False


def load_columns_cache_dir(cache_dir: str) -> Dict[str, List[Dict[str, str]]]:
    """Lê todos os JSONs de ``cache_dir`` e devolve ``{grupo: [{column,type}]}``."""
    result: Dict[str, List[Dict[str, str]]] = {}
    if not cache_dir or not os.path.isdir(cache_dir):
        return result
    for fname in sorted(os.listdir(cache_dir)):
        if not fname.endswith(".json"):
            continue
        grupo = os.path.splitext(fname)[0]
        path = os.path.join(cache_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:  # pragma: no cover
            logger.warning("Falha ao ler cache de colunas %s: %s", path, exc)
            continue
        cols = payload.get("columns") if isinstance(payload, dict) else payload
        if not cols:
            continue
        norm: List[Dict[str, str]] = []
        for c in cols:
            if isinstance(c, str):
                norm.append({"column": c, "type": ""})
            elif isinstance(c, Mapping):
                name = c.get("column") or c.get("name") or ""
                if name:
                    norm.append({"column": name, "type": str(c.get("type") or "")})
        if norm:
            result[grupo] = norm
    return result