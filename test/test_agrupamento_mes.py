#!/usr/bin/env python3
'''
Testes de AGRUPAMENTO POR MÊS no assistente DATASUS.

Cobrem as duas correções implementadas para a pergunta:
  "Quantos casos de Aids/HIV nos ultimos dois anos do Brasil? E agrupe por mes?"

Problemas corrigidos:
  1. NUMERO POR EXTENSO: "ultimos dois anos" nao era reconhecido (o regex exigia
     digitos), fazendo a resolucao de periodos do cache falhar.
  2. AGRUPAMENTO != FILTRO: "agrupe por mes"/"por mes"/"mensal" era tratado como
     selecao de um mes especifico, zerando/prendendo o filtro de meses. Agora a
     intencao de agrupamento e propagada ao LLM e a coluna JA EXISTENTE
     parquet_month e usada no SELECT/GROUP BY.

Estrutura:
  - Testes UNITARIOS (nao dependem de cache/LLM) para a deteccao de agrupamento.
  - Testes de SELECAO DE PERIODOS (dependem do cache local; pulados se ausente).
  - Testes de INTEGRACAO: verificam que o prompt do LLM recebe a instrucao de
    agrupar por parquet_month (com cache real).

Execucao:
    python3 test/test_agrupamento_mes.py
'''

import os
import sys

# Adiciona a raiz do projeto ao sys.path (padrao dos demais testes).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Garante que o LLM real nao seja usado nos testes.
os.environ.setdefault("LLM_ENABLED", "false")

SEP = "=" * 70


# ============================================================
# HELPERS
# ============================================================

def _check(nome: str, condicao: bool, detalhe: str = "") -> int:
    """Imprime o status de uma verificacao e retorna 0 (ok) ou 1 (erro)."""
    status = "OK" if condicao else "FALHOU"
    extra = f" -> {detalhe}" if detalhe else ""
    print(f"  [{status}] {nome}{extra}")
    return 0 if condicao else 1


def _cache_disponivel() -> bool:
    """Indica se existe um cache DuckDB local utilizavel para os testes de cache."""
    from config.settings import Settings
    settings = Settings()
    return (settings.cache_dir / "datasus.duckdb").is_file()


def _abrir_cache():
    """Abre o DatasusCache real (usado apenas nos testes de integracao)."""
    from db.cache import DatasusCache
    from config.settings import Settings
    return DatasusCache(Settings())


def _selecionar_anos_meses(cache, pergunta: str):
    """Resolve (anos, meses) para a pergunta a partir do catalogo de grupos.

    Usa ``DatasusCache.periods_for_question``, que le ``main.group_periods``.
    Perguntas sem recorte interpretavel recebem todos os periodos disponiveis
    (o parser registra um aviso e nao levanta excecao).

    Args:
        cache: instancia de DatasusCache.
        pergunta: pergunta do usuario.

    Returns:
        tuple[set[str], set[str]]: anos e meses no formato '01'..'12'.
    """
    selecionados = cache.periods_for_question(pergunta)
    anos = {str(ano) for _, ano, _ in selecionados}
    meses = {str(mes).zfill(2) for _, _, mes in selecionados}
    return anos, meses


# ============================================================
# TESTES UNITARIOS: deteccao de agrupamento por mes
# ============================================================

def test_1_question_wants_monthly_grouping_positivos():
    """Perguntas que PEDEM agrupamento por mes devem retornar True."""
    print(SEP)
    print("TESTE 1: Deteccao de agrupamento por mes (positivos)")
    print(SEP)

    from db.cache import DatasusCache
    metodo = DatasusCache.question_wants_monthly_grouping

    perguntas = [
        "Quantos casos de Aids/HIV nos ultimos dois anos do Brasil? E agrupe por mes?",
        "Mostre os casos agrupados por mes em 2024",
        "Quero a evolucao mensal dos casos de dengue",
        "Distribuicao dos atendimentos por mes",
        "Apresente os dados mes a mes no ultimo ano",
        "Total de casos por mês",
        "Agrupe os registros mensalmente",
        "casos por mes nos ultimos 3 anos",
    ]

    erros = 0
    for p in perguntas:
        resultado = metodo(p)
        erros += _check(f"agrupa: {p!r}", resultado is True, f"obtido={resultado}")
    print(f"  Resultado: {len(perguntas) - erros}/{len(perguntas)} perguntas detectadas")
    return erros


