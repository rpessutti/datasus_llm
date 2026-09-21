import sys
sys.path.insert(0, '.')

from api.llm_service_modular import LLMQueryService

# Testa a função _extract_limit_from_question
testes = [
    'quais os 10 municipios com mais diagnosticos de cancer de pele nos ultimos 2 anos ?',
    'quais os 5 estados com mais casos de dengue no ultimo ano ?',
    'quais as 20 cidades com mais registros de covid-19 em 2024 ?',
    'quais os 3 primeiros medicamentos mais prescritos ?',
    'top 15 municipios com mais casos de diabetes',
    'quantos casos de cancer de pulmao existem em 2024 ?',
    'quais os municipios com mais casos de cancer de mama ?',
]

for pergunta in testes:
    limite = LLMQueryService._extract_limit_from_question(pergunta)
    print(f'{pergunta!r} -> limite={limite}')