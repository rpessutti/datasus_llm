import sys
sys.path.insert(0, '.')

print("=== Teste de integração completa: LLMService + LLMGrafic ===")

from api.routes import cache, service

# Verificar se o cache está pronto
print(f"Cache pronto: {cache.is_ready()}")

# Se não estiver pronto, inicializar manualmente
if not cache.is_ready():
    print("Inicializando cache...")
    try:
        cache.ingest()
        cache.mark_ready()
        print("Cache inicializado!")
    except Exception as e:
        print(f"Erro ao inicializar cache: {e}")
        # Usar dados de teste direto
        print("Usando dados de teste mock para validar o fluxo")
        dados_mock = [
            {"ano": "2019", "casos": 120},
            {"ano": "2020", "casos": 145},
            {"ano": "2021", "casos": 180},
            {"ano": "2022", "casos": 210},
        ]
        # Testar o LLMGrafic com dados mock
        resultado = service.llm_grafic.gerar_grafico_llm(dados_mock, "Casos por ano no DATASUS")
        if resultado and resultado.startswith('data:image/jpeg;base64,'):
            print("✓ Gráfico JPG gerado via LLM com dados mock!")
            print(f"  - Tamanho do base64: {len(resultado)} caracteres")
        else:
            print("✗ Falha ao gerar gráfico com dados mock")
        sys.exit(0)

print(f"Cache pronto: {cache.is_ready()}")
print(f"Files: {cache.status().get('files')}")

# Testar consulta real
pergunta = "Quantos registros existem por ano para diabetes?"
print(f"\nPergunta: {pergunta}")

try:
    resultado = service.ask(pergunta, "test_integracao_001")
    print(f"\nResposta:")
    print(f"  - Answer: {resultado['answer'][:200]}..." if len(resultado['answer']) > 200 else f"  - Answer: {resultado['answer']}")
    print(f"  - SQL: {resultado['sql']}")
    print(f"  - Source: {resultado['source']}")
    print(f"  - Rows: {len(resultado['results'])}")
    
    if resultado.get('chart'):
        chart = resultado['chart']
        if chart.startswith('data:image/jpeg;base64,'):
            print(f"  ✓ Chart: JPG gerado via LLM dinâmico! (base64_len={len(chart)})")
        elif chart.startswith('data:image/png;base64,'):
            print(f"  ✓ Chart: PNG padrão (fallback) (base64_len={len(chart)})")
        else:
            print(f"  ? Chart: formato desconhecido: {chart[:50]}")
    else:
        print(f"  - Chart: None (sem dados)")
        
except Exception as e:
    print(f"Erro na consulta: {e}")
    import traceback
    traceback.print_exc()

print("\n=== Teste completo ===")
print("Teste de integração concluído!")