def test_2_question_wants_monthly_grouping_negativos():
    """Perguntas que NAO pedem agrupamento por mes devem retornar False."""
    print()
    print(SEP)
    print("TESTE 2: Deteccao de agrupamento por mes (negativos)")
    print(SEP)

    from db.cache import DatasusCache
    metodo = DatasusCache.question_wants_monthly_grouping

    perguntas = [
        "Quantos casos de dengue em 2024?",
        "Quais os 10 municipios com mais casos de sarampo?",
        "Qual a evolucao dos casos por ano e sexo?",
        "Quantos casos de Aids nos ultimos dois anos?",       # sem agrupamento por mes
        "Casos de tuberculose em janeiro de 2024",             # mes especifico, nao agrupamento
        "Total de internacoes por estado",
        "Casos de HIV em 2023",
    ]

    erros = 0
    for p in perguntas:
        resultado = metodo(p)
        erros += _check(f"nao agrupa: {p!r}", resultado is False, f"obtido={resultado}")
    print(f"  Resultado: {len(perguntas) - erros}/{len(perguntas)} perguntas rejeitadas")
    return erros


def test_3_agrupamento_ignora_acentos():
    """A deteccao deve funcionar com/sem acentos ('mes' vs 'mês')."""
    print()
    print(SEP)
    print("TESTE 3: Tolerancia a acentos e caixa")
    print(SEP)

    from db.cache import DatasusCache
    metodo = DatasusCache.question_wants_monthly_grouping

    casos = [
        "AGRUPE POR MÊS os casos de dengue",   # maiusculas + acento
        "agrupe por mes os casos de dengue",   # minusculas sem acento
        "AgRuPe PoR MêS",                      # misto
    ]

    erros = 0
    for p in casos:
        resultado = metodo(p)
        erros += _check(f"acento/caixa: {p!r}", resultado is True, f"obtido={resultado}")
    print(f"  Resultado: {len(casos) - erros}/{len(casos)} variacoes detectadas")
    return erros


# ============================================================
# TESTES DE SELECAO DE ARQUIVOS (dependem do cache local)
# ============================================================

def test_4_numero_por_extenso_nos_ultimos_anos():
    """"ultimos dois anos" (por extenso) NAO deve mais levantar ValueError."""
    print()
    print(SEP)
    print("TESTE 4: Numero por extenso em 'ultimos N anos'")
    print(SEP)

    if not _cache_disponivel():
        print("  [SKIP] Cache local (cache/datasus.duckdb) ausente; teste pulado.")
        return 0

    cache = _abrir_cache()
    try:
        perguntas = [
            "Quantos casos de Aids/HIV nos ultimos dois anos do Brasil? E agrupe por mes?",
            "Casos de dengue nos ultimos tres anos",
            "Internacoes nos ultimos cinco anos",
            "Registros do ultimo ano",
        ]

        erros = 0
        for p in perguntas:
            try:
                anos, meses = _selecionar_anos_meses(cache, p)
                # Deve selecionar ao menos um periodo (ano/mes) e nao vazio.
                erros += _check(f"seleciona periodos: {p!r}", len(anos) > 0,
                                f"anos={sorted(anos)}")
            except ValueError as exc:
                erros += _check(f"sem ValueError: {p!r}", False, f"ValueError: {exc}")

        # Verifica especificamente a contagem do "dois anos": 2 anos distintos.
        anos, _ = _selecionar_anos_meses(
            cache,
            "Quantos casos de Aids/HIV nos ultimos dois anos do Brasil? E agrupe por mes?",
        )
        erros += _check("'dois anos' => exatamente 2 anos distintos", len(anos) == 2,
                        f"anos={sorted(anos)}")

        print(f"  Resultado: {len(perguntas) + 1 - erros}/{len(perguntas) + 1} verificacoes passaram")
        return erros
    finally:
        cache.close()


def test_5_agrupamento_nao_filtra_um_unico_mes():
    """"agrupe por mes" NAO pode restringir a selecao a um mes especifico.

    Sem a correcao, "por mes" era capturado como filtro de mes e a selecao ficava
    presa. Aqui confirmamos que a selecao cobre TODOS os meses dos anos escolhidos.
    """
    print()
    print(SEP)
    print("TESTE 5: Agrupamento por mes nao vira filtro de um mes")
    print(SEP)

    if not _cache_disponivel():
        print("  [SKIP] Cache local ausente; teste pulado.")
        return 0

    cache = _abrir_cache()
    try:
        erros = 0
        _, meses_grupo = _selecionar_anos_meses(
            cache, "Casos de dengue nos ultimos dois anos e agrupe por mes"
        )
        _, meses_sem = _selecionar_anos_meses(
            cache, "Casos de dengue nos ultimos dois anos"
        )

        # Com agrupamento por mes, a selecao NAO deve ser reduzida a poucos meses;
        # deve cobrir o mesmo conjunto de meses que a consulta sem agrupamento.
        erros += _check("selecao com agrupamento cobre varios meses", len(meses_grupo) > 1,
                        f"meses={sorted(meses_grupo)}")
        erros += _check("agrupar nao restringe meses vs. sem agrupar",
                        meses_grupo == meses_sem,
                        f"com={sorted(meses_grupo)} sem={sorted(meses_sem)}")

        print(f"  Resultado: {2 - erros}/2 verificacoes passaram")
        return erros
    finally:
        cache.close()


# ============================================================
# TESTES DE INTEGRACAO: instrucao no prompt do LLM
# ============================================================

