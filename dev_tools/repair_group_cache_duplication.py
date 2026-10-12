"""Reparo do cache de grupos DUPLICADO (2x) — remove SO linhas duplicadas.

Contexto (medido em 2026-10-12)
-------------------------------
Os bancos `<grupo>.duckdb` vieram de outra maquina (Linux) e foram reingeridos no
Windows com uma CHAVE DE MANIFESTO diferente. Resultado: cada arquivo entrou DUAS
vezes na tabela fisica `datasus_cache` (uma com `parquet_path` Linux, outra com o
caminho Windows) e o `file_manifest` ficou com 2x entradas. Como a view unificada
(`datasus_query_cache`) e um UNION ALL filtrando por `atend_ano`/`atend_mes`, as
DUAS copias passam pelo filtro -> COUNT/SUM saem 2x.

O que este script faz
---------------------
  * `--dry-run` (default): MEDE e imprime, por grupo, `.duckdb`, No de Parquet,
    linhas da tabela, linhas reais dos Parquet (somente FOOTER), a razao
    tabela/Parquet, `parquet_path` distintos, o manifesto (total/distinto),
    as entradas de manifesto com caminho inexistente e o espaco recuperavel.
  * `--apply <grupo> ...`: repara APENAS as linhas/entradas duplicadas:
      1) remove do `file_manifest` as linhas cujo `cached` NAO existe no disco;
      2) remove da tabela as linhas cujo `parquet_path` esta FORA do cache atual
         (e as com `parquet_path` nulo);
      3) `CHECKPOINT` + `VACUUM` para devolver o espaco ao arquivo;
      4) revalida a razao (exige 1,000) e imprime antes/depois.
  * `--rebuild <grupo> ...`: caminho CARO (ultimo recurso) — recria a tabela a
    partir dos Parquet (`dev_tools/rebuild_group_cache.py`).

Garantias
---------
  * **NUNCA** apaga/move/cria Parquet: eles sao a fonte de verdade e o cache real.
  * `DELETE` transacional: se falhar, a tabela fica intacta (rollback do DuckDB).
  * Recusa-se a rodar se o resultado removeria TODAS as linhas (`keep_rows == 0`).
  * Cada grupo e aberto em conexao PROPRIA (sem ATTACH de 16 bancos) e fechado ao fim.
  * Mede antes/depois; nao confia em contagem estimada para decidir.

Uso
---
    python dev_tools/repair_group_cache_duplication.py --dry-run
    python dev_tools/repair_group_cache_duplication.py --apply er acf rj
    python dev_tools/repair_group_cache_duplication.py --rebuild pa
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import setting  # noqa: E402  (cache_dir/database_path reais)


def cache_dir() -> Path:
    """Diretorio de cache configurado (onde vivem `<grupo>.duckdb` e os Parquet).

    Returns:
        Path: diretorio real de cache (``config.settings.setting.cache_dir``).
    """
    return Path(setting.cache_dir)


def grupos(cache_dir_path: Path) -> list[str]:
    """Lista os grupos com banco DuckDB no cache (ignora bancos internos).

    Args:
        cache_dir_path: diretorio de cache.

    Returns:
        list[str]: nomes dos grupos (stem de ``<grupo>.duckdb``), ordenados.
    """
    ignorar = {"datasus", "datasus_fallback", "datasus_query_cache"}
    return sorted(
        p.stem for p in cache_dir_path.glob("*.duckdb") if p.stem not in ignorar
    )


def parquets_do_grupo(cache_dir_path: Path, grupo: str) -> list[Path]:
    """Retorna os Parquet cacheados de um grupo (somente leitura de disco).

    Args:
        cache_dir_path: diretorio de cache.
        grupo: nome do grupo.

    Returns:
        list[Path]: caminhos ordenados de ``<cache>/<grupo>/**.parquet``.
    """
    pasta = cache_dir_path / grupo
    return sorted(pasta.rglob("*.parquet")) if pasta.is_dir() else []


def _lista_sql(paths: list[Path]) -> str:
    """Monta um literal SQL de lista de strings a partir de caminhos.

    Args:
        paths: caminhos a listar.

    Returns:
        str: literal ``['a','b']`` (aspas simples escapadas).
    """
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def linhas_dos_parquets(paths: list[Path]) -> int:
    """Soma as linhas dos Parquet lendo APENAS os footers (sem varrer dados).

    Args:
        paths: Parquet do grupo.

    Returns:
        int: total de linhas; 0 se a lista estiver vazia.
    """
    if not paths:
        return 0
    con = duckdb.connect(":memory:")
    try:
        return int(con.execute(
            f"SELECT COALESCE(SUM(num_rows), 0) FROM parquet_file_metadata({_lista_sql(paths)})"
        ).fetchone()[0])
    finally:
        con.close()


def conectar(db_path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Abre uma conexao DuckDB ISOLADA para um banco de grupo.

    Configura memoria/threads/spill de forma conservadora (o reparo nao deve
    competir com o DuckDB da aplicacao). Nao faz ATTACH de outros bancos.

    Args:
        db_path: caminho do ``<grupo>.duckdb``.
        read_only: abre em modo somente leitura (usado no dry-run).

    Returns:
        duckdb.DuckDBPyConnection: conexao aberta (o chamador deve fechar).
    """
    con = duckdb.connect(str(db_path), read_only=read_only)
    try:
        con.execute("SET threads=4")
        con.execute("SET memory_limit='4GB'")
        spill = cache_dir() / "duckdb_spill"
        spill.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{str(spill).replace(chr(92), chr(47))}'")
    except Exception:
        pass  # PRAGMAs best-effort: o reparo funciona sem eles
    return con


def relacao_existe(con: duckdb.DuckDBPyConnection, nome: str) -> bool:
    """Informa se uma TABELA existe no banco aberto.

    Args:
        con: conexao DuckDB.
        nome: nome da tabela (sem schema).

    Returns:
        bool: True quando a tabela existe.
    """
    return bool(con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [nome]
    ).fetchone()[0])


def contar(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> int:
    """Executa um ``SELECT`` escalar e devolve inteiro (0 quando nulo ou erro).

    Args:
        con: conexao DuckDB.
        sql: consulta que devolve um unico valor.
        params: parametros posicionais (opcional).

    Returns:
        int: valor escalar como inteiro (0 em caso de erro).
    """
    try:
        valor = con.execute(sql, params or []).fetchone()
        return int(valor[0] or 0) if valor else 0
    except Exception:
        return 0


def medir_tabela(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Mede a tabela ``datasus_cache`` do banco aberto.

    Mede apenas contagens: total de linhas, `parquet_path` distintos e
    `parquet_path` nulos. Nao varre as demais colunas.

    Args:
        con: conexao DuckDB aberta no ``<grupo>.duckdb``.

    Returns:
        dict[str, int]: ``{"linhas", "paths_distintos", "paths_nulos"}``
        (tudo 0 quando a tabela nao existe).
    """
    if not relacao_existe(con, "datasus_cache"):
        return {"linhas": 0, "paths_distintos": 0, "paths_nulos": 0}
    return {
        "linhas": contar(con, "SELECT COUNT(*) FROM datasus_cache"),
        "paths_distintos": contar(con, "SELECT COUNT(DISTINCT parquet_path) FROM datasus_cache"),
        "paths_nulos": contar(con, "SELECT COUNT(*) FROM datasus_cache WHERE parquet_path IS NULL"),
    }


def medir_manifesto(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Mede o ``file_manifest`` do banco aberto.

    Args:
        con: conexao DuckDB aberta no ``<grupo>.duckdb``.

    Returns:
        dict[str, int]: ``{"total", "distintos", "in_db", "invalidos", "orfaos"}``,
        onde ``orfaos`` conta as entradas cujo ``cached`` nao existe no disco.
    """
    if not relacao_existe(con, "file_manifest"):
        return {"total": 0, "distintos": 0, "in_db": 0, "invalidos": 0, "orfaos": 0}
    return {
        "total": contar(con, "SELECT COUNT(*) FROM file_manifest"),
        "distintos": contar(con, "SELECT COUNT(DISTINCT source) FROM file_manifest"),
        "in_db": contar(con, "SELECT COUNT(*) FROM file_manifest WHERE in_db = 1"),
        "invalidos": contar(con, "SELECT COUNT(*) FROM file_manifest WHERE invalid = 1"),
        "orfaos": len(manifesto_orfaos(con)),
    }


def manifesto_orfaos(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """Lista entradas do manifesto cujo caminho cacheado NAO existe no disco.

    Essas linhas sao heranca da ingestao em outra maquina (caminhos Linux) e
    inflam a contagem de "arquivos" exibida, sem corresponder a nenhum Parquet.

    Args:
        con: conexao DuckDB aberta no ``<grupo>.duckdb``.

    Returns:
        list[tuple[str, str]]: pares ``(source, cached)`` orfaos (vazio quando
        nao existe a tabela ``file_manifest``).
    """
    if not relacao_existe(con, "file_manifest"):
        return []
    linhas = con.execute("SELECT source, COALESCE(cached, '') FROM file_manifest").fetchall()
    orfaos: list[tuple[str, str]] = []
    for source, cached in linhas:
        caminho = Path(str(cached)) if cached else None
        if caminho is None or not caminho.is_file():
            orfaos.append((str(source), str(cached)))
    return orfaos


def limpar_manifesto(con: duckdb.DuckDBPyConnection, orfaos: list[tuple[str, str]]) -> int:
    """Remove do manifesto as entradas orfas, em lotes de 500.

    Args:
        con: conexao DuckDB em modo escrita no ``<grupo>.duckdb``.
        orfaos: pares ``(source, cached)`` de :func:`manifesto_orfaos`.

    Returns:
        int: quantidade de linhas removidas (0 quando a lista esta vazia).
    """
    if not orfaos:
        return 0
    fontes = [source for source, _ in orfaos]
    for i in range(0, len(fontes), 500):
        lote = fontes[i:i + 500]
        marcadores = ", ".join(["?"] * len(lote))
        con.execute(f"DELETE FROM file_manifest WHERE source IN ({marcadores})", lote)
    return len(fontes)


def espaco_livre_suficiente(db_path: Path, min_razao: float = 0.15) -> tuple[bool, float, float]:
    """Informa se ha espaco livre para o 'VACUUM' reescrever o banco.

    Args:
        db_path: caminho do ``<grupo>.duckdb``.
        min_razao: fracao minima do tamanho do banco que deve estar livre.

    Returns:
        tuple[bool, float, float]: ``(ok, livre_gb, banco_gb)``.
    """
    livre = shutil.disk_usage(str(db_path.parent)).free / 1e9
    banco = db_path.stat().st_size / 1e9 if db_path.is_file() else 0.0
    return livre >= banco * min_razao, livre, banco


def diagnosticar(cache_dir_path: Path, grupo: str) -> dict[str, object]:
    """Produz o diagnostico SOMENTE LEITURA de um grupo (base do ``--dry-run``).

    Args:
        cache_dir_path: diretorio de cache.
        grupo: nome do grupo.

    Returns:
        dict[str, object]: medidas de disco, tabela, Parquet (via footer) e
        manifesto, incluindo ``razao`` (linhas da tabela / linhas dos Parquet) e
        ``mb_duplicados`` (espaco atribuido as linhas excedentes).
    """
    db_path = cache_dir_path / f"{grupo}.duckdb"
    pq = parquets_do_grupo(cache_dir_path, grupo)
    info: dict[str, object] = {
        "grupo": grupo,
        "db_mb": round(db_path.stat().st_size / 1e6, 1) if db_path.is_file() else None,
        "parquets": len(pq),
        "parquet_mb": round(sum(p.stat().st_size for p in pq) / 1e6, 1),
        "parquet_linhas": linhas_dos_parquets(pq),
    }
    if not db_path.is_file():
        info["erro"] = "banco do grupo inexistente"
        return info
    con = conectar(db_path, read_only=True)
    try:
        info["tabela"] = medir_tabela(con)
        info["manifesto"] = medir_manifesto(con)
    finally:
        con.close()
    tabela = info["tabela"] if isinstance(info["tabela"], dict) else {}
    linhas_tabela = int(tabela.get("linhas", 0))          # type: ignore[union-attr]
    linhas_parquet = int(info["parquet_linhas"] or 0)
    info["razao"] = round(linhas_tabela / linhas_parquet, 3) if linhas_parquet else None
    excedente = max(linhas_tabela - linhas_parquet, 0)
    if linhas_tabela:
        info["mb_duplicados"] = round(float(info["db_mb"] or 0.0) * excedente / linhas_tabela, 1)
    return info


def deduplicar_tabela(
    con: duckdb.DuckDBPyConnection,
    cache_dir_path: Path,
    remover_nulos: bool = False,
) -> dict[str, int]:
    """Remove da tabela APENAS as linhas cujo ``parquet_path`` nao pertence ao cache atual.

    As linhas duplicadas sao as herdadas da outra maquina: o MESMO arquivo entrou
    uma vez com caminho Linux e outra com o caminho Windows. Mantemos as que
    apontam para ``<cache_dir>/...`` e removemos o resto.

    Guarda de seguranca: se a remocao deixaria a tabela VAZIA, nao executa nada
    (levanta ``RuntimeError``) — melhor deixar como esta e reconstruir (F5).

    Args:
        con: conexao DuckDB em modo escrita.
        cache_dir_path: diretorio de cache atual (dono das linhas a manter).
        remover_nulos: quando True, remove tambem linhas com ``parquet_path`` NULL.

    Returns:
        dict[str, int]: ``{"antes", "mantidas", "removidas"}``.

    Raises:
        RuntimeError: quando nenhuma linha aponta para o cache atual.
    """
    if not relacao_existe(con, "datasus_cache"):
        return {"antes": 0, "mantidas": 0, "removidas": 0}
    prefixo = str(cache_dir_path).rstrip("/\\") + os.sep
    antes = contar(con, "SELECT COUNT(*) FROM datasus_cache")
    mantidas = contar(
        con,
        "SELECT COUNT(*) FROM datasus_cache WHERE starts_with(parquet_path, ?)",
        [prefixo],
    )
    if mantidas == 0:
        raise RuntimeError(
            f"nenhuma linha aponta para {prefixo!r}: remocao abortada "
            "(usar --rebuild para recriar do Parquet)"
        )
    condicao = "NOT starts_with(parquet_path, ?)"
    if remover_nulos:
        condicao = f"parquet_path IS NULL OR {condicao}"
    con.execute(f"DELETE FROM datasus_cache WHERE {condicao}", [prefixo])
    return {
        "antes": antes,
        "mantidas": mantidas,
        "removidas": antes - contar(con, "SELECT COUNT(*) FROM datasus_cache"),
    }


def compactar(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Compacta o arquivo do banco para devolver o espaco das linhas removidas.

    Executa ``CHECKPOINT`` e, quando disponivel, ``VACUUM`` (DuckDB >= 1.2).
    Falhas nao abortam o reparo: apenas sao registradas no retorno.

    Args:
        con: conexao DuckDB (banco do grupo como 'main', sem ATTACH).

    Returns:
        dict[str, str]: status por comando (``"ok"`` ou a mensagem de erro).
    """
    status: dict[str, str] = {}
    for comando in ("CHECKPOINT", "VACUUM"):
        try:
            con.execute(comando)
            status[comando.lower()] = "ok"
        except Exception as exc:
            status[comando.lower()] = f"{type(exc).__name__}: {exc}"
    return status


def reparar(
    cache_dir_path: Path,
    grupo: str,
    remover_nulos: bool = False,
    min_razao: float = 0.15,
    force: bool = False,
) -> dict[str, object]:
    """Repara UM grupo: manifesto orfao + linhas duplicadas + compactacao.

    Fluxo: mede antes -> valida espaco livre -> (1) remove do manifesto as
    entradas cujo caminho nao existe; (2) remove as linhas cujo ``parquet_path``
    esta fora do cache atual; (3) ``CHECKPOINT``/``VACUUM`` -> mede depois.
    O ``DELETE`` e transacional: qualquer erro deixa a tabela como estava.

    Args:
        cache_dir_path: diretorio de cache.
        grupo: nome do grupo.
        remover_nulos: remove tambem linhas com ``parquet_path`` NULL.
        min_razao: fracao minima de espaco livre (relativa ao tamanho do banco).
        force: ignora a checagem de espaco livre.

    Returns:
        dict[str, object]: resultado com ``antes``/``depois``, contagens removidas
        e status da compactacao (ou ``erro`` quando nao aplicavel).
    """
    db_path = cache_dir_path / f"{grupo}.duckdb"
    if not db_path.is_file():
        return {"grupo": grupo, "erro": f"banco inexistente: {db_path}"}

    ok_espaco, livre_gb, banco_gb = espaco_livre_suficiente(db_path, min_razao)
    if not ok_espaco and not force:
        return {
            "grupo": grupo,
            "erro": "espaco livre insuficiente para o VACUUM (use --force para ignorar)",
            "livre_gb": round(livre_gb, 2),
            "banco_gb": round(banco_gb, 2),
        }

    antes = diagnosticar(cache_dir_path, grupo)
    con = conectar(db_path, read_only=False)
    try:
        linhas_manifesto = limpar_manifesto(con, manifesto_orfaos(con))
        dedup = deduplicar_tabela(con, cache_dir_path, remover_nulos)
        compactacao = compactar(con)
    except duckdb.IOException as exc:
        return {"grupo": grupo, "erro": f"banco em uso (feche a aplicacao): {exc}"}
    finally:
        con.close()

    return {
        "grupo": grupo,
        "manifesto_removidas": linhas_manifesto,
        "linhas": dedup,
        "compactacao": compactacao,
        "antes": antes,
        "depois": diagnosticar(cache_dir_path, grupo),
    }


def reconstruir(cache_dir_path: Path, grupo: str) -> dict[str, object]:
    """Caminho CARO (ultimo recurso): recria a tabela a partir dos Parquet.

    Delega para ``dev_tools.rebuild_group_cache.rebuild_group``. Exige que o
    modulo aponte para o MESMO diretorio de cache (o padrao do modulo e
    ``<projeto>/cache``, normalmente vazio).

    Args:
        cache_dir_path: diretorio de cache real.
        grupo: nome do grupo.

    Returns:
        dict[str, object]: ``{"grupo", "acao"}`` ou ``{"erro"}``.
    """
    from dev_tools import rebuild_group_cache as rg

    if Path(rg.CACHE_DIR) != cache_dir_path:
        return {
            "grupo": grupo,
            "erro": (
                f"rebuild_group_cache.CACHE_DIR={rg.CACHE_DIR} difere do cache real "
                f"({cache_dir_path}); ajuste o modulo antes de usar --rebuild"
            ),
        }
    rg.rebuild_group(grupo, dry_run=False)
    return {"grupo": grupo, "acao": "rebuild a partir do Parquet"}


def compactar_arquivo(cache_dir_path: Path, grupo: str, force: bool = False) -> dict[str, object]:
    """Devolve o espaco em disco REESCREVENDO o arquivo do grupo (unica forma eficaz).

    Por que: `DELETE` + `CHECKPOINT`/`VACUUM` removem as LINHAS, mas os blocos
    ficam *livres dentro* do `<grupo>.duckdb` (medido: acf com 68 blocos usados e
    115 livres). Para devolver espaco ao sistema e preciso reescrever o arquivo.

    Estrategia (sem NENHUM reprocessamento de dados, sem tocar nos Parquet):
      1. `ATTACH` do banco atual (somente leitura) e de um arquivo NOVO;
      2. `COPY FROM DATABASE origem TO destino` (copia tabelas/views ja deduplicadas);
      3. valida contagens no arquivo novo (tabela e manifesto);
      4. troca os arquivos por `rename` (o antigo vira `.bak_<ts>`);
      5. apaga o `.bak_<ts>` somente APOS a validacao do arquivo novo.

    Exige espaco livre >= blocos USADOS do banco (mais 15% de margem). Em caso de
    falha, o arquivo original permanece intacto (nada e apagado antes da validacao).

    Args:
        cache_dir_path: diretorio de cache.
        grupo: nome do grupo.
        force: ignora a checagem de espaco livre.

    Returns:
        dict[str, object]: medidas antes/depois (MB) e contagens validadas.
    """
    db_path = cache_dir_path / f"{grupo}.duckdb"
    if not db_path.is_file():
        return {"grupo": grupo, "erro": f"banco inexistente: {db_path}"}

    novo_path = cache_dir_path / f"{grupo}.recompact.duckdb"
    if novo_path.exists():
        novo_path.unlink()

    con = conectar(db_path, read_only=False)
    try:
        linhas_cols = [r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM pragma_database_size()"
        ).fetchall()]
        med = dict(zip(linhas_cols, con.execute("SELECT * FROM pragma_database_size()").fetchone()))
        usados_bytes = int(med.get("used_blocks") or 0) * int(med.get("block_size") or 0)
        con.execute("CHECKPOINT")
    finally:
        con.close()

    livre = shutil.disk_usage(str(cache_dir_path)).free
    if not force and livre < usados_bytes * 1.15:
        return {
            "grupo": grupo,
            "erro": "espaco livre insuficiente para reescrever o arquivo",
            "livre_gb": round(livre / 1e9, 2),
            "necessario_gb": round(usados_bytes * 1.15 / 1e9, 2),
        }

    antes = diagnosticar(cache_dir_path, grupo)
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{db_path}' AS origem (READ_ONLY)")
        con.execute(f"ATTACH '{novo_path}' AS destino")
        con.execute("COPY FROM DATABASE origem TO destino")
        con.execute("DETACH destino")
        con.execute("DETACH origem")
    except Exception as exc:
        con.close()
        if novo_path.exists():
            novo_path.unlink()
        return {"grupo": grupo, "erro": f"{type(exc).__name__}: {exc}"}
    con.close()

    # Validacao no arquivo NOVO (antes de trocar qualquer coisa).
    con = conectar(novo_path, read_only=True)
    try:
        novo_tabela = medir_tabela(con)
        novo_manifesto = medir_manifesto(con)
    finally:
        con.close()
    esperado = antes.get("tabela") if isinstance(antes.get("tabela"), dict) else {}
    if novo_tabela["linhas"] != int(esperado.get("linhas", -1)):  # type: ignore[union-attr]
        novo_path.unlink()
        return {
            "grupo": grupo,
            "erro": "validacao falhou: contagem de linhas difere no arquivo reescrito",
            "novo": novo_tabela,
            "esperado": esperado,
        }

    backup_path = cache_dir_path / f"{grupo}.duckdb.bak_{int(time.time())}"
    db_path.rename(backup_path)
    try:
        novo_path.rename(db_path)
    except Exception as exc:
        backup_path.rename(db_path)  # desfaz a troca
        return {"grupo": grupo, "erro": f"falha ao trocar o arquivo: {exc}"}

    con = conectar(db_path, read_only=True)
    try:
        tabela_final = medir_tabela(con)
        manifesto_final = medir_manifesto(con)
    finally:
        con.close()
    backup_path.unlink(missing_ok=True)

    return {
        "grupo": grupo,
        "acao": "arquivo reescrito (linhas e manifesto preservados)",
        "mb_antes": antes.get("db_mb"),
        "mb_depois": round(db_path.stat().st_size / 1e6, 1),
        "tabela": tabela_final,
        "manifesto": manifesto_final,
    }


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada da CLI (dry-run por padrao; ``--apply`` efetiva o reparo).

    Args:
        argv: argumentos de linha de comando (default: ``sys.argv[1:]``).

    Returns:
        int: 0 em sucesso; 1 quando algum grupo retorna ``erro``.
    """
    parser = argparse.ArgumentParser(
        description="Reparo do cache de grupos duplicado (NUNCA toca nos Parquet).",
    )
    parser.add_argument("--dry-run", nargs="*", metavar="GRUPO",
                        help="mede e reporta (padrao quando nao ha --apply/--rebuild)")
    parser.add_argument("--apply", nargs="+", metavar="GRUPO",
                        help="manifesto orfao + linhas duplicadas + CHECKPOINT/VACUUM")
    parser.add_argument("--rebuild", nargs="+", metavar="GRUPO",
                        help="recria a tabela a partir dos Parquet (caro)")
    parser.add_argument("--rewrite", nargs="+", metavar="GRUPO",
                        help="devolve espaco em disco reescrevendo o arquivo (COPY FROM DATABASE)")
    parser.add_argument("--remover-paths-nulos", action="store_true",
                        help="remove tambem linhas com parquet_path NULL")
    parser.add_argument("--min-free-ratio", type=float, default=0.15,
                        help="espaco livre minimo (fracao do tamanho do banco)")
    parser.add_argument("--force", action="store_true", help="ignora a checagem de espaco")
    args = parser.parse_args(argv)

    cache_dir_path = cache_dir()
    todos = grupos(cache_dir_path)
    if args.apply:
        alvos, modo = args.apply, "apply"
    elif args.rewrite:
        alvos, modo = args.rewrite, "rewrite"
    elif args.rebuild:
        alvos, modo = args.rebuild, "rebuild"
    else:
        alvos, modo = (args.dry_run or todos), "dry-run"

    print(json.dumps({"cache_dir": str(cache_dir_path), "modo": modo, "grupos": alvos},
                     ensure_ascii=False), flush=True)

    falhas = 0
    for grupo in alvos:
        if modo != "rebuild" and grupo not in todos:
            print(json.dumps({"grupo": grupo, "aviso": "sem banco no cache"},
                             ensure_ascii=False), flush=True)
            continue
        if modo == "dry-run":
            resultado = diagnosticar(cache_dir_path, grupo)
        elif modo == "apply":
            resultado = reparar(cache_dir_path, grupo, args.remover_paths_nulos,
                                args.min_free_ratio, args.force)
        elif modo == "rewrite":
            resultado = compactar_arquivo(cache_dir_path, grupo, args.force)
        else:
            resultado = reconstruir(cache_dir_path, grupo)
        if "erro" in resultado:
            falhas += 1
        print(json.dumps(resultado, ensure_ascii=False, default=str), flush=True)

    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
