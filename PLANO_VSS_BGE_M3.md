# Plano de Implementação — Busca Semântica de Grupos/Tabelas com VSS (HNSW) + `bge-m3-onnx-o2-cpu`

> ⚠️ **SUPERSEDIDO — NÃO EXECUTAR COMO ESTÁ.** Este plano foi **ajustado** após análise
> crítica do código real; o resultado vigente está em **`PLANO_MELHORIA_PROMPT_CID.md`**.
> Nesta revisão foram **DESCARTADOS**:
> - **Índice HNSW** (`CREATE INDEX ... USING HNSW`) e a flag
>   **`hnsw_enable_experimental_persistence`** (persistência experimental → risco de
>   corrupção; ~16 grupos → busca **linear** basta);
> - o modelo **`bge-m3` (1024d, ~568M)** em CPU (latência/contensão de threads
>   desproporcionais ao ganho de escolher entre ~16 grupos) → substituído por priorização
>   **lexical** (P0) e, se necessário, um modelo **pequeno** (P1);
> - o **`CHECKPOINT`/`fsync` manual no `.duckdb`** no caminho quente (colide com 16 schemas
>   anexados).
>
> O esforço migrou para: **(P0)** injetar os **comentários de coluna no prompt** +
> priorização lexical; **(P2)** fazer `_discover_cid_via_llm` retornar **TODOS** os CIDs
> da doença com **análise restritiva de falso positivo**. Use o documento novo para o plano
> vigente.

> **Status:** PLANO ANTIGO (não implementado; preservado apenas como histórico).
> **Modelo de embedding:** `bge-m3-onnx-o2-cpu` — o BGE-M3 (`BAAI/bge-m3`, dimensão **1024**)
> exportado para **ONNX** e otimizado no nível **O2** para **CPU**
> (`onnxruntime` + `CPUExecutionProvider`). **Sem dependência de `torch` em produção.**

---

## 1. Resumo das decisões

- **Busca vetorial SOMENTE em grupo/tabela** (não há seleção semântica de colunas).
- **Seleção de colunas: 100% a cargo do LLM**, que recebe o **schema COMPLETO**
  (colunas + comentários) apenas dos grupos/tabelas semanticamente selecionados.
- **Seleção adaptativa de grupos/tabelas** (sem top-k fixo): corte por score relativo +
  *elbow* + teto de segurança.
- **Backup completo antes de cada ajuste** e **gravação atômica com `fsync`** em toda
  escrita — para nunca corromper arquivos nem o `.duckdb`.
- Tudo reversível, atrás da flag **`DATASUS_VSS_ENABLED`** (default **`false`**) — o
  comportamento atual permanece intacto até a flag ser habilitada.
- **Best-effort:** se o modelo VSS/Ort estiver indisponível, o pipeline segue como hoje.

---

## 2. Diagnóstico do ambiente (confirmado)

| Item | Situação |
|---|---|
| `duckdb` | `1.5.5` instalado ✔ |
| `LOAD vss` + `SET hnsw_enable_experimental_persistence=true` | Já implementado em `core/duckdb_config.py` (Passo 1) ✔ |
| Metadados (descrição/exemplos) via `COMMENT ON COLUMN` | Já implementado em `db/column_comments.py` (fonte textual dos embeddings) ✔ |
| Dicionário consolidado de colunas | `db/column_dictionary.py` ✔ |
| `onnxruntime`, `transformers`, `tokenizers`, `huggingface_hub`, `optimum` | **NÃO instalados** → entram no `requirements-vss.txt` |

**Já existe (não precisa recriar):** `core/duckdb_config.py` aplica a flag HNSW experimental
na conexão principal (`DatasusCache._configure_memory`) e em cada grupo (`group_cache.py`).

**O que falta (núcleo deste plano):** gerar embeddings com `bge-m3-onnx-o2-cpu`
(Passo 2), criar índice HNSW cosine (Passo 3) e consultar via `array_cosine_distance`
(Passo 4) — além dos hooks de reconstrução e da integração com o LLM.

---

## 3. Fases de implementação

