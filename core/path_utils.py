from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

LOGGER = logging.getLogger("datasus_llm")


def _schema_rows(connection, table: str) -> list[tuple[str, str]]:
    """Retorna as linhas de DESCRIBE (coluna, tipo) de uma tabela/view."""
    return [(row[0], row[1]) for row in connection.execute(f"DESCRIBE {table}").fetchall()]


def _file_period(path: Path) -> tuple[str, str] | None:
    """Extrai (ano, mês) de um arquivo Parquet DATASUS.

    O nome segue o padrão DATASUS: 'estadoAAMM_algo.parquet' (ex.: BIAC2201.parquet).
    Retorna (ano, mês) com ano de 4 dígitos ou None se o padrão não for reconhecido.
    """
    match = re.search(r"(?:20)?(\d{2})(0[1-9]|1[0-2])[a-z]*\.parquet$", path.name.casefold())
    if match:
        return "20" + match.group(1), match.group(2)
    return None


def _grouped_cache_path(cache_dir: Path, group_name: str, source: Path) -> Path:
    """Retorna o caminho de cache agrupado por ano/mês para um arquivo-fonte.

    Estrutura: {cache_dir}/{grupo}/{ano}/{mes}/{nome_do_arquivo}.parquet
    Se o ano/mês não puder ser extraído do nome, usa o caminho relativo original.
    """
    period = _file_period(source)
    if period:
        year, month = period
        return cache_dir / group_name / year / month / source.name
    return cache_dir / group_name / source.name


def _extract_period_from_path(path: Path) -> str | None:
    """Extrai o período (ano/mês) do caminho do cache agrupado.

    Estrutura: {cache_dir}/{grupo}/{ano}/{mes}/{arquivo}.parquet
    Retorna a string "ano/mês" (ex.: "2024/01") ou None se o padrão
    não for reconhecido (ano com 4 dígitos e mês com 2 dígitos).
    """
    try:
        parts = path.parts
        # Procura os últimos 3 componentes: ano, mes, arquivo
        if len(parts) >= 3:
            year = parts[-3]
            month = parts[-2]
            if year.isdigit() and len(year) == 4 and month.isdigit() and len(month) == 2:
                return f"{year}/{month}"
    except (IndexError, ValueError):
        pass
    return None


def _migrate_legacy_cache_paths(cache_dir: Path, group_name: str) -> None:
    """Move arquivos de cache antigos (estrutura {grupo}/arquivo.parquet) para a nova
    estrutura {grupo}/{ano}/{mes}/arquivo.parquet.

    Útil para migrar caches existentes sem re-ingestão completa.
    """
    group_cache_dir = cache_dir / group_name
    if not group_cache_dir.is_dir():
        return
    for path in list(group_cache_dir.rglob("*.parquet")):
        # Pula arquivos que já estão na estrutura nova (parent contém 4 dígitos + 2 dígitos)
        parts = path.relative_to(group_cache_dir).parts
        if len(parts) >= 3 and parts[-3].isdigit() and len(parts[-3]) == 4 \
           and parts[-2].isdigit() and len(parts[-2]) == 2:
            continue
        period = _file_period(path)
        if period:
            year, month = period
            new_path = group_cache_dir / year / month / path.name
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if not new_path.exists():
                try:
                    # Tenta mover diretamente para evitar cópia dupla
                    path.rename(new_path)
                    LOGGER.info("migrado %s -> %s", path, new_path)
                except OSError:
                    # Se renomear falhar (ex.: diretórios diferentes), copia e remove
                    shutil.copy2(path, new_path)
                    path.unlink(missing_ok=True)
                    LOGGER.info("migrado via cópia %s -> %s", path, new_path)