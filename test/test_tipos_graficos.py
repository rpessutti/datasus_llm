import sys
sys.path.insert(0, '.')

"""
Teste de validação dos diferentes tipos de gráfico suportados pelo LLMGrafic.

Verifica:
1. Detecção automática de tipo de gráfico a partir da pergunta
2. Geração de prompts com tipo forçado
3. Execução de código matplotlib para cada tipo
4. Compatibilidade com chamadas anteriores
"""

import os
from typing import Any

from api.llm_grafic import LLMGrafic
from config.settings import Settings

TOTAL_TESTS = 0
PASSED_TESTS = 0
FAILED_TESTS = []


def log_test(nome: str, resultado: bool, detalhe: str = ""):
    """Registra resultado de teste."""
    global TOTAL_TESTS, PASSED_TESTS
    TOTAL_TESTS += 1
    if resultado:
        PASSED_TESTS += 1
        print(f"  ✓ {nome}" + (f" | {detalhe}" if detalhe else ""))
    else:
        FAILED_TESTS.append((nome, detalhe))
        print(f"  ✗ {nome}" + (f" | {detalhe}" if detalhe else ""))


# Dados de exemplo para os testes
DADOS_DISTRIBUICAO = [
    {"CID": "A00", "casos": 45},
    {"CID": "A01", "casos": 32},
    {"CID": "B20", "casos": 78},
    {"CID": "B22", "casos": 56},
    {"CID": "C34", "casos": 23},
    {"CID": "D10", "casos": 91},
]

DADOS_TEMPORAIS = [
    {"ano": "2020", "casos": 100},
    {"ano": "2021", "casos": 150},
    {"ano": "2022", "casos": 200},
    {"ano": "2023", "casos": 180},
    {"ano": "2024", "casos": 250},
]

DADOS_CATEGORIAS = [
    {"UF": "SP", "casos": 1520},
    {"UF": "RJ", "casos": 890},
    {"UF": "MG", "casos": 760},
    {"UF": "RS", "casos": 540},
    {"UF": "BA", "casos": 430},
    {"UF": "PR", "casos": 320},
    {"UF": "AM", "casos": 210},
    {"UF": "PA", "casos": 170},
    {"UF": "GO", "casos": 140},
    {"UF": "PE", "casos": 120},
    {"UF": "CE", "casos": 95},
    {"UF": "SC", "casos": 80},
]


def test_deteccao_tipo_grafico():
    """Testa a detecção automática do tipo de gráfico pela pergunta."""
    print("\n[1] Testes de detecção automática de tipo de gráfico")

    casos_teste = [
        # (pergunta, tipo_esperado)
        ("Crie um gráfico de pizza para distribuição de casos por CID", "pizza"),
        ("Quero ver uma pizza com a distribuição por sexo", "pizza"),
        ("Me mostre um pie chart dos diagnósticos", "pizza"),
        ("Faça um gráfico circular/torta", "pizza"),
        ("Preciso de um gráfico donut de procedimentos", "donut"),
        ("Crie um gráfico de rosca/anel", "donut"),
        ("Barras horizontais com os municípios", "horizontal"),
        ("Quero gráfico de barras laterais", "horizontal"),
        ("Me mostre deitado as categorias", "horizontal"),
        ("Gráfico de barras verticais com os valores", "vertical"),
        ("Criar colunas vertical para os estados", "vertical"),
        ("Gráfico de linha mostrando a evolução temporal", "linha"),
        ("Quero ver a tendência ao longo dos anos", "linha"),
        ("Um scatter plot das variáveis", "dispersao"),
        ("Gráfico de dispersão da correlação", "dispersao"),
        ("Mostre pontos para ver relação", "dispersao"),
        ("Gráfico de radar comparando indicadores", "radar"),
        # Sem menção a tipo - deve retornar None
        ("Liste os 10 estados com mais casos de dengue", None),
        ("Total de internações por ano", None),
        ("Compare as taxas entre as regiões", None),
    ]

    for pergunta, esperado in casos_teste:
        tipo = LLMGrafic.detectar_tipo_grafico(pergunta)
        if esperado is None:
            ok = tipo is None
        else:
            ok = tipo == esperado
        log_test(
            f"detectar({pergunta[:50]!r})",
            ok,
            detalhe=f"esperado={esperado}, obtido={tipo}",
        )