### FASE 0 — Infraestrutura segura (backup + fsync)

**Novo `core/safe_io.py`:**

- `backup_file(path)` → copia para `<path>.bak_<YYYYMMDD_HHMMSS>` + `os.fsync`.
- `atomic_write_text(path, text)` → escreve em `.tmp` + `flush` + `os.fsync` +
  `os.replace` + `fsync_dir`.
- `fsync_dir(path)` → `os.open(dir, O_RDONLY)` + `os.fsync`.
- `fsync_duckdb(conn)` → `CHECKPOINT` + fsync do arquivo `.duckdb`.

**Regra:** **nenhuma edição sem backup prévio**; toda escrita passa por escrita atômica + fsync.

**Validação:** `test/test_safe_io.py` (backup idêntico, tmp removido, fsync chamado via
monkeypatch); `python -m compileall core/safe_io.py`.

---

### FASE 1 — Dependências e configuração

**1.1 `requirements-vss.txt`** (novo):

```txt
onnxruntime>=1.18,<2
transformers>=4.40,<5
tokenizers>=0.19,<1
huggingface_hub>=0.23,<1
numpy>=1.26,<3
# --- apenas para EXPORTAR o modelo uma vez (dev; ver FASE 2b) ---
optimum[onnxruntime]>=1.20,<2
onnx>=1.16,<2
```

> `torch` **não** entra em produção; é dependência transitiva do processo de export (FASE 2b)
> e fica documentado como opcional de desenvolvimento.

**1.2 `config/settings.py`** (backup + edit):

- `vss_enabled` = `DATASUS_VSS_ENABLED` (default **false**)
- `embed_model_dir` = `DATASUS_EMBED_MODEL_DIR` (default `PROJECT_DIR/models/bge-m3-onnx-o2-cpu`)
- `embed_dim` = `DATASUS_EMBED_DIM` (default `1024`)
- `embed_threads` = `DATASUS_EMBED_THREADS` (default `= threads`)
- `vss_cache_dir` (cache do `huggingface_hub`)
- **Seleção adaptativa de GRUPO/TABELA (substitui top-k fixo):**
  - `vss_min_similarity` = `DATASUS_VSS_MIN_SIMILARITY` (default `0.35`)
  - `vss_rel_ratio` = `DATASUS_VSS_REL_RATIO` (default `0.75`)
  - `vss_elbow_gap` = `DATASUS_VSS_ELBOW_GAP` (default `0.08`)
  - `vss_max_tables` = `DATASUS_VSS_MAX_TABLES` (default `12`) — teto de segurança

**Validação:** `python -c "from config.settings import setting; print(setting.vss_enabled, setting.embed_model_dir)"`.

---

### FASE 2 — Runtime ONNX do `bge-m3-onnx-o2-cpu`

**Novo `db/bge_m3_onnx.py`:**

- `_load_model()`: tokenizer (transformers/tokenizers) +
  `onnxruntime.InferenceSession(<model_dir>/model.onnx, providers=["CPUExecutionProvider"])`,
  com `SessionOptions.optimization_level=ORT_ENABLE_EXTENDED` (O2) e
  `intra_op_num_threads=embed_threads`. Singleton lazy; falha → `None` (segue sem VSS).
- `embed_texts(texts)`: tokenização → sessão ONNX → pooling BGE-M3 (CLS) +
  normalização L2 → vetores `FLOAT[1024]`.
- Verificação offline de `model.onnx`/`tokenizer.json` em `models/bge-m3-onnx-o2-cpu/`;
  se ausentes, loga a instrução para rodar o export (FASE 2b).

**Validação:** `test/test_bge_m3_onnx.py` (sessão ONNX *fake* via monkeypatch validando
pooling/normalização) + teste real sob `@skipUnless(model_dir.exists())`.

---

### FASE 2b — `dev_tools/export_bge_m3_onnx.py` (novo)

Export one-shot `BAAI/bge-m3` → **`bge-m3-onnx-o2-cpu`**:

- `optimum-cli export onnx --model BAAI/bge-m3 --task feature-extraction --optimize O2`
  → destino `models/bge-m3-onnx-o2-cpu/` (com tokenizer/config).
