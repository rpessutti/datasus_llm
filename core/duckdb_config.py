from __future__ import annotations

import logging
import os

LOGGER = logging.getLogger("datasus_llm")

# Nome canonico do parametro no DuckDB (VSS/HNSW).
HNSW_EXPERIMENTAL_PERSISTENCE_PARAM = "hnsw_enable_experimental_persistence"

# Valores que consideramos "ligado" nas variaveis de ambiente.
_TRUTHY = {"1", "true", "yes", "on"}


def hnsw_experimental_persistence_enabled() -> bool:
    """Indica se a persistencia experimental de HNSW deve ser habilitada.

    Controlado por ``DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE`` (default: ligado),
    permitindo desligar caso a versao do DuckDB/VSS nao reconheca a flag.
    """
    raw = os.getenv("DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE", "true")
    return str(raw).strip().casefold() in _TRUTHY


def _ensure_vss_loaded(connection, *, label: str) -> bool:
    """Garante que a extensao VSS esteja carregada na conexao (best-effort).

    O parametro ``hnsw_enable_experimental_persistence`` so EXISTE no catalogo
    do DuckDB depois que a extensao VSS e carregada. Sem ela, ``SET ...`` falha
    com ``CatalogException``. Portanto precisamos carregar o VSS ANTES de
    aplicar o parametro.

    Estrategia (tolerante a ambientes offline e a versoes do DuckDB que ja
    embutem o VSS):
      * tenta ``LOAD vss`` (extensao ja instalada localmente);
      * se falhar, tenta ``INSTALL vss`` seguido de ``LOAD vss`` (baixa/instala);
      * qualquer falha e registrada em DEBUG e NAO interrompe o fluxo.

    Retorna True se o VSS ficou disponivel na conexao.
    """
    # 1) Ja esta carregado?
    try:
        connection.execute("SELECT 1 FROM duckdb_extensions() WHERE extension_name = 'vss' AND loaded")
        if connection.fetchone()[0]:
            return True
    except Exception:
        pass
    # 2) LOAD direto (caso ja instalado).
    for stmt in ("LOAD vss;", "INSTALL vss; LOAD vss;"):
        try:
            connection.execute(stmt)
            LOGGER.debug("%s: vss carregado via %r", label, stmt)
            return True
        except Exception as exc:
            LOGGER.debug("%s: falha em %r (%s)", label, stmt, exc)
            continue
    LOGGER.debug("%s: extensão vss indisponível; persistência HNSW não será configurada", label)
    return False


def apply_duckdb_experimental_settings(connection, *, label: str = "duckdb") -> None:
    """Habilita a persistencia experimental de HNSW na conexao informada.

    Deve ser chamado ANTES de gerar ou ler caches que usam indices HNSW/VSS.
    Idempotente e best-effort: falhas (ex.: extensao/flag indisponivel na versao
    do DuckDB) sao registradas em DEBUG/WARNING e NAO interrompem a
    inicializacao, garantindo que o restante do pipeline continue funcionando.

    Parametros
    ----------
    connection:
        Conexao DuckDB (``duckdb.DuckDBPyConnection``).
    label:
        Rotulo usado nos logs (ex.: nome do grupo) para diagnostico.
    """
    if connection is None:
        return
    if not hnsw_experimental_persistence_enabled():
        LOGGER.debug(
            "%s: %s desabilitado por configuração (DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE)",
            label, HNSW_EXPERIMENTAL_PERSISTENCE_PARAM,
        )
        return
    # A flag so existe no catalogo DEPOIS que o VSS esta carregado.
    _ensure_vss_loaded(connection, label=label)
    try:
        connection.execute(
            f"SET {HNSW_EXPERIMENTAL_PERSISTENCE_PARAM}=true"
        )
        LOGGER.debug("%s: %s=true aplicado", label, HNSW_EXPERIMENTAL_PERSISTENCE_PARAM)
    except Exception as exc:
        # Versoes do DuckDB sem a flag (ou sem o VSS) retornam erro de catalogo.
        LOGGER.debug(
            "%s: não foi possível aplicar %s (%s); seguindo sem persistência HNSW experimental",
            label, HNSW_EXPERIMENTAL_PERSISTENCE_PARAM, exc,
        )