def test_deteccao_falsos_positivos():
    """Testa que palavras comuns em consultas de saúde não geram falsos positivos."""
    print("\n[2] Testes de falsos positivos na detecção de tipo")

    perguntas_nao_relacionadas = [
        "Qual a linha de cuidado para diabetes?",
        "Liste os atendimentos na linha de pesquisa",
        "Monte uma linha do tempo dos atendimentos",
        "Quantos casos de dengue tivemos em 2024?",
    ]

    for pergunta in perguntas_nao_relacionadas:
        tipo = LLMGrafic.detectar_tipo_grafico(pergunta)
        log_test(
            f"sem_falso_positivo({pergunta[:50]!r})",
            tipo is None,
            detalhe=f"tipo detectado: {tipo}",
        )


def test_prompt_contem_instrucoes_tipo():
    """Verifica que o prompt gerado contém instruções específicas para o tipo forçado."""
    print("\n[3] Testes do prompt com instruções de tipo")

    settings = Settings()
    llm_grafic = LLMGrafic(settings)

    casos_teste = [
        ("pizza", "ax.pie"),
        ("donut", "wedgeprops"),
        ("horizontal", "ax.barh"),
        ("vertical", "ax.bar"),
        ("linha", "ax.plot"),
        ("area", "fill_between"),
        ("dispersao", "ax.scatter"),
        ("radar", "polar"),
    ]

    for tipo, texto_esperado in casos_teste:
        prompt = llm_grafic._build_chart_prompt(
            DADOS_DISTRIBUICAO,
            "Distribuição de casos",
            tipo_grafico=tipo,
        )
        ok = "**INSTRUÇÃO DE TIPO FORÇADO**" in prompt
        ok = ok and "tipo_instruction" not in prompt
        ok = ok and texto_esperado in prompt
        log_test(
            f"prompt_tipo_{tipo}",
            ok,
            detalhe=f"conteúdo: {texto_esperado!r}",
        )

    # Teste sem tipo forçado
    prompt_sem_tipo = llm_grafic._build_chart_prompt(
        DADOS_DISTRIBUICAO,
        "Distribuição de casos",
        tipo_grafico=None,
    )
    ok = "**INSTRUÇÃO DE TIPO FORÇADO**" not in prompt_sem_tipo
    log_test("prompt_sem_tipo_nao_forcado", ok)


def test_execucao_diferentes_tipos():
    """Testa a execução de código matplotlib para cada tipo de gráfico."""
    print("\n[4] Testes de execução de código para diferentes tipos")

    settings = Settings()
    llm_grafic = LLMGrafic(settings)

    codigos_teste = {
        "pizza": (
            """
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(8, 8))
categorias = [d["CID"] for d in dados]
valores = [d["casos"] for d in dados]
ax.pie(valores, labels=categorias, autopct='%1.1f%%', colors=plt.cm.Set3.colors)
ax.set_title("Distribuição")
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
""",
            DADOS_DISTRIBUICAO,
        ),
        "donut": (
            """
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(8, 8))
categorias = [d["CID"] for d in dados]
valores = [d["casos"] for d in dados]
ax.pie(valores, labels=categorias, autopct='%1.1f%%', wedgeprops=dict(width=0.3))
ax.set_title("Donut")
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
""",
            DADOS_DISTRIBUICAO,
        ),
        "horizontal": (
            """
import matplotlib.pyplot as plt
categorias = [d["UF"] for d in dados]
valores = [d["casos"] for d in dados]
fig, ax = plt.subplots(figsize=(10, 8))
ax.barh(categorias, valores)
ax.set_title("Casos por UF")
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
""",
            DADOS_CATEGORIAS,
        ),
        "vertical": (
            """
import matplotlib.pyplot as plt
categorias = [d["CID"] for d in dados]
valores = [d["casos"] for d in dados]
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(categorias, valores)
ax.set_title("Casos")
ax.tick_params(axis='x', rotation=45)
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
""",
            DADOS_DISTRIBUICAO,
        ),
        "linha": (
            """
import matplotlib.pyplot as plt
anos = [d["ano"] for d in dados]
casos = [d["casos"] for d in dados]
fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(anos, casos, marker='o')
ax.set_title("Evolução")
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
""",
            DADOS_TEMPORAIS,
        ),
    }

    for tipo, (codigo, dados) in codigos_teste.items():
        try:
            resultado = llm_grafic._executar_codigo_grafico(codigo, dados, tipo_grafico=tipo)
            ok = resultado is not None
            log_test(
                f"executar_{tipo}",
                ok,
                detalhe=f"base64_len={len(resultado) if resultado else 0}",
            )
        except Exception as e:
            log_test(f"executar_{tipo}", False, detalhe=str(e))