- Sanidade: inferência de teste → dimensão 1024 + norma ≈ 1.
- Flags: `--dry-run`, `--force`; toda escrita via `core/safe_io` (backup + fsync).
- CLI:
  ```
  python dev_tools/export_bge_m3_onnx.py [--out models/bge-m3-onnx-o2-cpu] [--dry-run] [--force]
  ```

---

### FASE 3 — Tabela, índice e busca (Passos 2–4) — **somente grupo/tabela**

**Novo `db/schema_embeddings.py`:**

**Documento (nível grupo/tabela):** `descrição do grupo + lista de campos + comentários
(descrição/exemplos dos layouts)`; embedado pelo `bge-m3-onnx-o2-cpu`.

**Passo 2 — tabelas:**

```sql
CREATE TABLE IF NOT EXISTS schema_table_embeddings (
  grupo VARCHAR, table_name VARCHAR, document VARCHAR, embedding FLOAT[1024]
);
CREATE TABLE IF NOT EXISTS schema_embeddings_meta (key VARCHAR PRIMARY KEY, value VARCHAR);
```

Idempotência pela *signature* do `ColumnDictionary` + mtime dos `layout_csv`.

**Passo 3 — índice HNSW (cosine):**

```sql
CREATE INDEX IF NOT EXISTS schema_table_hnsw_idx
ON schema_table_embeddings USING HNSW (embedding) WITH (metric='cosine');
```

**Passo 4 — busca:**

```sql
SELECT grupo, table_name, document,
       array_cosine_distance(embedding, ?::FLOAT[1024]) AS distance
FROM schema_table_embeddings ORDER BY distance;
```

**API:** `search_similar_tables(conn, question)` (retorna scores) e
`ensure_schema_embeddings(cache)` (orquestra build + índice + `CHECKPOINT` + `fsync_duckdb`).

**Validação:** `test/test_schema_embeddings.py` (runtime *fake*, dim pequena) — schema,
índice HNSW, ordenação cosine e idempotência.

---

### FASE 4 — Hooks de (re)construção

- **`db/cache.py`** (backup + edit): em `refresh_column_dictionary()` e no `__init__`,
  se `vss_enabled` → `ensure_schema_embeddings(self)` (best-effort).
- **`db/group_cache.py`** (backup + edit): os comentários continuam sendo a fonte textual;
  os vetores são gerados **apenas no nível global** (evita carregar o ONNX 16×).

**Validação:** `test_canonical_schema_s2.py` e `test_hnsw_experimental_persistence.py`
sem regressão; flag desligada = zero efeito.

---

### FASE 5 — Integração com o LLM + seleção adaptativa de grupos/tabelas

**`api/llm_service_parts/generate.py`** (backup + edit) — em `_generate_sql`, com `vss_enabled`:

1. `search_similar_tables(question)` usando o `bge-m3-onnx-o2-cpu` → lista (grupo, score).
2. **Corte adaptativo (sem N fixo):**
   - `max_score = 1 - min(distance)`; mantém `score >= max(vss_rel_ratio * max_score, vss_min_similarity)`.
   - **Elbow:** maior queda consecutiva `>= vss_elbow_gap`.
   - Teto de segurança `vss_max_tables`.
3. Para os **grupos/tabelas selecionados**, o LLM recebe o **schema COMPLETO**
   (todas as colunas + comentários) — **a escolha das colunas é 100% do LLM**.
4. **Fallback:** VSS vazio/erro ou `vss_enabled=false` → comportamento atual **intacto**.

**Validação:** `test/test_vss_generate_integration.py` (prompt VSS on/off + telemetria
`chars`; casos específico vs amplo).

---

### FASE 6 — Cache warm (opcional)

**`core/duckdb_config.py`** (backup + edit): `INSTALL cache_prewarm; LOAD cache_prewarm;` +
`PRAGMA prewarm` sob `DATASUS_CACHE_PREWARM`.

---

### FASE 7 — Testes finais

