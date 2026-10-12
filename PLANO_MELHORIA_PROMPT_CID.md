# Plano de Implementação — Prompt Enxuto + Acurácia de Coluna/CID (sem VSS/HNSW)

> **Status:** PLANO REVISADO. **FASES P0-A, P0-B, P2, P3 e P4 IMPLEMENTADAS** (ver "Status de
> execução" abaixo); resta apenas a **P1** — semântica de grupo, **OPCIONAL** e desligada por
> default (`DATASUS_GROUP_SEMANTIC_ENABLED=false`) até haver avaliação rotulada.
> Documento para execução.
> **Substitui** o antigo plano de VSS/HNSW (`PLANO_VSS_BGE_M3.md`).
> **Revisão motivada por:** análise crítica do código real (`generate.py`, `prompt.py`,
> `cid.py`, `cid_map.py`, `cache.py`, `column_dictionary.py`, `column_comments.py`),
> que mostrou que (a) o gargalo de acurácia é a escolha de COLUNA e CID, não de grupo;
> (b) o HNSW persistente experimental + modelo de 568M em CPU introduzem risco e latência
> desproporcionais ao ganho; e (c) os comentários de coluna — que o plano antigo elegeu
> como "fonte textual dos embeddings" — hoje **nem chegam ao prompt**.

---

## Status de execução

| Fase | Status | Entregáveis |
|---|---|---|
| **P0-A — comentários de coluna no prompt** | ✅ **IMPLEMENTADA** | ver abaixo |
| **P0-B — priorização lexical** | ✅ **IMPLEMENTADA** | `db/lexical_rank.py` + integração em `generate.py`/`column_dictionary.py`; ver abaixo |
| P1 — semântica de grupo (opcional) | ⏳ pendente | — |
| P2 — CID completo + anti-falso-positivo | ✅ **IMPLEMENTADA** | `_discover_cid_full` + `CidMap.identificacao_de_alias`/`familia_completa`/`termos_doenca`/`entries_por_termos`; ver abaixo |
| P3 — `core/safe_io.py` | ✅ implementado (pré-requisito) | `core/safe_io.py` + `test/test_safe_io.py` (6 testes) |
| **P4 — suite de testes determinística** | ✅ **IMPLEMENTADA** | `pytest.ini` + `test/conftest.py` (`collect_ignore` dos scripts manuais) + testes novos por fase; **200 testes coletados** em ~0,35 s, **sem `INTERNALERROR`** — ver `PLANO_P4_SUITE_TESTES.md` |

> **⚠️ CORREÇÃO DE DADOS (2026-10-12) — cache dos grupos estava DUPLICADO (2×).**
> Cada Parquet entrou **duas vezes** em `<grupo>.datasus_cache` (uma com o caminho Linux
> antigo, outra com o caminho Windows atual) e o `file_manifest` tinha 2× entradas, o que
> **dobrava COUNT/SUM** de toda resposta que passasse pelos grupos (a view unificada é
> `UNION ALL` filtrando por período, e as duas cópias têm o mesmo período).
> Reparo (delete in-place + limpeza de manifesto + guarda de idempotência na ingestão) em
> **`PLANO_REPARO_CACHE_DUPLICADO.md`** e `dev_tools/repair_group_cache_duplication.py`.
> **Consequência prática:** qualquer telemetria de acurácia/contagem medida ANTES do reparo
> está inflada em 2× e deve ser descartada ou reescalada.

**P0-A — o que foi entregue:**

- `db/column_dictionary.py`: campo `comments` (COLUNA_UPPER → texto), `_load_comments(...)`
  (best-effort via `column_comments.load_layout`), `comment_for_column(col)`,
  `labeled_columns(...)`/`labeled_columns_sql_hint(...)` com `with_comments` e
  `max_comment_chars`. A chave do `ColumnDictionaryStore` inclui o `cache_dir` (evita servir
  um dicionário sem comentários a uma chamada que pediu comentários).
- `db/cache.py`: os 4 pontos que criam o dicionário passam `cache_dir=str(settings.cache_dir)`.
- `api/llm_service_parts/generate.py`: as 2 chamadas de `labeled_columns_sql_hint` propagam
  `with_comments=settings.schema_comments_enabled` e
  `max_comment_chars=settings.schema_comment_max_chars`. No **fallback** (sem grupo
  identificado), o `limit_per_group` foi reduzido de **40 → 25** (igual ao caminho
  principal): com os comentários cada entrada fica ~3x maior, e 40 colunas/grupo em vários
  grupos inflaria demais o prompt (risco de reativar o `APITimeoutError`).
- `config/settings.py`: `schema_comments_enabled` (default `true`),
  `schema_comment_max_chars` (default `90`).
- **Retrocompatível:** se os layouts (`<cache_dir>/layout_csv/<grupo>.csv`) não existirem, o
  comportamento é idêntico ao anterior (nenhum comentário).
- Testes: `test/test_schema_comments_prompt.py` (10 testes) cobrindo dicionário, `generate.py`
  (propagação liga/desliga), `db/cache.py` (repasse do `cache_dir`) e **telemetria** de chars
  (com dados reais quando disponíveis; SKIP gracioso caso contrário).

**Telemetria medida (dados reais, `D:\datasus\cache`, 16 grupos, `limit_per_group=25`):**

| Métrica | Valor |
|---|---|
| Cobertura de comentário | **299/390 colunas (76.7%)** |
| Grupos SEM layout (hint idêntico ao antigo) | `abo`, `amp`, `er`, `rj` (retrocompatível) |
| chars SEM comentários | 6.809 |
| chars COM comentários | 21.597 |
| delta | **+14.788 chars (+217%)** (para os 16 grupos; no caminho real do `generate.py` entram ≤ 6 grupos → ~+5,5k chars) |

> Reproduzir com `python dev_tools/measure_prompt_comments.py`.

**P0-B — o que foi entregue:**

- **Novo `db/lexical_rank.py`** (sem dependências novas):
  - `rank_columns(question, columns, comment_for_column=None)` — ordena colunas por
    relevância lexical (nome da coluna + comentário P0-A + conceitos de `CONCEPT_TOKENS`),
    **estável** (empates/sem-sinal preservam a ordem original);
  - `rank_groups(question, grupos, column_dict)` — reordena grupos por score das suas
    colunas, **sem remover nenhum** (a seleção continua vindo de `_determine_relevant_grupos`);
  - reutiliza `cid_map._normalize`/`_STOPWORDS` e `column_dictionary.CONCEPT_TOKENS`;
  - `_has_signal` garante que perguntas **puramente conceituais** ("casos por sexo", onde
    "sexo" é stopword) ainda ranqueiem via o casamento de conceito.
- **`db/column_dictionary.py`**: `labeled_columns`/`labeled_columns_sql_hint` ganham
  `rank_question` — reordena as colunas **antes** do corte de `limit_per_group`.
- **`api/llm_service_parts/generate.py`**: reordena `relevant_grupos` com `rank_groups`
  **antes** do corte `MAX_GRUPOS_LABELED=6`; passa `rank_question` às duas chamadas de
  `labeled_columns_sql_hint` (caminho principal e fallback). Tudo atrás de
  `settings.lexical_rank_enabled` (default `true`); qualquer falha degrada para a ordem
  original (best-effort).

**Telemetria medida (dados reais):**

| Pergunta | Grupo nº1 antes (alfabético) | Grupo nº1 agora (lexical) | Colunas top-8 |
|---|---|---|---|
| "por sexo" | `ab` | **`bi`** | `SEXOPAC`, … |
| "município de residência" | `ab` | **`bi`** | `MUNPAC`, … |
| "valor total pago" | `ab` | **`rd`** | `VAL_SH`, `VAL_SP`, `VAL_TOT`, … |
| "óbitos por faixa de idade" | `ab` | **`pa`** | `PA_FLIDADE`, `PA_OBITO`, `PA_IDADE`, … |

> Reproduzir com `python dev_tools/measure_prompt_comments.py` (seção final). Antes, o corte
> pegava os **primeiros alfabéticos** (`ab`/`abo`/`acf`…) e podia **descartar** o grupo
> certo; agora os mais prováveis sobem e as colunas relevantes sobrevivem ao corte de 25.
- Testes: `test/test_lexical_rank.py` (**19 testes**): tokenização, conceitos, ranking
  estável, reordenação de grupos, `rank_question` antes do corte e propagação no `generate.py`.

> **Nota sobre `prompt.py`:** a tabela da seção 5 listava `prompt.py` como alvo da P0-A. Na
> prática, o `labeled_hint` é montado inteiramente em `generate.py`; o `prompt.py` só contém
> `_build_concept_guidance`/`_build_fewshot_examples`. Como os comentários já entram pelo
> `labeled_hint` (e o `concept_guidance` lista as mesmas colunas — evita-se duplicá-las no
> prompt), **não foi necessário editar `prompt.py`** na P0-A.

**P2 — o que foi entregue:**

- **`db/cid_map.py`** — nova API determinística (sem DuckDB obrigatório):
  - `identificacao_de_alias(pergunta) -> list[str]`: retorna as **FAMÍLIAS** (categorias de
    3 chars) dos **aliases curados**. Âncora forte para o anti-falso-positivo
    (ex.: `"hepatite c"` → `["B17"]`; `"câncer de pele"` → `["C43","C44"]`; `"tuberculose"`
    → `["A15".."A19"]`). **Suporta MÚLTIPLAS doenças** na mesma pergunta (ex.: `"dengue e
    sarampo"` → `A90/A91 + B05`; `"tuberculose ou aids"` → `A15..A19 + B20..B24`): coleta
    TODAS as âncoras específicas independentes e, ao mesmo tempo, **engole** um alias
    GENÉRICO (ex.: `hepatite`, `cancer`) quando ele é apenas prefixo de um alias
    ESPECÍFICO mais longo da pergunta — assim NÃO mistura etiologias/topografias.
  - `termos_doenca(pergunta, min_len=4) -> tuple[str,...]`: extrai os termos específicos da
    doença (remove STOPWORDS). `min_len=4` permite termos curtos VÁLIDOS ("gota" → M10);
    o risco de casar "pele"→"pelego" é neutralizado pelo casamento por **prefixo** + interseção.
  - `entries_por_termos(termos) -> list[CidEntry]`: casamento por **palavra/segmento** na
    descrição oficial, exigindo a **INTERSEÇÃO** dos termos (não união). Ex.: "esclerose
    **E** múltipla" → só `G35` (antes, união trazia 174 CIDs de aids/tuberculose/câncer).
    **Fallback por RARIDADE (IDF)**, não por contagem de termos: quando não há interseção
    completa, pontua cada entrada por `log(N/df)` de cada termo casado, de modo que o termo
    **RARO** domine o **COMUM** — ex.: "degeneração **macular**" → `H353` (o adjetivo raro
    "macular" vence o termo comum "degeneração", que sozinho casava 13+ órgãos).
  - **Variantes de termo (adjetivo→substantivo + plural)** em `entries_por_termos`:
    "macular"→"mácula", "hepática"→"hepat", além de plural→singular. Sem isso a pergunta
    coloquial ("macular", "hepática") nãocasava a descrição oficial ("mácula", "fígado").
  - **Casamento por PREFIXO de segmento** (não substring solta): "cistite" casa
    "cistites" (plural) mas **NÃO** "cole**cistite**" (vesícula). Corrigiu over-matching
    (`cistite` → K80/K81 da vesícula; `separava` amigdalite/B002, etc.).
  - **Novos aliases curados** (termos coloquiais ausentes literalmente nas descrições):
    `sepse`/`septicemia` → `A40`,`A41`; `enfarte` → `I21`–`I23`; `pressao alta` → `I10`–`I15`;
    `calculose renal`/`pedra no rim`/`nefrolitíase` → `N20`,`N21`,`N23`.
  - **Aliases de SINTOMAS populares (códigos R)** — a descrição oficial usa o termo
    técnico, então o termo coloquial NÃO casa: `falta de ar` → `R060` (Dispneia),
    `dor de cabeca` → `R51` (Cefaleia), `dor no peito` → `R071..R074`, `tosse` → `R05`,
    `febre` → `R509`, `enxaqueca` → `G43/G44`, `desmaio` → `R55`, `nausea` → `R11`,
    `tontura`/`vertigem` → `R42`, `dor de garganta` → `R070`. Sem alias, "falta de ar"
    casava por ARTEFATO o termo "falta" em `X530 = Falta de alimento`.
  - **Sinônimos populares de doenças:** `catapora` → `B01` (Varicela); `verminose` →
    `B68/B71/A07`; `pedra na vesicula` → `K80`; `acucar no sangue`/`glicemia alta` →
    `E10`–`E14`. `amigdalite` → `J03/J35` (alias exato).
  - **`codigos_para` com precedência ABSOLUTA do alias curado:** quando a pergunta casa um
    alias, retorna **só** os códigos do alias (sem o casamento por termo) — evita que
    "falta de ar" vaze `X530` pela palavra "falta". Sem alias, mantém o comportamento
    antigo (`identificar`).
  - `familia_completa(familias) -> list[CidEntry]`: **todas** as entradas do `cid10` nas
    famílias dadas (base para grounding/validação sem top-N).
- **`api/llm_service_parts/cid.py`**:
  - **`_discover_cid_full(question, request_id) -> list[str]`** — fonte **PRIMÁRIA** da
    descoberta: (1) âncora por alias curado (família completa); (2) descoberta por termos
    quando não há alias; (3) **agregação de categorias**: adiciona a categoria-pai de 3
    chars para cada subcategoria (garante `B05`, `A15`, `C44`… para um `LIKE 'COD%'` que
    cobre os filhos); (4) **priorização** (não descarte) pelos prefixos reais nos dados;
    (5) **teto de sanidade** `settings.cid_discovery_max` (default 200). **Fallback**:
    sem a tabela `cid10`, usa as **famílias do próprio alias** (ex.: tuberculose → A15–A19).
  - `_cid_discovery_block(codes, request_id)` — bloco de prompt que lista os códigos,
    diz que **todos são da MESMA doença** e recomenda o `LIKE '<categoria>%'`.
  - **`_discover_cid_via_llm`** — refatorada para usar `_discover_cid_full` **primeiro**
    (sem LLM, sem `LIMIT 50`); a via "LLM monta o SELECT" só roda como **fallback** e, agora,
    **não trunca** (`rows[:50]` removido) e **não** pede `LIMIT`. O teto de sanidade é o
    mesmo `cid_discovery_max`.
  - **`_resolve_cid_grounding`** (2.3): usa a **família completa** quando há alias curado
    (não `max_resultados=8`).
  - **`_cid_map_prefixes_para`** (2.3): retorna a **família inteira** do alias curado
    (não top-3); mantém top-3 só para perguntas **ambíguas** (sem alias).
  - **2.4 — precedência preservada:** CID explícito (`_extract_explicit_cid`) continua
    pulando descoberta/de-para (inalterado).
- **`api/llm_service_parts/generate.py`** — *integração no prompt* (evita que a lista bruta
  de prefixos **compita** com a descoberta completa):
  - Quando há `cid_discovery` (descoberta completa), a lista de "prefixos reais" vira
    **referência SECUNDÁRIA** ("OUTROS PREFIXOS DE CID … NÃO troque o código indicado
    acima") e é **limitada a 30** (antes 60) — a fonte da doença é o bloco de descoberta.
    Sem descoberta, mantém o rótulo primário e o limite de 60.
  - A amostra de prefixos **filtra códigos de 2 chars** (`A0`, `A1`…), exibindo apenas
    **categorias de 3+** (`A00`, `A90`, `C44`…), reduzindo ruído.
  - Medição real (pergunta "sarampo"): prompt **22.269 chars**, com o bloco primário
    `CIDs DA DOENÇA CITADA … B05 = Sarampo … LIKE 'B05%'` + o bloco secundário rotulado.
- **Validação (medida com dados REAIS, `cid10` = 12.451 linhas):**

  | Pergunta | Resultado (`_discover_cid_full`) |
  |---|---|
  | "casos de dengue" | `A90, A91` |
  | "óbitos por sarampo" | `B05, B050..B059` (8) |
  | "casos de tuberculose" | `A15..A19, A150..` (**42**) |
  | "casos de hepatite c" | `B17, B170..B178` (**só C** — anti-falso-positivo) |
  | "casos de hepatite" | `B15..B19, …` (22 — todas as etiologias) |
  | "óbitos por câncer de pele" | `C43, C44, C430..` (22 — **sem C00/C50**) |
  | "casos de aids" | `B20..B24, B200..` (30) |
  | "casos de covid" | `U07` |
  | "total de atendimentos por mensagem" | `[]` (sem doença) |

  **Rodada extra (fallback por raridade + prefixo de segmento + aliases novos):**

  | Pergunta | Antes | Depois |
  |---|---|---|
  | "degeneração macular" | 25 CIDs (degenerações de outros órgãos) | `H35, H353` |
  | "cistite" | 24 (incl. K80/K81 = vesícula) | `N30, N33, N300..` (bexiga) |
  | "amigdalite" | 8 (incl. B00/B002 = herpes) | `J03, J35, J030..` |
  | "sepse" | **0** (termo coloquial) | `A40, A41, A400..` |
  | "enfarte" | 0 (→ alias) | `I21, I22, I23, …` |
  | "pressão alta" | 0 (→ alias) | `I10..I15, …` |
  | "pedra no rim" | 0 (→ alias) | `N20, N21, N23, …` |
  | "falta de ar" | `X530` ("Falta de alimento") | `R06, R060..` (dispneia) |
  | "dor de cabeça" | 56 (ruído por "cabeca") | `R51` (cefaleia) |
  | "dor no peito" | `P925` ("amamentação no peito") | `R07, R071..R074` |
  | "catapora" | **0** | `B01` (varicela) |
  | "verminose" | **0** | `A07, B68, B71` |
  | "açúcar no sangue" | 38 (D500, D758…) | `E10..E14` (diabetes) |
  | "pedra na vesícula" | 22 (B001, C23…) | `K80, K800..` |

- **Testes:** `test/test_cid_discovery_full.py` (13 → **22**) + `test/test_cid_false_positive.py`
  (9 → **17**) + `test/test_p2_prompt_cid.py` (5, **integração no prompt** via `generate.py`:
  bloco primário, rótulo SECUNDÁRIO, filtro de 2 chars, limite de 30, e "sem doença" não
  quebra) + `test/_cid_fake_catalogo.py` (catálogo fake compartilhado). Cobrem família
  completa, subcategorias, ordenação (categoria antes de subcategoria), teto de sanidade,
  bloco do prompt, **fallback sem `cid10`**, **prioridade da via determinística sobre o LLM**,
  os casos de **over-matching** ("hepatite c" ↛ B15/B16; "câncer de pele" ↛ C00/C50),
  **variante adjetivo→substantivo** ("macular"→"mácula"), **substring por prefixo**
  ("cistite" ↛ "colecistite") e os **aliases coloquiais novos** (sepse, enfarte, pressão alta)
  + **aliases de sintomas** (falta de ar ↛ "falta de alimento"; dor de cabeça, dor no peito)
  e a **precedência do alias em `codigos_para`**.
  Suites focadas: **84 testes** (6 arquivos) — todos passando.

---

## 0. Decisões de revisão (o que muda em relação ao plano antigo)

| Tema | Plano antigo (VSS/HNSW) | **Plano revisado** |
|---|---|---|
| Índice HNSW | `CREATE INDEX ... USING HNSW` | **DESCARTADO.** Busca vetorial **linear** (ou ranking lexical) sobre ≤ ~16 grupos. |
| `hnsw_enable_experimental_persistence` | Habilita persistência experimental | **DESCARTADO** — não usar HNSW em produção (flag experimental + risco de corrupção). |
| Modelo de embedding | `bge-m3` (1024d, ~568M) em ONNX | **Modelo pequeno** multi-idioma (ex.: `bge-small`/`e5-small`, ~33–110M) **opcional**; P0 = ranking lexical (sem ONNX). |
| Onde incide o esforço | Seleção semântica de GRUPO/TABELA | **Seleção de COLUNA/CID** (onde estão os erros reais) + **prompt enxuto**. |
| Comentários de coluna | "fonte textual dos embeddings" | **Injetados DIRETAMENTE no prompt** (P0) e também usados no documento do grupo. |
| CID (`_discover_cid_via_llm`) | Não tratado como núcleo | **NÚCLEO**: passa a retornar **TODOS** os CIDs da doença (catálogo `cid10` inteiro), com **análise restritiva de falso positivo**. |
| `CHECKPOINT`/`fsync` manual no `.duckdb` | A cada escrita (com 16 schemas anexados) | **Evitado** no caminho quente; escrita só via `core/safe_io` para artefatos próprios (JSON), nunca CHECKPOINT manual com ATTACH ativo. |
| Backup + escrita atômica | Mantido | **Mantido** (`core/safe_io.py`) — é barato e benéfico. |

> **Princípio-guia:** *ganho de acurácia e latência com o menor risco possível.* Tudo que
> adiciona um passivo novo (índice experimental, modelo pesado, escrita concorrente no
> `.duckdb`) só entra se houver evidência medida de que compensa.

---

## 1. Diagnóstico refinado (o que o código realmente diz)

| Observação (verificada no código) | Implicação para o plano |
|---|---|
| `generate.py` monta o schema do LLM a partir de `column_dict` → **apenas `column` + `type`**. O `schema_context` retorna `{"column","type"}` **sem comentário**. | Os `COMMENT ON COLUMN` de `column_comments.py` **não são usados no prompt**. Injetá-los melhora diretamente a escolha de coluna. |
| Prompt já foi limitado a `MAX_GRUPOS_LABELED=6` / `MAX_COLS_POR_GRUPO=25` (`generate.py`). | O timeout já tem mitigação parcial; o ganho adicional está em **priorizar** o que entra, não em remover grupos. |
| `_determine_relevant_grupos` seleciona por **período/arquivo**, e no fallback retorna **todos os grupos**. Já existem só ~16 grupos. | Seleção semântica de grupo tem **baixa granularidade** e ganho marginal. |
| Erros dominantes nos comentários do código: `AP_CIDCAS` em `resumo_*`, CID inventado (A67 para sarampo, C44 para câncer de pele), coluna homônima entre grupos. | O gargalo é **coluna + CID**. É aí que o esforço deve incidir. |
| `_discover_cid_via_llm`: `LIMIT 50` + `max_tokens=200` + `rows[:50]`; `_resolve_cid_grounding` usa `max_resultados=8`; `_cid_map_prefixes_para` usa `max_resultados=3`. | Hoje a descoberta **trunca** os CIDs da doença → risco de perder subcategorias válidas. Corrigir para **retornar todos** (com filtro restritivo anti-falso-positivo). |
| Não há dataset rotulado de "grupos corretos por pergunta". | Qualquer corte adaptativo (elbow/ratio) é **especulação** sem ground truth → evitar cortes agressivos; preferir recall. |

**Já existe e deve ser preservado:** `core/duckdb_config.py` (aplicação da flag é best-effort;
no plano revisado, simplesmente **não criaremos índices HNSW** — a flag pode permanecer ligada
sem efeito, ou desligada por padrão), `db/column_comments.py`, `db/column_dictionary.py`,
`db/cid_map.py` + tabela `cid10`.

---

## 2. Fases de implementação (priorizadas por impacto × risco)

### FASE P0-A — Injeção dos comentários de coluna no prompt  *(maior ganho de acurácia, risco quase nulo)*  ✅ **IMPLEMENTADA**

**Objetivo:** o LLM passar a enxergar `descricao`/`exemplos` das colunas, que **já existem**
em `db/column_comments.py` e nos layouts, mas hoje são descartados.

**Ação (backup + edit):**

- **`db/column_dictionary.py`**: `ColumnDictionary` passa a carregar também o comentário por
  coluna. Nova fonte: `column_comments.load_layout(cache_dir, grupo)` (lazy, cacheado).
  Cada item de `by_group` ganha `comment` (string, possivelmente vazia). Nova API:
  - `comment_for_column(column) -> str`
  - `labeled_columns_sql_hint(...)` passa a opcionalmente anexar `— <comment truncado>`.
- **`api/llm_service_parts/prompt.py`** (`_build_concept_guidance` / `labeled_hint`):
  emitir, por coluna, `"COLUNA" (TIPO) — <descrição curta>`, com **truncamento
  configurável** (ex.: `DATASUS_SCHEMA_COMMENT_MAX_CHARS=90`) para não estourar o prompt.
- **`api/llm_service_parts/generate.py`**: montar `schema` com o comentário incluído,
  reutilizando a priorização lexical da FASE P0-B.

**Guardas:** desligável por `DATASUS_SCHEMA_COMMENTS_ENABLED` (default `true`, mas com
truncamento); se nenhum comentário existir, comportamento idêntico ao atual.

**Validação:** `test/test_schema_comments_prompt.py` — com layouts fake, conferir que o
comentário aparece no `labeled_hint`/`schema`; medir `chars` do prompt antes/depois.

---

### FASE P0-B — Priorização lexical de colunas/grupos (prompt enxuto SEM modelo neural)  ✅ **IMPLEMENTADA**

**Objetivo:** reduzir o prompt de forma **determinística e auditável**, sem embeddings.

**Ação:**

- **Novo `db/lexical_rank.py`:** ranqueia grupos e colunas por relevância à pergunta usando
  os **tokens de conceito** de `column_dictionary.CONCEPT_TOKENS` + casamento lexical
  (nome de coluna, descrição do layout, `CONCEPT_LABELS`) com normalização (`_normalize` de
  `cid_map`) e tolerância a plural/acentos. **Sem dependências novas.**
- **`generate.py`:** em vez de cortar por ordem de `sorted(grupos)` e `cols[:25]`, ordenar
  por score lexical e manter:
  - grupos: todos acima do limiar **conservador** (favorece recall) ou, no mínimo, os
    `MAX_GRUPOS_LABELED` mais bem ranqueados;
  - colunas: as `MAX_COLS_POR_GRUPO` **mais relevantes** (não as primeiras).
- **Nunca remover** um grupo que hoje entraria por período (a seleção de grupo continua
  vindo de `_determine_relevant_grupos`); a priorização só reordena/limita a **lista rotulada**.

**Validação:** `test/test_lexical_rank.py` (ranking determinístico) + telemetria de
`chars` do prompt (meta: ≤ ~15k chars nos casos que hoje chegam a ~29,6k).

---

### FASE P1 — Seleção semântica de grupo/tabela OPCIONAL (sem HNSW, sem modelo pesado)

> **Só executar se P0-A/P0-B se mostrarem insuficientes** para o caso de "todos os grupos".
> Ganho esperado modesto (16 grupos), risco baixo se feita como abaixo.

**Ação:**

- **Modelo pequeno (opcional):** `db/small_embedder.py` com `bge-small`/`e5-small` ONNX
  (~33–110M), `intra_op_num_threads` **limitado** (ex.: `max(1, threads//2)`) para **não
  competir** com o `SET threads` do DuckDB. Se indisponível → cai no FASE P0-B (lexical).
- **Sem índice HNSW:** busca **linear** em memória sobre ≤ ~16 vetores de grupo
  (custo desprezível). Se quiser usar o DuckDB, usar `array_cosine_distance` com
  **`ORDER BY` simples** (sem `CREATE INDEX ... HNSW`).
- **Documento do grupo:** `descrição do grupo + lista de campos + comentários (descrição/
  exemplos dos layouts)` — o mesmo texto que P0-A agora injeta no prompt.
- **Corte conservador (recall-first):** manter um grupo se `score >= max(min_similarity,
  rel_ratio * max_score)` com `rel_ratio` **alto** (default `0.55`, não `0.75`) e **teto de
  segurança** `vss_max_tables` em **todos** os grupos (default = nº de grupos, i.e., sem corte
  agressivo). **Sem "elbow"** (frágil sem ground truth).
- **Sem `CHECKPOINT`/`fsync` manual no `.duckdb`.** Se os vetores forem persistidos, gravar
  em **JSON próprio** via `core/safe_io` (backup + escrita atômica), não como tabela HNSW.

**Validação (obrigatória antes de ligar):** harness com perguntas rotuladas (grupo(s)
esperado(s)); medir **recall** (meta: ≥ baseline atual que manda todos) e latência por
request. Só ligar se recall não cair.

---

### FASE P2 — Acurácia de CID: `_discover_cid_via_llm` retorna **TODOS** os CIDs da doença

> **Este é o item de maior acurácia.** Hoje a descoberta trunca (`LIMIT 50`,
> `rows[:50]`, `max_tokens=200`) e as famílias usam top-3/top-8 — pode **perder
> subcategorias válidas** da mesma doença (ex.: nem toda subcategoria de tuberculose,
> dengue ou câncer aparece).

**Objetivo:** para uma doença citada, obter **o conjunto completo** de códigos CID-10 que a
ela pertencem, com **análise restritiva de falso positivo**.

**2.1 — Descoberta determinística no `cid10` (fonte de verdade, sem truncar)**

- **Nova função** `_discover_cid_full(question, request_id) -> list[str]` em `cid.py`
  (ou refatoração de `_discover_cid_via_llm`):
  - Resolve os **termos da doença** da pergunta (reutilizando `CidMap._build_terms`/
    `_normalize` e `CID_ALIASES`), **removendo STOPWORDS** e termos genéricos.
  - Executa no DuckDB **uma consulta determinística** à tabela `cid10` que retorna **todas**
    as linhas cujo `descricao`/`descabrev` casam com os termos da doença,
    **SEM `LIMIT` artificial** (o `cid10` completo tem ~14k linhas; o recorte por termo é
    naturalmente pequeno) e **sem** depender de `max_tokens` do LLM.
  - Mantém a via "LLM monta o SELECT" apenas como **fallback**, mas sempre com o
    `LIMIT` removido/ampliado e com pós-processamento que **não trunca** (`rows[:50]` → todas).
- **Agregação de categorias:** para cada doença, retornar:
  - **todas** as **categorias de 3 chars** (ex.: `A15`, `A16`, …, `B05`) **e**
  - **todas** as **subcategorias de 4 chars** sob elas (ex.: `A150`, `A151`, `B050`, …),
    pois os dados DATASUS armazenam ambos os formatos (`SUBCAT` de 3 ou 4 chars).
  - Dedup por `SUBCAT`; **não colapsar** a subcategoria na categoria quando o usuário/doença
    exige granularidade (exceto no uso de filtro `LIKE`, que naturalmente cobre os filhos).

**2.2 — Análise restritiva de falso positivo (evitar CID errado por casamento amplo)**

O risco de "retornar todos" é **over-matching** (ex.: "câncer de pele" casar com
neoplasias de outras topografias; "hepatite" casar com `B18.x` de etiologia distinta). Regras:

1. **Prioridade a alias curado:** se `CID_ALIASES`/`cid_map.identificar` retornar famílias
   (ex.: câncer de pele → `C43`, `C44`), **essas famílias são a âncora** e só entram códigos
   que pertençam a elas.
2. **Casamento por termos completos, não substring solta:** exigir que o termo da doença
   (≥ 5 chars, normalizado) case como **palavra/segmento** na descrição, evitando
   substrings espúrias ("pele" dentro de "pelego"). Reutilizar a lógica de `_variantes`
   (plural/concordância) **sem** afrouxar para substring de 3–4 chars.
3. **Exclusão de termos de ruído:** descartar descrições que casem apenas com STOPWORDS/
   genéricos ("neoplasia maligna", "não especificada", "CID") **sem** o termo específico.
4. **Conflito de família:** se o termo casar com **mais de uma família não relacionada**
   (ex.: "meningite" → `A39`, `G00`–`G03`), manter todas **apenas se** o alias curado as
   listar; caso contrário, **exigir evidência forte** (o termo específico aparece na
   descrição) e, se ambíguo, **registrar log** e devolver o conjunto do alias.
5. **Verificação anti-alucinação com dados:** intersecção opcional com os prefixos reais
   presentes (`_known_cid_prefixes`/tabela `cid_prefixes`) **apenas para priorizar**, nunca
   para descartar (uma doença válida pode ter 0 casos no recorte e ainda ser correta).
6. **Limite superior de segurança** (não de recall): teto alto (ex.: 200 códigos) apenas
   para garantir sanidade; **não** truncar doenças com muitas subcategorias.

**2.3 — Consistência entre descoberta, grounding e validação**

- `_resolve_cid_grounding` passa a usar a **mesma família completa** (não `max_resultados=8`).
- `_cid_map_prefixes_para` deixa de usar `max_resultados=3` **quando a âncora é um alias
  curado**; deve retornar **a família inteira** do alias (ex.: todo `A15`–`A19` para
  tuberculose), preservando o comportamento atual para perguntas ambíguas.
- O **bloco do prompt** deixa explícito: "use QUALQUER um dos códigos abaixo (todos são da
  doença X); prefira a categoria de 3 chars no `LIKE` para cobrir as subcategorias".

**2.4 — Precedência preservada**

- CID explícito na pergunta continua com **prioridade absoluta** (`_extract_explicit_cid`),
  sem descobrir/substituir. O "retornar todos" só se aplica a **doença por nome**.

**Validação (obrigatória):**

- `test/test_cid_discovery_full.py`: para um conjunto de doenças (dengue, tuberculose,
  sarampo, câncer de pele, hepatite), conferir que **todas** as subcategorias do `cid10`
  pertencentes à família aparecem no resultado (comparação contra o próprio `cid10`).
- `test/test_cid_false_positive.py`: casos de over-matching (e.g., "hepatite" vs "hepatite
  C"; "câncer de pele" vs outras neoplasias) **não** devem incluir famílias fora da âncora.
- Regressão: `test/` completo (a validação `_validate_cid_filters` deve continuar coerente).

---

### FASE P3 — Infraestrutura segura (mantida, escopo reduzido)

**`core/safe_io.py` (novo)** — para artefatos **próprios** (JSON de cache de vetores/ranking,
modelos export). **Não** para CHECKPOINT manual do `.duckdb` (evita lock com 16 ATTACH).

- `backup_file(path)` → `<path>.bak_<ts>` + `os.fsync`.
- `atomic_write_text(path, text)` → `.tmp` + `flush` + `os.fsync` + `os.replace` + `fsync_dir`.
- `fsync_dir(path)`.

**Regra:** nenhuma edição sem backup; toda escrita nossa passa por escrita atômica + fsync.

**Validação:** `test/test_safe_io.py`.

---

### FASE P4 — Testes finais e telemetria

- `python -m pytest test/` (zero regressão).
- Telemetria de prompt: log `chars` antes/depois (já existe `llm sql prompt ... chars=`).
- Métrica de acurácia: contadores de `first_attempt_ok` (já existe) antes/depois.
- E2E com perguntas representativas (específica vs ampla; doença por nome vs CID explícito).

---

## 3. Ordem de execução

```
P0-A (comentários no prompt)  →  P0-B (priorização lexical)
        →  P2 (CID completo + anti-falso-positivo)   [maior acurácia]
        →  P1 (semântica de grupo OPCIONAL, só se necessário, com avaliação rotulada)
        →  P3 (safe_io, conforme necessário)  →  P4 (testes/telemetria)
```

- Cada arquivo: **backup `.bak_<ts>` + edição** antes de prosseguir.
- Cada mudança atrás de flag própria, com default **conservador** (P0-A ligado com
  truncamento; P1 **desligado** até haver avaliação).
- **NÃO** criar índices HNSW; **NÃO** usar `hnsw_enable_experimental_persistence`.

---

## 4. Efeito esperado no comportamento

- **Antes:** prompt com até ~462 colunas / ~29,6k chars (sem comentários) → `APITimeoutError`
  documentado; descoberta de CID truncada; erros de coluna (`AP_CIDCAS` em `resumo_*`) e de
  CID inventado.
- **Depois:**
  - prompt enxuto **e informativo** (colunas rotuladas por grupo + descrição curta),
    com priorização lexical;
  - LLM com **todos** os códigos da doença disponíveis (menos alucinação de CID);
  - seleção de grupo/tabela sem modelos pesados nem índices experimentais.

---

## 5. Arquivos afetados (resumo)

| Arquivo | Ação | Fase |
|---|---|---|
| `db/column_dictionary.py` | edit: comentário por coluna + hint enriquecido | P0-A |
| `api/llm_service_parts/prompt.py` | edit: comentários no `labeled_hint`/`concept_guidance` | P0-A |
| `api/llm_service_parts/generate.py` | edit: schema com comentários + priorização | P0-A/P0-B |
| `db/lexical_rank.py` | **novo**: ranking lexical (sem deps) | P0-B |✅ |
| `api/llm_service_parts/cid.py` | edit: `_discover_cid_full` (todos os CIDs) + anti-falso-positivo | P2 ✅ |
| `db/cid_map.py` | edit: `identificacao_de_alias`/`familia_completa`/`termos_doenca`/`entries_por_termos` (sem truncar) | P2 ✅ |
| `db/small_embedder.py` | **novo** (opcional): modelo pequeno ONNX; fallback lexical | P1 |
| `db/group_embeddings.py` | **novo** (opcional): vetores de grupo + busca **linear** (sem HNSW) | P1 |
| `core/safe_io.py` | **novo**: backup + escrita atômica (artefatos próprios) | P3 |
| `config/settings.py` | edit: flags `DATASUS_SCHEMA_COMMENTS_ENABLED`, `DATASUS_SCHEMA_COMMENT_MAX_CHARS`, `DATASUS_GROUP_SEMANTIC_ENABLED`, `DATASUS_GROUP_*` | todas |
| `db/cache.py` | edit (opcional): hook de rebuild dos vetores de grupo (JSON, best-effort) | P1 |
| `test/test_*.py` | novos testes por fase | P0–P4 |

---

## 6. Configuração (variáveis de ambiente)

| Variável | Default | Descrição |
|---|---|---|
| `DATASUS_SCHEMA_COMMENTS_ENABLED` | `true` | Injeta descrição/exemplos das colunas no prompt (P0-A) |
| `DATASUS_SCHEMA_COMMENT_MAX_CHARS` | `90` | Truncamento do comentário por coluna |
| `DATASUS_LEXICAL_RANK_ENABLED` | `true` | Priorização lexical de colunas/grupos (P0-B) |
| `DATASUS_GROUP_SEMANTIC_ENABLED` | `false` | Liga seleção semântica de grupo (P1; desligado até haver avaliação) |
| `DATASUS_EMBED_MODEL_DIR` | `models/bge-small-onnx` | Diretório do modelo pequeno (P1, opcional) |
| `DATASUS_EMBED_THREADS` | `max(1, threads//2)` | Threads do ONNX (não competir com o DuckDB) |
| `DATASUS_GROUP_MIN_SIMILARITY` | `0.35` | Piso absoluto de similaridade (P1) |
| `DATASUS_GROUP_REL_RATIO` | `0.55` | Fração do melhor score mantida (P1, conservador) |
| `DATASUS_GROUP_MAX_TABLES` | `= nº de grupos` | Teto de segurança (P1; por padrão não corta) |
| `DATASUS_CID_DISCOVERY_MAX` | `200` | Teto de sanidade (não de recall) da descoberta de CID (P2) |

> **Removidas** do plano antigo: `DATASUS_VSS_ENABLED`, `DATASUS_EMBED_DIM=1024`,
> `DATASUS_VSS_ELBOW_GAP`, `DATASUS_VSS_MAX_TABLES` (elbow/ratio agressivos).
> **Mantida** a flag de HNSW existente apenas por compatibilidade; **sem criar índice HNSW**.

---

## 7. Pré-requisitos de execução

1. **P0-A/P0-B/P2 não requerem nenhuma dependência nova** (nem ONNX). Rodar direto:
   ```
   python -m pytest test/
   ```
2. **P1 (opcional):** só se P0/P2 se mostrarem insuficientes. Exportar um modelo **pequeno**
   (ex.: `bge-small`/`e5-small`) para ONNX e **avaliar recall/latência** com perguntas
   rotuladas antes de ligar. Sem HNSW.
3. Medir, nos logs (`llm sql prompt ... chars=`, `llm sql first_attempt ...`), o efeito de
   cada fase antes/depois.