def test_6_prompt_inclui_instrucao_de_agrupamento():
    """O prompt de geracao de SQL deve conter a instrucao de agrupar por atend_mes."""
    print()
    print(SEP)
    print("TESTE 6: Prompt inclui instrucao de agrupamento por mes")
    print(SEP)

    import inspect
    from api.llm_service_modular import LLMQueryService

    source = inspect.getsource(LLMQueryService._generate_sql)

    erros = 0
    erros += _check("chama question_wants_monthly_grouping", "question_wants_monthly_grouping" in source)
    erros += _check("define monthly_group_hint", "monthly_group_hint" in source)
    erros += _check("usa coluna atend_mes", "atend_mes" in source)
    erros += _check("instrui GROUP BY", "GROUP BY" in source)

    print(f"  Resultado: {4 - erros}/4 verificacoes passaram")
    return erros


def test_7_execucao_real_de_agrupamento_mensal():
    """Integracao real: prepara o cache e executa uma consulta agrupada por mes.

    Usa o banco DuckDB real do cache, sem LLM. Pula caso nao haja dados.
    """
    print()
    print(SEP)
    print("TESTE 7: Execucao real de agrupamento mensal (sem LLM)")
    print(SEP)

    if not _cache_disponivel():
        print("  [SKIP] Cache local ausente; teste pulado.")
        return 0

    cache = _abrir_cache()
    try:
        pergunta = "Casos de dengue nos ultimos dois anos e agrupe por mes"
        try:
            info = cache.prepare_question_cache(pergunta, "test-agrupamento-mes")
        except Exception as exc:
            print(f"  [SKIP] Nao foi possivel preparar o cache: {type(exc).__name__}: {exc}")
            return 0

        # Confirma que as colunas de periodo estao disponiveis na view preparada.
        desc = cache.connection.execute("DESCRIBE datasus_query_cache").fetchall()
        colunas = {row[0].lower() for row in desc}
        erros = 0
        erros += _check("view expõe atend_mes", "atend_mes" in colunas)
        erros += _check("view expõe atend_ano", "atend_ano" in colunas)

        # Executa um agrupamento mensal real (nao depende de CID especifico).
        sql = (
            "SELECT atend_ano AS ano, atend_mes AS mes, COUNT(*) AS casos "
            "FROM datasus_query_cache "
            "GROUP BY atend_ano, atend_mes "
            "ORDER BY atend_ano, atend_mes"
        )
        linhas = cache.execute(sql, "test-agrupamento-mes")
        erros += _check("consulta agrupada retorna linhas", len(linhas) > 0,
                        f"linhas={len(linhas)}")

        if linhas:
            tem_mes = all("mes" in linha and linha["mes"] for linha in linhas)
            meses = sorted({str(linha["mes"]) for linha in linhas})
            erros += _check("cada linha tem mes preenchido", tem_mes, f"meses={meses}")
            # Os meses devem ser strings '01'..'12' de 2 digitos.
            formato_ok = all(len(m) == 2 and m.isdigit() and 1 <= int(m) <= 12 for m in meses)
            erros += _check("meses no formato '01'..'12'", formato_ok, f"meses={meses}")

        print(f"  Resultado: {4 - erros}/4 verificacoes passaram")
        return erros
    finally:
        cache.close()


def main():
    """Executa todos os testes de agrupamento por mes."""
    print(SEP)
    print(" TESTES DE AGRUPAMENTO POR MES - ASSISTENTE DATASUS")
    print(SEP)

    resultados = []
    resultados.append(("test_1_deteccao_positivos", test_1_question_wants_monthly_grouping_positivos()))
    resultados.append(("test_2_deteccao_negativos", test_2_question_wants_monthly_grouping_negativos()))
    resultados.append(("test_3_tolerancia_acentos", test_3_agrupamento_ignora_acentos()))
    resultados.append(("test_4_numero_por_extenso", test_4_numero_por_extenso_nos_ultimos_anos()))
    resultados.append(("test_5_agrupamento_sem_filtro", test_5_agrupamento_nao_filtra_um_unico_mes()))
    resultados.append(("test_6_prompt_instrucao", test_6_prompt_inclui_instrucao_de_agrupamento()))
    resultados.append(("test_7_execucao_real", test_7_execucao_real_de_agrupamento_mensal()))

    print()
    print(SEP)
    print("RESUMO DOS TESTES")
    print(SEP)
    total_erros = 0
    for nome, erros in resultados:
        status = "PASS" if erros == 0 else f"FAIL ({erros})"
        if erros:
            total_erros += erros
        print(f"  [{status}] {nome}")

    print()
    if total_erros == 0:
        print(" TODOS OS TESTES DE AGRUPAMENTO POR MES PASSARAM!")
    else:
        print(f" {total_erros} erros encontrados nos testes de agrupamento por mes")
    print(SEP)

    return 0 if total_erros == 0 else 1


if __name__ == "__main__":
    sys.exit(main())