def test_assinatura_metodo():
    """Verifica a assinatura do método gerar_grafico_llm."""
    print("\n[5] Teste de assinatura e dados vazios")

    import inspect

    settings = Settings()
    llm_grafic = LLMGrafic(settings)

    sig = inspect.signature(llm_grafic.gerar_grafico_llm)
    parametros = list(sig.parameters.keys())

    log_test(
        "tem_param_tipo",
        "tipo_grafico" in parametros,
        detalhe=f"parametros: {parametros}",
    )

    # Dados vazios -> deve retornar None imediatamente
    resultado = llm_grafic.gerar_grafico_llm([], "Teste", "pizza")
    log_test(
        "dados_vazios_retorna_none",
        resultado is None,
        detalhe="None esperado para dados vazios",
    )

    # Chance de retornar algo se API estiver disponível, mas não deve exceção
    try:
        resultado = llm_grafic.gerar_grafico_llm(
            DADOS_DISTRIBUICAO[:2],
            "Compare os casos por CID",
        )
        log_test(
            "chamada_compat_sem_tipo",
            True,  # deve rodar sem exceção
            detalhe=f"resultado_type={type(resultado).__name__}",
        )
    except Exception as e:
        log_test("chamada_compat_sem_tipo", False, detalhe=str(e))

    try:
        resultado = llm_grafic.gerar_grafico_llm(
            DADOS_DISTRIBUICAO[:2],
            "Compare os casos por CID",
            "pizza",
        )
        log_test(
            "chamada_compat_com_tipo",
            True,  # deve rodar sem exceção
            detalhe=f"resultado_type={type(resultado).__name__}",
        )
    except Exception as e:
        log_test("chamada_compat_com_tipo", False, detalhe=str(e))


def test_extracao_codigo():
    """Verifica a extração de código Python do LLM."""
    print("\n[6] Testes de extração de código Python")

    codigo_com_markdown = """
Aqui está o gráfico:

```python
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
plt.savefig('/tmp/llm_graph.jpg')
```
"""

    resultado = LLMGrafic._extract_python_code(codigo_com_markdown)
    log_test(
        "extrai_codigo_markdown",
        resultado is not None and "import matplotlib" in resultado,
        detalhe="deve extrair código dentro de ```python```",
    )

    codigo_sem_markdown = """
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
plt.savefig('/tmp/llm_graph.jpg')
"""

    resultado = LLMGrafic._extract_python_code(codigo_sem_markdown)
    log_test(
        "extrai_codigo_limpo",
        resultado is not None and "import matplotlib" in resultado,
        detalhe="deve extrair código sem markdown",
    )

    resultado = LLMGrafic._extract_python_code("")
    log_test(
        "codigo_vazio_retorna_none",
        resultado is None,
        detalhe="None para string vazia",
    )


def main():
    print("=" * 70)
    print("TESTE DE VALIDAÇÃO DE TIPOS DE GRÁFICO - LLMGrafic")
    print("=" * 70)
    print(f"Módulo: api/llm_grafic.py")
    print(f"Data: {os.popen('date').read().strip()}")

    test_functions = [
        test_deteccao_tipo_grafico,
        test_deteccao_falsos_positivos,
        test_prompt_contem_instrucoes_tipo,
        test_execucao_diferentes_tipos,
        test_assinatura_metodo,
        test_extracao_codigo,
    ]

    for func in test_functions:
        try:
            func()
        except Exception as e:
            print(f"\nERRO ao executar {func.__name__}: {e}")
            FAILED_TESTS.append((func.__name__, str(e)))

    print("\n" + "=" * 70)
    print("RESUMO DOS TESTES")
    print(f"  Total de testes:    {TOTAL_TESTS}")
    print(f"  Testes passados:    {PASSED_TESTS}")
    print(f"  Testes falhados:    {TOTAL_TESTS - PASSED_TESTS}")

    if FAILED_TESTS:
        print("\nTestes falhados:")
        for nome, detalhe in FAILED_TESTS:
            print(f"  ✗ {nome}: {detalhe}")

    if TOTAL_TESTS == PASSED_TESTS:
        print("\n✅ TODOS OS TESTES PASSARAM!")
    else:
        print("\n❌ ALGUNS TESTES FALHARAM")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
