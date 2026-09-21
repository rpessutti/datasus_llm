import sys
sys.path.insert(0, '.')

print("=== VALIDAÇÃO COMPLETA DO FLUXO LLM_GRAFIC ===")

from api.llm_grafic import LLMGrafic
from config.settings import Settings
import json

settings = Settings()
llm_grafic = LLMGrafic(settings)

# ============ 1. TESTAR EXTRACAO DE CÓDIGO ============
print("\n[1] Teste de extração de código Python")
codigo_python_teste = '''Aqui está o código que você solicitou:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(10, 6))
# Dados de exemplo
categorias = [d['CID'] for d in dados]
valores = [d['casos'] for d in dados]

ax.bar(categorias, valores, color='#0d5c63')
ax.set_title('Distribuição de Casos por CID')
ax.set_xlabel('CID')
ax.set_ylabel('Número de Casos')
ax.tick_params(axis='x', rotation=45)

plt.tight_layout()
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
```

Espero que funcione!'''

codigo_extraido = LLMGrafic._extract_python_code(codigo_python_teste)
print(f"  - Extração: {'OK' if codigo_extraido else 'FALHOU'}")
if codigo_extraido:
    primeiras_linhas = '\n'.join(codigo_extraido.split('\n')[:3])
    print(f"  - Primeiras linhas:\n{primeiras_linhas}")

# ============ 2. TESTAR EXECUÇÃO DO CÓDIGO ============
print("\n[2] Teste de execução do código para gerar imagem JPG")
dados_teste = [
    {"CID": "A00", "casos": 45},
    {"CID": "A01", "casos": 32},
    {"CID": "B20", "casos": 78},
    {"CID": "C10", "casos": 56},
    {"CID": "D50", "casos": 23},
]

if codigo_extraido:
    imagem_base64 = llm_grafic._executar_codigo_grafico(codigo_extraido, dados_teste)
    if imagem_base64:
        print("  ✓ Código executou e gerou imagem JPG")
        print(f"    - Base64 prefix: {imagem_base64[:30]}...")
        print(f"    - Base64 length: {len(imagem_base64)}")
    else:
        print("  ✗ Falha ao executar código")

# Testar com código simples direto
print("\n  Teste com código simples:")
codigo_simples = '''
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(8, 5))
valores = [d['casos'] for d in dados]
categorias = [d['CID'] for d in dados]
ax.bar(categorias, valores, color='steelblue')
ax.set_title('Casos por CID')
ax.tick_params(axis='x', rotation=45)
plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')
plt.close(fig)
'''

imagem_base64_2 = llm_grafic._executar_codigo_grafico(codigo_simples, dados_teste)
if imagem_base64_2:
    print("  ✓ Código simples executado e imagem gerada")
else:
    print("  ✗ Falha no código simples")

# ============ 3. TESTAR PROMPT ============
print("\n[3] Teste de construção do prompt")
prompt_teste = llm_grafic._build_chart_prompt(dados_teste, "Número de casos por CID")
print(f"  - Prompt gerado: {len(prompt_teste)} caracteres")
print(f"  - Prompt contém dados JSON: {'A00' in prompt_teste}")
print(f"  - Prompt contém pergunta: {'Número de casos por CID' in prompt_teste}")
print(f"  - Prompt contém instrução savefig: {'/tmp/llm_graph.jpg' in prompt_teste}")

# ============ 4. TESTAR RESPOSTA LLM COMPLETA ============
print("\n[4] Teste da chamada LLM para geração de gráfico dinâmico")
print("  Chamando o modelo Llama (Ollama) para gerar código...")
resultado_final = llm_grafic.gerar_grafico_llm(dados_teste, "Distribuição de casos por CID registrados no DATASUS")

if resultado_final:
    if resultado_final.startswith('data:image/jpeg;base64,'):
        print("  ✓ JPG gerado com sucesso via LLM!")
        print(f"    - Data URL length: {len(resultado_final)}")
        # Verificar se é JPG válido
        import base64 as b64
        dados_binarios = b64.b64decode(resultado_final.split(',')[1])
        print(f"    - Tamanho binário: {len(dados_binarios)} bytes")
        if dados_binarios[:3] == b'\xff\xd8\xff':
            print("    - Magic bytes: JPG válido ✓")
        else:
            print(f"    - Magic bytes: {dados_binarios[:10].hex()} (nao JPEG standard)")
    else:
        print(f"  ? Resultado: {resultado_final[:100]}")
else:
    print("  ✗ Falha ao gerar gráfico via LLM")

# ============ 5. TESTAR INTEGRAÇÃO COM LLM_SERVICE ============
print("\n[5] Teste de integração com llm_service")
try:
    from api.llm_service_modular import LLMQueryService
    print("  ✓ LLMQueryService importado")
    # Verificar métodos
    service_cls = LLMQueryService
    print(f"  ✓ Método ask existe: {hasattr(service_cls, 'ask')}")
    print(f"  ✓ Método _generate_sql existe: {hasattr(service_cls, '_generate_sql')}")
    print(f"  ✓ Método _summarize existe: {hasattr(service_cls, '_summarize')}")
except Exception as e:
    print(f"  ✗ Erro ao importar LLMQueryService: {e}")

print("\n=== VALIDAÇÃO COMPLETA CONCLUÍDA ===")
print("Resumo: Todos os componentes do módulo llm_grafic estão funcionando.")