- `test/test_safe_io.py`, `test/test_bge_m3_onnx.py`, `test/test_schema_embeddings.py`,
  `test/test_vss_generate_integration.py`, `test/test_export_bge_m3_onnx.py`.
- Suíte completa: `python -m pytest test/` (zero regressão).
- E2E: `DATASUS_VSS_ENABLED=true` → conferir `schema_table_embeddings` + índice HNSW via
  `duckdb_indexes()`; medir o tamanho do prompt antes/depois.

---

## 4. Ordem de execução e segurança

```
FASE 0  →  FASE 1  →  FASE 2  →  FASE 2b  →  FASE 3  →  FASE 4  →  FASE 5  →  FASE 6  →  FASE 7
```

- Cada arquivo: **backup `.bak_<ts>` + edição + `os.fsync`** antes de prosseguir.
- Todas as mudanças atrás de **`DATASUS_VSS_ENABLED`** (default **false**).
- O `.duckdb` sempre recebe **`CHECKPOINT` + `fsync`** antes de fechar.

---

## 5. Efeito no comportamento

- **Antes:** prompt com ~462 colunas de 14 grupos (≈ 29,6k chars) → `APITimeoutError`
  documentado no código (`generate.py`).
- **Depois:** apenas os **grupos/tabelas semanticamente relevantes** entram no prompt; a
  escolha das **colunas é feita pelo LLM** a partir do schema completo desses grupos →
  prompt enxuto, sem perder contexto e sem estourar o prefill do modelo.

---

## 6. Arquivos afetados (resumo)

| Arquivo | Ação |
|---|---|
| `core/safe_io.py` | **novo** (backup + fsync) |
| `requirements-vss.txt` | **novo** (dependências do VSS/ONNX) |
| `config/settings.py` | edit (config VSS/embedding) |
| `db/bge_m3_onnx.py` | **novo** (runtime ONNX do `bge-m3-onnx-o2-cpu`) |
| `dev_tools/export_bge_m3_onnx.py` | **novo** (export one-shot O2 CPU) |
| `db/schema_embeddings.py` | **novo** (tabela + índice HNSW + busca) |
| `db/cache.py` | edit (hooks de reconstrução) |
| `db/group_cache.py` | edit (garante fonte textual; sem vetores por grupo) |
| `api/llm_service_parts/generate.py` | edit (seleção adaptativa de grupos/tabelas) |
| `core/duckdb_config.py` | edit opcional (cache prewarm) |
| `test/test_*.py` | novos testes por fase |

---

## 7. Configuração (variáveis de ambiente)

| Variável | Default | Descrição |
|---|---|---|
| `DATASUS_VSS_ENABLED` | `false` | Liga/desliga toda a busca vetorial |
| `DATASUS_EMBED_MODEL_DIR` | `models/bge-m3-onnx-o2-cpu` | Diretório do modelo ONNX |
| `DATASUS_EMBED_DIM` | `1024` | Dimensão dos embeddings |
| `DATASUS_EMBED_THREADS` | `= threads` | Threads do `onnxruntime` |
| `DATASUS_VSS_MIN_SIMILARITY` | `0.35` | Piso absoluto de similaridade |
| `DATASUS_VSS_REL_RATIO` | `0.75` | Fração do melhor score mantida |
| `DATASUS_VSS_ELBOW_GAP` | `0.08` | Queda consecutiva que define o corte |
| `DATASUS_VSS_MAX_TABLES` | `12` | Teto de segurança de grupos/tabelas |
| `DATASUS_CACHE_PREWARM` | `false` | Habilita `cache_prewarm` (FASE 6, opcional) |

---

## 8. Pré-requisitos de execução

1. Criar o modelo local uma única vez:
   ```
   pip install -r requirements-vss.txt
   python dev_tools/export_bge_m3_onnx.py
   ```
   Isso gera `models/bge-m3-onnx-o2-cpu/` (`model.onnx` + `tokenizer.json` + config).
2. Definir `DATASUS_VSS_ENABLED=true` e reiniciar a aplicação.
3. (Opcional) medir prompt antes/depois nos logs (`llm sql prompt ... chars=`).
