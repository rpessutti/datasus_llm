"""Migra o catalogo de grupos e remove o legado do banco principal (F5.5).

Uso:
    python -u dev_tools/migrate_group_catalog.py --dry-run
    python -u dev_tools/migrate_group_catalog.py --limpar-legado --backfill
    python -u dev_tools/migrate_group_catalog.py --backfill --grupo bi --periodos-distintos

Etapas:
    --limpar-legado: remove views/tabelas do modo legado no banco principal
        (datasus, datasus_cache, file_manifest, parquet_period_index,
        cache_metadata, cache_checkpoint).
    --backfill: preenche group_registry/group_periods (files, rows_est,
        status, periodos) a partir do disco e de cada <grupo>.duckdb.
    --periodos-distintos: permite SELECT DISTINCT em datasus_cache quando o
        period_index do grupo estiver vazio/ausente (lento em grupos grandes).
    --grupo: restringe o backfill a um grupo (pode repetir).
    --sem-backup: nao exige o export previo em _backups/.../main_db.
    --dry-run: apenas relata o que seria feito (nenhuma escrita).

Saida: _backups/simplificacao_cache_20261018_170419/limpeza.txt
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import duckdb  # noqa: E402

from config.settings import Settings  # noqa: E402
from core.io_utils import _discover_files_in  # noqa: E402
from db import group_registry  # noqa: E402
from db.period_fields import PERIOD_FIELDS, ano_int_expr, mes_expr  # noqa: E402

BACKUP_DIR = PROJECT_DIR / "_backups" / "simplificacao_cache_20261018_170419"
RELATORIO = BACKUP_DIR / "limpeza.txt"
LEGADO_VIEWS = ("datasus", "datasus_cache")
LEGADO_TABELAS = ("file_manifest", "parquet_period_index", "cache_metadata", "cache_checkpoint")


def _relacoes(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Le ``{nome: tipo}`` das relacoes do schema main."""
    linhas = conn.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    return {str(nome): str(tipo) for nome, tipo in linhas}


def _contar_legado(conn: duckdb.DuckDBPyConnection, relacoes: dict[str, str]) -> dict[str, int]:
    """Conta linhas das tabelas legadas existentes (views ficam de fora)."""
    contagens: dict[str, int] = {}
    for nome in LEGADO_TABELAS:
        if relacoes.get(nome) != "BASE TABLE":
            continue
        try:
            contagens[nome] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{nome}"').fetchone()[0] or 0
            )
        except Exception:
            contagens[nome] = -1
    return contagens


def _limpar_legado(
    conn: duckdb.DuckDBPyConnection, relacoes: dict[str, str], dry_run: bool
) -> list[str]:
    """Remove as views/tabelas legadas e devolve as linhas do relatorio."""
    linhas: list[str] = []
    for nome in LEGADO_VIEWS:
        if relacoes.get(nome) != "VIEW":
            linhas.append(f"view {nome}: ausente")
            continue
        if dry_run:
            linhas.append(f"view {nome}: DROP (dry-run)")
            continue
        conn.execute(f'DROP VIEW IF EXISTS "{nome}"')
        linhas.append(f"view {nome}: removida")
    for nome in LEGADO_TABELAS:
        tipo = relacoes.get(nome)
        if tipo != "BASE TABLE":
            linhas.append(f"tabela {nome}: ausente")
            continue
        if dry_run:
            linhas.append(f"tabela {nome}: DROP (dry-run)")
            continue
        conn.execute(f'DROP TABLE IF EXISTS "{nome}"')
        linhas.append(f"tabela {nome}: removida")
    return linhas

