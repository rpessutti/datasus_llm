"""Testes unitários para a lógica de logging do cache gravado."""

import sys
import os
from pathlib import Path

# Adiciona o diretório do projeto ao path para importar app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Importa as funções e classes necessárias do app.py
from core.path_utils import _grouped_cache_path, _file_period


def test_extract_period_from_path():
    """Testa a extração do período (ano/mês) do caminho do cache."""
    from core.path_utils import _extract_period_from_path

    # Casos de teste: caminho -> período esperado
    test_cases = [
        # Caminho com estrutura ano/mês correta
        (Path("/home/robinson/datasus_llm/cache/sp/2024/01/BIAC2401.parquet"), "2024/01"),
        (Path("/home/robinson/datasus_llm/cache/rj/2023/12/BIAR2312.parquet"), "2023/12"),
        (Path("/home/robinson/datasus_llm/cache/am/2025/06/BIAAM2506.parquet"), "2025/06"),
        # Caminho sem estrutura ano/mês
        (Path("/home/robinson/datasus_llm/cache/sp/arquivo.parquet"), None),
        (Path("/home/robinson/datasus_llm/cache/parquet/BIAC2401.parquet"), None),
        # Caminho curto demais
        (Path("/tmp/test.parquet"), None),
        # Estrutura correta mas formato errado
        (Path("/home/robinson/datasus_llm/cache/sp/24/01/BIAC2401.parquet"), None),  # ano com 2 dígitos
        (Path("/home/robinson/datasus_llm/cache/sp/2024/1/BIAC2401.parquet"), None),  # mês com 1 dígito
    ]

    print("=== Teste: _extract_period_from_path ===")
    all_passed = True
    for path, expected in test_cases:
        result = _extract_period_from_path(path)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  {status}: {path} -> {result} (esperado: {expected})")

    return all_passed


def test_file_period():
    """Testa a extração do período do nome do arquivo."""
    print("\n=== Teste: _file_period ===")
    all_passed = True
    test_cases = [
        (Path("BIAC2401.parquet"), ("2024", "01")),
        (Path("BIAR2312.parquet"), ("2023", "12")),
        (Path("BIAAM2506.parquet"), ("2025", "06")),
        (Path("BIASP1203.parquet"), ("2012", "03")),  # ano 2012 (12)
        (Path("arquivo_sem_periodo.parquet"), None),
        (Path("BIAC2401b.parquet"), ("2024", "01")),  # sufixo em minúsculas
    ]

    for path, expected in test_cases:
        result = _file_period(path)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  {status}: {path.name} -> {result} (esperado: {expected})")

    return all_passed


def test_grouped_cache_path():
    """Testa a extração do caminho de cache agrupado."""
    print("\n=== Teste: _grouped_cache_path ===")
    all_passed = True
    test_cases = [
        # (source, cache_dir, grupo, esperado)
        (Path("BIAC2401.parquet"), Path("/tmp/cache"), "sp",
         Path("/tmp/cache/sp/2024/01/BIAC2401.parquet")),
        (Path("BIAR2312.parquet"), Path("/tmp/cache"), "rj",
         Path("/tmp/cache/rj/2023/12/BIAR2312.parquet")),
        (Path("arquivo_sem_periodo.parquet"), Path("/tmp/cache"), "sp",
         Path("/tmp/cache/sp/arquivo_sem_periodo.parquet")),
    ]

    for source, cache_dir, group, expected in test_cases:
        result = _grouped_cache_path(cache_dir, group, source)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  {status}: {source.name} -> {result} (esperado: {expected})")

    return all_passed


def test_percent_calculation():
    """Testa o cálculo do percentual de progresso."""
    print("\n=== Teste: Cálculo de percentual ===")
    test_cases = [
        # (processed, total, esperado_percent)
        (0, 100, 0.0),
        (25, 100, 25.0),
        (50, 100, 50.0),
        (100, 100, 100.0),
        (0, 0, 0.0),  # divisão por zero
        (1, 3, 33.33),
        (2, 3, 66.67),
        (75, 200, 37.5),
    ]

    all_passed = True
    for processed, total, expected in test_cases:
        percent = (processed / total * 100) if total > 0 else 0
        status = "PASS" if abs(percent - expected) < 0.01 else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"  {status}: {processed}/{total} = {percent:.1f}% (esperado: {expected:.1f}%)")

    return all_passed


def test_log_format():
    """Testa a formatação do log de cache gravado."""
    print("\n=== Teste: Formatação do log ===")
    all_passed = True

    # Simula o log que seria emitido
    grupo = "sp"
    arquivo = "BIAC2401.parquet"
    parquet = "BIAC2401.parquet"
    particao = "2024/01"
    processed = 5
    total = 10
    percent = (processed / total * 100) if total > 0 else 0

    log_msg = (
        f"CACHE GRAVADO | grupo={grupo} | arquivo={arquivo} | parquet={parquet} "
        f"| partição={particao} | progresso={processed}/{total} ({percent:.1f}%)"
    )

    expected_parts = [
        "CACHE GRAVADO",
        f"grupo={grupo}",
        f"arquivo={arquivo}",
        f"parquet={parquet}",
        f"partição={particao}",
        f"progresso={processed}/{total} (50.0%)",
    ]

    for part in expected_parts:
        if part not in log_msg:
            status = "FAIL"
            all_passed = False
        else:
            status = "PASS"
        print(f"  {status}: Contém '{part}'")

    print(f"  Log completo: {log_msg}")
    return all_passed


def main():
    """Executa todos os testes."""
    print("=" * 60)
    print("TESTES DA LÓGICA DE LOGGING DO CACHE")
    print("=" * 60)

    results = []
    results.append(("test_extract_period_from_path", test_extract_period_from_path()))
    results.append(("test_file_period", test_file_period()))
    results.append(("test_grouped_cache_path", test_grouped_cache_path()))
    results.append(("test_percent_calculation", test_percent_calculation()))
    results.append(("test_log_format", test_log_format()))

    print("\n" + "=" * 60)
    print("RESUMO DOS TESTES")
    print("=" * 60)
    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"  {status}: {name}")

    print()
    if all_passed:
        print("✅ TODOS OS TESTES PASSARAM!")
        return 0
    else:
        print("❌ ALGUNS TESTES FALHARAM")
        return 1


if __name__ == "__main__":
    sys.exit(main())