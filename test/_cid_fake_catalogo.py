"""Catálogo CID-10 fake compartilhado pelos testes da P2.

Fornece um ``CidMap`` populado por ``_parse_rows`` (mesmo parser do loader real)
SEM abrir DuckDB, e o catálogo ``CATALOGO`` de (subcat, descricao, descabrev).

Importado por ``test_cid_discovery_full.py`` e ``test_cid_false_positive.py``
via inserção do diretório ``test/`` no ``sys.path`` (o diretório não é pacote).
"""

from __future__ import annotations

from db.cid_map import CidMap

# (subcat, descricao, descabrev) — representativo das doenças da P2.
CATALOGO = [
    # Dengue (A90 categoria + subcategorias)
    ("A90", "Dengue [dengue clássico]", "A90 Dengue"),
    ("A900", "Dengue clássico", "A90.0 Dengue clássico"),
    ("A91", "Febre hemorrágica devida ao vírus do dengue", "A91 Hemorrágica"),
    ("A910", "Dengue hemorrágico", "A91.0 Hemorrágico"),
    # Sarampo (categoria + subcategorias)
    ("B05", "Sarampo", "B05 Sarampo"),
    ("B050", "Sarampo complicado por encefalite", "B05.0 Encefalite"),
    ("B051", "Sarampo complicado por meningite", "B05.1 Meningite"),
    # Tuberculose (A15..A19)
    ("A15", "Tuberculose respiratória, com confirmação bacteriológica", "A15 Tuberculose"),
    ("A150", "Tuberculose pulmonar com confirmação", "A15.0 Pulmonar"),
    ("A16", "Tuberculose respiratória sem confirmação bacteriológica", "A16 Tuberculose"),
    ("A17", "Tuberculose do sistema nervoso", "A17 Tuberculose SNC"),
    ("A18", "Tuberculose de outros órgãos", "A18 Tuberculose"),
    ("A19", "Tuberculose miliar", "A19 Miliar"),
    # Hepatites (A/B/C distintas)
    ("B15", "Hepatite viral A", "B15 Hepatite A"),
    ("B16", "Hepatite viral B", "B16 Hepatite B"),
    ("B17", "Hepatite viral C", "B17 Hepatite C"),
    ("B170", "Hepatite C aguda", "B17.0 Aguda"),
    ("B18", "Hepatite viral crônica", "B18 Crônica"),
    ("B19", "Hepatite viral não especificada", "B19 Não especificada"),
    # Câncer de pele (C43 melanoma + C44 outras neoplasias de pele)
    ("C43", "Melanoma maligno da pele", "C43 Melanoma"),
    ("C430", "Melanoma maligno do lábio", "C43.0 Lábio"),
    ("C44", "Outras neoplasias malignas da pele", "C44 Neoplasia pele"),
    ("C440", "Neoplasia maligna da pele do lábio", "C44.0 Lábio"),
    # Outras topografias (NÃO devem entrar em "câncer de pele")
    ("C00", "Neoplasia maligna do lábio", "C00.0 Lábio"),
    ("C50", "Neoplasia maligna da mama", "C50 Mama"),
    # Ruído para testes negativos
    ("A67", "Pinta [carate]", "A67 Pinta"),
    # Doença multi-palavra (exige INTERSEÇÃO de termos): "esclerose múltipla"
    # só deve casar G35. As entradas abaixo casam apenas com UM dos termos e
    # NÃO devem aparecer no resultado (ruído de união).
    ("G35", "Esclerose múltipla", "G35 Esclerose multipla"),
    ("G12", "Atrofia muscular espinal", "G12 Atrofia muscular espinal"),
    ("E03", "Hipotireoidismo não especificado",
     "E03 Hipotireoidismo esclerose"),  # casa só 'esclerose'
    ("G99", "Neoplasia múltipla metastática",
     "G99 Neoplasia multipla"),  # casa só 'multipla'
    ("M10", "Gota", "M10 Gota"),
    ("M100", "Gota idiopática", "M10.0 Gota idiopatica"),
    # "degeneração macular": a pergunta usa o ADJETIVO ("macular") e a descrição
    # usa o SUBSTANTIVO ("mácula"). Exige a variante adjetivo→substantivo
    # (macular→macula) E o casamento por PREFIXO do segmento (não substring solta).
    ("H353", "Degeneração da mácula e do polo posterior",
     "H35.3 Degeneracao da macula"),
    # Ruído: casa só 'degeneracao' (termo COMUM) — NÃO deve entrar em
    # "degeneração macular" (o termo RARO 'macular' deve dominar o fallback).
    ("I515", "Degeneração miocárdica", "I51.5 Degeneracao miocardica"),
    ("G311", "Degeneração cerebral senil", "G31.1 Degeneracao cerebral senil"),
    # "cistite": prefixo 'cistite' casa 'cistites' (plural), mas NÃO deve casar
    # 'colecistite' (vesícula) — bug de over-matching corrigido pelo prefixo.
    ("N30", "Cistite", "N30 Cistite"),
    ("N300", "Cistite aguda", "N30.0 Cistite aguda"),
    ("N301", "Cistite intersticial (crônica)", "N30.1 Cistite intersticial"),
    ("K81", "Colecistite", "K81 Colecistite"),
    ("K810", "Colecistite aguda", "K81.0 Colecistite aguda"),
    # "sepse": termo COLOQUIAL que NÃO aparece literalmente nas descrições
    # ("Septicemia") — exige o alias curado.
    ("A40", "Septicemia estreptocócica", "A40 Septicemia estreptococica"),
    ("A41", "Septicemia, outras", "A41 Septicemia"),
    ("A419", "Septicemia não especificada", "A41.9 Septicemia nao especificada"),
    # Sintomas coloquiais (códigos R): a descrição oficial usa o termo TÉCNICO
    # ("Dispneia", "Cefaleia"), então a busca por termo NÃO casa "falta de ar"
    # nem "dor de cabeça" — exige alias curado.
    ("R060", "Dispneia", "R06.0 Dispneia"),
    ("R51", "Cefaleia", "R51 Cefaleia"),
    ("R05", "Tosse", "R05 Tosse"),
    ("R11", "Náusea e vômitos", "R11 Nausea e vomitos"),
    ("R42", "Tontura e instabilidade", "R42 Tontura"),
    # Sinônimos populares de doenças
    ("B01", "Varicela [catapora]", "B01 Varicela"),
    ("B011", "Varicela com meningite", "B01.1 Meningite"),
    ("B68", "Infestação por Taenia", "B68 Teniase"),
    ("K80", "Calculose da vesícula biliar", "K80 Calculose biliar"),
    ("K800", "Calculose da vesícula biliar com colicistite aguda", "K80.0"),
    ("E10", "Diabetes mellitus tipo 1", "E10 Diabetes tipo 1"),
    ("E11", "Diabetes mellitus tipo 2", "E11 Diabetes tipo 2"),
    # Ruído: "Falta de alimento" casa só o termo 'falta' (de "falta de ar"),
    # NÃO deve vazar para a descoberta de dispneia.
    ("X530", "Falta de alimento - residência", "X53 Falta de alimento"),
]


def catalogo_map() -> CidMap:
    """Retorna um ``CidMap`` carregado com ``CATALOGO`` (sem DuckDB)."""
    cm = CidMap()
    cm._entries = cm._parse_rows(CATALOGO)
    cm._by_norm_desc = {}
    for entry in cm._entries:
        for termo in entry.termos:
            cm._by_norm_desc.setdefault(termo, entry)
    cm._loaded = True
    return cm
