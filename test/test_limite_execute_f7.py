#!/usr/bin/env python3
"""Testes da PARTE 7 (#8 limite no execute, #9 reprojecao minima).

#8: `execute(sql, request_id, limite=...)` deve respeitar o limite; sem limite,
    usa DEFAULT_FETCH_ROWS.
#9: o fallback de coluna ausente deve reconstruir a view com a projecao MINIMA
    (colunas usadas + faltante), e NAO com TODAS as colunas dos grupos.

Estrategia: monta um DatasusCache de teste com um grupo real (DuckDB temporario,
uma coluna de CID alem da de periodo), prepara o cache e exercita o execute.
Pula automaticamente se o ambiente DuckDB nao estiver disponivel.

Execucao:
    python -m pytest test/test_limite_execute_f7.py -v
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_ENABLED", "false")

try:
    import duckdb  # noqa: F401
except Exception:  # pragma: no cover
    duckdb = None

pytestmark = pytest.mark.skipif(duckdb is None, reason="duckdb indisponivel")


class _Settings:
    def __init__(self, data, cache):
        self.data_dir = data
        self.cache_dir = cache
        self.official_cache_dir = cache / "official"
        self.database_path = cache / "datasus.duckdb"
        self.memory_limit = "512MB"
        self.threads = 2
        self.temp_dir = cache / "tmp"


def _montar_cache(tmp: Path):
    """Cria um DatasusCache de teste com o grupo 'er' e 30 linhas."""
    data = tmp / "data"
    cache = tmp / "cache"
    cache.mkdir(parents=True)
    p = data / "er" / "2022" / "05"
    p.mkdir(parents=True)
    # 30 linhas, CID variados, campo nativo de periodo DT_INTER.
    duckdb.connect().execute(
        "COPY (SELECT "
        "  20220500 + CAST(g AS INTEGER) AS DT_INTER, "
        "  'B0' || CAST(g AS VARCHAR) AS CIDPRI, "
        "  'M' AS SEXOPAC "
        "FROM (SELECT UNNEST(range(30)) AS g)) TO '"
        + str(p / "ER2205.parquet")
        + "' (FORMAT PARQUET)"
    )

    from db.cache import DatasusCache  # import tardio (depende do env)

    c = DatasusCache(_Settings(data, cache))
    c.sync_groups()
    for g in c.groups.values():
        g.reopen()
        g.ingest()
    return c


@pytest.fixture()
def cache_teste():
    tmp = Path(tempfile.mkdtemp(prefix="f7_"))
    c = None
    try:
        c = _montar_cache(tmp)
        yield c
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _preparar(c):
    return c.prepare_question_cache("casos de dengue em 2022", "req-f7")


# ------------------------------------------------------------------ #
# #8 LIMITE
# ------------------------------------------------------------------ #
def test_8_execute_respeita_limite(cache_teste):
    """SQL com LIMIT 5 deve retornar no maximo 5 linhas mesmo com 30 no grupo."""
    c = cache_teste
    _preparar(c)
    sql = (
        "SELECT \"CIDPRI\" AS cid, COUNT(*) AS casos "
        "FROM datasus_query_cache GROUP BY 1 ORDER BY 1 LIMIT 5"
    )
    rows = c.execute(sql, "req-f7", limite=5)
    assert len(rows) <= 5, f"esperado <=5, obtido {len(rows)}"
    assert len(rows) >= 1


def test_8_execute_limite_um(cache_teste):
    """limite=1 retorna exatamente 1 linha."""
    c = cache_teste
    _preparar(c)
    sql = (
        "SELECT \"CIDPRI\" AS cid, COUNT(*) AS casos "
        "FROM datasus_query_cache GROUP BY 1 ORDER BY 1"
    )
    rows = c.execute(sql, "req-f7", limite=1)
    assert len(rows) == 1


def test_8_execute_sem_limite_usa_default(cache_teste):
    """Sem limite, usa DEFAULT_FETCH_ROWS (>= numero de linhas disponiveis aqui)."""
    from config.constants import DEFAULT_FETCH_ROWS

    c = cache_teste
    _preparar(c)
    sql = "SELECT \"CIDPRI\" FROM datasus_query_cache"
    rows = c.execute(sql, "req-f7")  # sem limite
    # 30 linhas no grupo; default (>=100) deve trazer todas as 30.
    assert len(rows) == 30
    assert DEFAULT_FETCH_ROWS >= 30


# ------------------------------------------------------------------ #
# #9 REPROJECAO MINIMA
# ------------------------------------------------------------------ #
def test_9_extra_columns_inclui_coluna_faltante(cache_teste):
    """prepare_question_cache com extra_columns expoe a coluna pedida."""
    c = cache_teste
    c.prepare_question_cache("casos de dengue em 2022", "req-f7", sql=None)
    # View inicial: so a projecao minima das colunas usadas (vazia -> todas). Agora
    # reprepare com uma SQL que usa apenas CIDPRI e peca SEXOPAC como extra.
    c.prepare_question_cache(
        "casos de dengue em 2022",
        "req-f7",
        sql='SELECT "CIDPRI" FROM datasus_query_cache',
        extra_columns={"SEXOPAC"},
    )
    cols = {
        r[0].upper()
        for r in c.connection.execute("DESCRIBE datasus_query_cache").fetchall()
    }
    assert "SEXOPAC" in cols, f"SEXOPAC ausente: {sorted(cols)}"
    assert "CIDPRI" in cols


def test_9_reprojecao_nao_degrada_para_todas(cache_teste):
    """A reprojecao com extra_columns mantem projecao MINIMA (nao todas as 469)."""
    c = cache_teste
    total_grupo = len(
        c.connection.execute("DESCRIBE er.datasus_cache").fetchall()
    )
    c.prepare_question_cache(
        "casos de dengue em 2022",
        "req-f7",
        sql='SELECT "CIDPRI" FROM datasus_query_cache',
        extra_columns={"SEXOPAC"},
    )
    n_view = len(c.connection.execute("DESCRIBE datasus_query_cache").fetchall())
    # Deve ser pequeno (poucas colunas), bem menor que o total do grupo.
    assert n_view < total_grupo, f"view={n_view} grupo={total_grupo}"
    assert n_view <= 6, f"projecao deveria ser minima, obtido {n_view} colunas"
