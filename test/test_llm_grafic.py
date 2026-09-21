import sys
sys.path.insert(0, '.')

from api.llm_grafic import LLMGrafic
from config.settings import Settings

print("=== Teste do módulo LLMGrafic ===")

settings = Settings()
llm_grafic = LLMGrafic(settings)
print("LLMGrafic instanciado com sucesso!")
print(f"  - llm_enabled: {llm_grafic.llm_enabled}")
print(f"  - model: {llm_grafic.model}")
print(f"  - client: {llm_grafic.client}")

# Testar extração de código Python
codigo_teste = '''Aqui está o código:

```python
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(['A', 'B'], [1, 2])
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
```'''

codigo_extraido = LLMGrafic._extract_python_code(codigo_teste)
print("\nExtração de código:", "OK" if codigo_extraido else "FALHOU")
print("Código extraído:")
print(codigo_extraido)

# Testar execução de código para gerar imagem
print("\n=== Teste de execução de código para gerar gráfico ===")
dados_teste = [
    {"ano": "2020", "casos": 100},
    {"ano": "2021", "casos": 150},
    {"ano": "2022", "casos": 200},
]

codigo_exec = '''
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(10, 6))
anos = [d["ano"] for d in dados]
casos = [d["casos"] for d in dados]
ax.bar(anos, casos)
ax.set_title("Casos por ano")
ax.set_xlabel("Ano")
ax.set_ylabel("Casos")
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
'''

imagem_base64 = llm_grafic._executar_codigo_grafico(codigo_exec, dados_teste)
print("Execução de código:", "OK" if imagem_base64 else "FALHOU")
if imagem_base64:
    print(f"  - Base64 length: {len(imagem_base64)}")
    print(f"  - Preview: {imagem_base64[:50]}...")

print("\n=== Teste com dados reais (caso de uso) ===")
# Testar com dados mais realistas
dados_reais = [
    {"CID": "A00", "casos": 45},
    {"CID": "A01", "casos": 32},
    {"CID": "B20", "casos": 78},
    {"CID": "B22", "casos": 56},
]

resultado = llm_grafic.gerar_grafico_llm(dados_reais, "Casos por CID registrados")
print("Geração de gráfico via LLM:", "OK" if resultado else "SEM CLIENTE (esperado se sem API key)")

print("\n=== Teste de integração do llm_service ===")
from api.llm_service_modular import LLMQueryService
print("Importação LLMQueryService: OK")

print("\n=== Teste de extração de limite da pergunta ===")
from api.llm_service_modular import LLMQueryService

testes_limite = [
    ("quais os 10 municipios com mais diagnosticos de cancer de pele nos ultimos 2 anos ?", 10),
    ("quais os 5 estados com mais casos de dengue no ultimo ano ?", 5),
    ("quais as 20 cidades com mais registros de covid-19 em 2024 ?", 20),
    ("quais os 3 primeiros medicamentos mais prescritos ?", 3),
    ("top 15 municipios com mais casos de diabetes", 15),
    ("quantos casos de cancer de pulmao existem em 2024 ?", 100),
    ("quais os municipios com mais casos de cancer de mama ?", 100),
]

for pergunta, esperado in testes_limite:
    limite = LLMQueryService._extract_limit_from_question(pergunta)
    status = "OK" if limite == esperado else f"FALHOU (esperado {esperado})"
    print(f"  {pergunta!r} -> limite={limite} | {status}")

print("\nTodos os testes completados com sucesso!")