def _periodos_do_grupo(
    grupo: str, db_path: Path, permitir_distinto: bool
) -> tuple[dict[str, object], list[tuple[int, str]], list[str]]:
    """Le tabelas, rows_est e periodos de um banco de grupo (read-only).

    Args:
        grupo: nome do grupo (define o campo nativo de periodo).
        db_path: caminho do ``<grupo>.duckdb``.
        permitir_distinto: permite DISTINCT em ``datasus_cache`` quando nao ha
            ``period_index`` (lento em grupos com centenas de milhoes de linhas).

    Returns:
        tuple: ``(resumo, periodos, observacoes)``; ``resumo`` traz
        ``rows_est`` e ``tabelas``.
    """
    observacoes: list[str] = []
    if not db_path.is_file():
        return {"rows_est": 0, "tabelas": []}, [], ["banco ausente"]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tabelas = [
            str(row[0]) for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        rows_est = 0
        if "datasus_cache" in tabelas:
            rows_est = int(con.execute(
                "SELECT estimated_size FROM duckdb_tables() "
                "WHERE table_name = 'datasus_cache'"
            ).fetchone()[0] or 0)
        periodos: list[tuple[int, str]] = []
        if "period_index" in tabelas:
            periodos = [
                (int(ano), str(mes).zfill(2))
                for ano, mes in con.execute(
                    "SELECT DISTINCT parquet_year, parquet_month FROM period_index "
                    "WHERE parquet_year IS NOT NULL ORDER BY 1, 2"
                ).fetchall()
            ]
        if not periodos and permitir_distinto and "datasus_cache" in tabelas:
            colunas = {
                str(row[0]).lower()
                for row in con.execute("DESCRIBE datasus_cache").fetchall()
            }
            if "atend_ano" in colunas and "atend_mes" in colunas:
                ano_sql, mes_sql = '"atend_ano"', '"atend_mes"'
            else:
                periodo = PERIOD_FIELDS.get(grupo)
                if periodo is None:
                    observacoes.append("grupo sem campo nativo de periodo")
                    return {"rows_est": rows_est, "tabelas": tabelas}, [], observacoes
                ano_sql = ano_int_expr(periodo.columns[0])
                mes_sql = mes_expr(periodo.columns[0])
            observacoes.append("periodos via SELECT DISTINCT (lento)")
            try:
                periodos = [
                    (int(ano), str(mes).zfill(2))
                    for ano, mes in con.execute(
                        f"SELECT DISTINCT {ano_sql}, {mes_sql} FROM datasus_cache "
                        f"WHERE {ano_sql} IS NOT NULL ORDER BY 1, 2"
                    ).fetchall()
                ]
            except Exception as exc:
                observacoes.append(f"DISTINCT falhou: {type(exc).__name__}")
        return {"rows_est": rows_est, "tabelas": tabelas}, periodos, observacoes
    finally:
        con.close()


def _gravar_relatorio(linhas: list[str]) -> None:
    """Acrescenta um bloco datado ao relatorio do F5.5."""
    RELATORIO.parent.mkdir(parents=True, exist_ok=True)
    cabecalho = [
        "",
        "=" * 70,
        f"F5.5 {datetime.now().isoformat(timespec='seconds')}",
        "=" * 70,
    ]
    with RELATORIO.open("a", encoding="utf-8") as arq:
        arq.write("\n".join(cabecalho + linhas) + "\n")
    print(f"relatorio: {RELATORIO}")



def main() -> int:
    """Executa limpeza/backfill conforme as flags e grava o relatorio."""
    parser = argparse.ArgumentParser(description="F5.5: catalogo de grupos + limpeza")
    parser.add_argument("--limpar-legado", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--periodos-distintos", action="store_true")
    parser.add_argument("--grupo", action="append", default=[])
    parser.add_argument("--sem-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not (args.limpar_legado or args.backfill):
        parser.error("informe --limpar-legado e/ou --backfill")

    settings = Settings()
    cache_dir = Path(settings.cache_dir)
    data_dir = Path(settings.data_dir) if getattr(settings, "data_dir", None) else None
    main_db = Path(settings.database_path)
    linhas: list[str] = [
        f"banco: {main_db}",
        f"cache_dir: {cache_dir}",
        f"data_dir: {data_dir}",
        f"dry_run: {args.dry_run}",
    ]

    if not args.sem_backup and not (BACKUP_DIR / "main_db" / "load.sql").is_file():
        print("ERRO: export previo do banco principal ausente (rode o F0.2).")
        return 2

    conn = duckdb.connect(str(main_db))
    try:
        relacoes = _relacoes(conn)
        contagens = _contar_legado(conn, relacoes)
        linhas.append(f"relacoes main: {sorted(relacoes)}")
        linhas.append(f"linhas legado: {contagens}")

        if args.limpar_legado:
            linhas.extend(_limpar_legado(conn, relacoes, args.dry_run))

        if args.backfill:
            if args.dry_run:
                nomes_disco, origem = group_registry.grupos_no_disco(cache_dir, data_dir)
                registros = {
                    nome: {
                        "db_path": str(cache_dir / f"{nome}.duckdb"),
                        "source_dir": str(origem[nome]) if nome in origem else None,
                    }
                    for nome in nomes_disco
                }
                nomes = sorted(registros)
            else:
                nomes = group_registry.sync(conn, cache_dir, data_dir)
                registros = group_registry.registrados(conn)
            alvo = [n for n in nomes if not args.grupo or n in args.grupo]
            linhas.append(f"backfill: {len(alvo)} grupos ({alvo})")
            for nome in alvo:
                registro = registros.get(nome, {})
                db_path = Path(registro.get("db_path") or cache_dir / f"{nome}.duckdb")
                source_dir = Path(registro.get("source_dir") or cache_dir / nome)
                files = len(_discover_files_in(source_dir))
                if not files:
                    alternativa = cache_dir / nome
                    if alternativa != source_dir:
                        files = len(_discover_files_in(alternativa))
                resumo, periodos, observacoes = _periodos_do_grupo(
                    nome, db_path, args.periodos_distintos
                )
                tabelas = resumo["tabelas"]
                rows_est = int(resumo["rows_est"])
                status = "ok" if "datasus_cache" in tabelas and periodos else "unknown"
                linha = (
                    f"grupo {nome}: files={files} rows_est={rows_est} "
                    f"periodos={len(periodos)} status={status} tabelas={tabelas}"
                )
                if observacoes:
                    linha += f" obs={observacoes}"
                linhas.append(linha)
                if args.dry_run:
                    continue
                if status == "ok":
                    group_registry.atualizar_apos_ingestao(
                        conn, nome, files, rows_est, periodos
                    )
                else:
                    conn.execute(
                        "UPDATE group_registry SET files = ?, rows_est = ?, "
                        "status = 'unknown', updated_at = now() WHERE name = ?",
                        [files, rows_est, nome],
                    )
    finally:
        conn.close()

    for linha in linhas:
        print(linha)
    _gravar_relatorio(linhas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
