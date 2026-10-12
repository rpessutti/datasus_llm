# PLANO — Reparo do cache de grupos duplicado (2×) + documentação de métodos

> **Status:** EM EXECUÇÃO. Escrito em 2026-10-12 (branch `2026-10-12`).
> **Objetivo:** eliminar a duplicação **real** (2×) de linhas em `<grupo>.datasus_cache`
> e de entradas em `<grupo>.file_manifest`, **preservando integralmente os Parquet**
> (que são o cache real) e **sem reprocessar dados** desnecessariamente, além de
> documentar (docstring) **todos os métodos dos arquivos envolvidos** e blindar a
> ingestão para a duplicação não voltar.

---

## 1. Diagnóstico (medido, não estimado)

### 1.1 Tamanho e contagens (2026-10-12)

| grupo | `<g>.duckdb` | Parquet do grupo | `file_manifest` | linhas `datasus_cache` | linhas reais nos Parquet (footer) | razão |
|---|---|---|---|---|---|---|
| `er` | 7,6 MB | 1,9 MB (27 arq) | **54** (=2×27) | 190.744 | 95.372 | **2,000** |
| `rj` | 72 MB | 18 MB (85) | **170** | 595.728 | 297.864 | **2,000** |
| `acf` | 39 MB | 76 MB (2.063) | **4.126** | 455.306 | 227.653 | **2,000** |
| `rd` | 2,25 GB | 590 MB (324) | **648** | 21.320.534 | 10.660.267 | **2,000** |
| `sp` | 47,3 GB | 7,2 GB (1.100) | **2.200** | 1.257.474.998 | 628.737.499 | **2,000** |
| `am` | 14,2 GB | 5,1 GB (1.949) | 3.865 | 263.666.606 | 131.833.303 | **2,000** |
| `bi` | 88,6 GB | 31,5 GB (1.211) | **2.422** | 1.858.437.834 | 929.218.917 | **2,000** |
| `pa` | **149 GB** | 28 GB (958) | **1.916** | 2.998.286.594 | 1.499.143.297 | **2,000** |

Reprodução: `python -u dev_tools/repair_group_cache_duplication.py --dry-run`
(e os scripts de diagnóstico `_diag_cache.py`, `_diag_dupla_contagem.py`, `_diag_manifest.py`).

### 1.2 Prova de que são as MESMAS linhas

`er`, ano 2025: tabela `datasus_cache` = **77.592** linhas; Parquets `cache/er/2025` = 8 arquivos,
**38.796** linhas; `parquet_path` **distintos = 16** (2 por arquivo); razão **2,000**:

```
  19.907  D:\datasus\cache\er\2025\03\ERBA2503.parquet
  19.907  /home/robinson/datasus_llm/cache/er/2025/03/ERBA2503.parquet
```

Duas famílias no `file_manifest` (chaves distintas, mesmo arquivo):

```
source='ERRO2101.parquet'          cached='/media/robinson/OS/datasus/cache/er/ERRO2101.parquet'  <- era Linux
source='2021\01\ERRO2101.parquet'  cached='D:\datasus\cache\er\2021\01\ERRO2101.parquet'         <- era Windows
```

### 1.3 Causa raiz

1. Os `.duckdb` foram **trazidos da outra máquina (Linux)** — os `parquet_path` Linux estão gravados nas tabelas.
2. O formato da **chave do manifesto** mudou (`ERRO2101.parquet` plano → `2021\01\ERRO2101.parquet`
   relativo a `source_dir`), e `db/group_cache.py` decide "já em cache" **apenas por essa chave**
   (`ingest`/`pending_batch`), sem nunca verificar `parquet_path`.
3. Consequência: a ingestão no Windows recopiou/registrou tudo com a chave nova e
   **reinseriu** as mesmas linhas na tabela já existente (`INSERT INTO … FROM temp_batch`),
   sem qualquer guarda de idempotência → 2× linhas + 2× manifesto.
4. `db/cache.py:939` — como `DATASUS_DATA_DIR=D:\datasus\cache\parquet` **não existe**, cai no
   fallback `source_dir = <cache_dir>/<grupo>` (o próprio cache): a chave vira
   `ano\mes\arquivo.parquet` e o "copy" é no-op (por isso o disco **não** ganhou arquivos duplicados).

### 1.4 Impacto

* **Funcional (grave):** a `datasus_query_cache` é `UNION ALL` de `<grupo>.datasus_cache` filtrando por
  `atend_ano`/`atend_mes` (colunas nativas, iguais nas duas cópias) → **COUNT/SUM saem 2×**.
* **Espaço:** ~157 GB de blocos redundantes somando os grupos (metade dos ~315 GB dos `.duckdb`).
* **Interface:** o nº de "arquivos" por grupo aparece 2× porque vem do `file_manifest`.
* **Estado atual:** congelado — o log mostra `lote de 0 pendentes de N totais (já em cache: N)`
  para todos os grupos (a ingestão não cresce mais a duplicação).

---

## 2. Estratégia (o que apaga e o que preserva)

| Ação | Apaga Parquet? | Reprocessa dados? | Custo | Uso |
|---|---|---|---|---|
| **A. `DELETE` das linhas duplicadas + `CHECKPOINT` + `VACUUM` (in-place)** | Não | **Não** | baixo/médio | **principal** |
| **B. `CREATE OR REPLACE TABLE … AS SELECT …`** | Não | Sim (reescreve tudo) | alto | fallback |
| **C. `dev_tools/rebuild_group_cache.py`** (recria do Parquet) | Não (só lê) | Sim (recria a tabela) | alto (horas) | último recurso |
| **D. Limpeza do `file_manifest` (caminhos inexistentes)** | Não | Não | segundos | **principal** |
| **E. `DATASUS_CACHE_AS_VIEW=true` (S1)** | Não | Não (em teoria) | — | **bloqueado hoje** (ver §2.1) |
| **F. Guardas de idempotência no código** | Não | Não | — | **principal** |

**Decisão:** executar **A + D + F**. B/C apenas se algum grupo falhar; E fica como melhoria futura.

### 2.1 Por que E (S1) está fora deste reparo

* A conversão tabela→view só existe em `_GroupCache.ingest()` (branch S1) e a **produção não chama**
  esse método (usa `pending_batch` + `import_batch`).
* `groups_needing_ingest()` exige **tabela** (`_cache_table_exists()`); com view, todos os grupos
  seriam "pendentes" → 3 rodadas + `RuntimeError` → app sobe em **503**.
* Conclusão: S1 exige mudança de código própria (aceitar view no `groups_needing_ingest` +
  caminho explícito de conversão). Não entra no reparo de hoje; fica registrado como follow-up.

## 3. Regras de segurança (obrigatórias neste reparo)

1. **App parada** durante o reparo (verificar `Get-Process python`; liberar locks `*.duckdb`).
   Não abrir os 16 bancos em ATTACH — cada grupo é tratado **isoladamente**.
2. **Backup integral** de todo arquivo-fonte refatorado, antes da 1ª edição:
   `_backups/cache_duplicado_<ts>/` (cópia byte a byte + SHA256). **Feito:**
   `_backups/cache_duplicado_20261012_113100/`.
3. **Os Parquet nunca são apagados/movidos** — são a fonte de verdade e o cache real.
4. `--dry-run` **sempre** antes de `--apply`; um grupo por vez nos grupos grandes.
5. **Nada de `CHECKPOINT` manual com os 16 schemas anexados** — só na conexão isolada do grupo.
6. Toda verificação é **por medição**: razão `linhas(tabela)/linhas(Parquet) == 1,000` e
   manifesto == nº de arquivos em disco.
7. Falhou algo → **rollback** (§6). Se o `.duckdb` ficar inconsistente, reconstruir do Parquet (§F5).

---

## 4. Fases de execução

### F0 — Inventário (feito)
`python -u _inventario_metodos.py` → `inventario.txt` (métodos, docstrings ausentes, mojibake).
Resumo: `db/group_cache.py` **10** métodos sem docstring; `db/cache.py` **24**;
`dev_tools/rebuild_group_cache.py` **3**.

### F1 — Backups integrais (feito)
`_backups/cache_duplicado_20261012_113100/` com `db__cache.py` (133.942 B),
`db__group_cache.py` (74.492 B), `dev_tools__rebuild_group_cache.py` (8.099 B) + `SHA256SUMS.txt`.
Restauração em §6.

### F2 — Ferramenta de reparo (novo arquivo)
`dev_tools/repair_group_cache_duplication.py` — **novo**, com docstring em **todos** os métodos:
* `--dry-run` (default): por grupo, reporta `.duckdb`, nº de Parquet, linhas da tabela, linhas dos
  Parquet (footer), razão, `parquet_path` distintos, manifesto total/distinto, entradas de manifesto
  com caminho inexistente e espaço potencialmente recuperável.
* `--apply <grupo> ...`: (1) remove do `file_manifest` as linhas cujo caminho não existe;
  (2) `DELETE FROM <g>.datasus_cache WHERE parquet_path` **fora** do cache atual;
  (3) `CHECKPOINT` + `VACUUM`; (4) revalida a razão (exige **1,000**) e imprime antes/depois.
* `--rebuild <grupo>`: caminho caro (opção C) — delega ao rebuild a partir do Parquet.
* Nunca toca em Parquet; toda escrita é precedida de contagem (dry-run) e o `DELETE` é transacional.

### F3 — Execução do reparo (dados reais)
Ordem pequenos → grandes. **Lote 1:** `er`, `acf`, `ar`, `amp`, `atd`, `rj`.
**Lote 2:** `ps`, `rd`, `aq`, `am`. **Lote 3 (com espaço livre e janela de tempo):** `sp`, `bi`, `pa`.
Após cada grupo: `_diag_dupla_contagem.py <g> <ano>` (razão 1,000) + `_diag_cache.py`.

### F4 — Blindagem do código (`db/group_cache.py`)
* `import_batch`: **antes** do INSERT do lote, remover a contribuição anterior do mesmo arquivo
  (`DELETE FROM <cache_table> WHERE parquet_path IN (<targets>)`) → idempotência real.
* `pending_batch`/manifesto: considerar inválida a entrada cujo `cached` não existe no disco
  (evita reprocessar por chave órfã e ignora a família Linux "fantasma").
* Sem mudança de comportamento quando o manifesto está íntegro (guardas best-effort, com log).

### F5 — `dev_tools/rebuild_group_cache.py` (fallback / último recurso)
* Corrigir `CACHE_DIR` para `config.settings.setting.cache_dir` (hoje `ROOT/"cache"`, vazio).
* Adicionar as docstrings faltantes (`_parquet_files`, `rebuild_group`, `main`).
* Continua sendo o caminho C (recria a tabela do zero a partir do Parquet).

### F6 — Documentação (docstring em TODOS os métodos dos arquivos envolvidos)
* **L1 (obrigatória — arquivos modificados):** `db/group_cache.py` (10 métodos),
  `dev_tools/rebuild_group_cache.py` (3), `dev_tools/repair_group_cache_duplication.py` (novo, todos).
* **L2 (recomendada, mesma execução):** `db/cache.py` (24 métodos).
* Fora de escopo (apenas lidos): `core/io_utils.py` (já 100% documentado),
  `db/column_dictionary.py`, `db/cid_map.py`, `api/*`.
* Regra: a docstring descreve **o que o método faz, entradas, saídas e efeitos colaterais**;
  **nenhuma mudança de lógica**. Verificação: `py_compile` + `pytest --collect-only -q`.

### F7 — Testes e validação
* Novo `test/test_repair_group_cache.py` (dados **fake** em DuckDB temporário): monta tabela +
  manifesto duplicados, roda o reparo, exige razão 1,000 e manifesto sem órfãos.
* Suíte focada: `test_cid_discovery_full.py`, `test_cid_false_positive.py`, `test_p2_prompt_cid.py`,
  `test_lexical_rank.py`, `test_safe_io.py`, `test_atend_columns_f3.py`, `test_cache_as_view_s1.py`.
* `python -m pytest test/ --collect-only -q` → **193** testes, sem `INTERNALERROR`.

### F8 — Fechamento
Atualizar `PLANO_P4_SUITE_TESTES.md` / `PLANO_MELHORIA_PROMPT_CID.md` com o resultado do reparo;
commit na branch `2026-10-12`.

---

## 5. Anexo A — métodos sem docstring (inventário de 2026-10-12)

**`db/group_cache.py` (10):** `__init__`, `_configure_memory`, `_scalar`, `_cache_table_exists`,
`_datasus_view_exists`, `discover_files`, `_refresh_period_index`, `ingest`, `_cached_group_files`,
`_ensure_view_and_index`.

**`db/cache.py` (24):** `class DatasusCache`, `__init__`, `database_path` (propriedade linha 267),
`_indexed_parquet_files`, `_indexed_parquet_records`, `_refresh_period_index`, `question_scope`,
`_file_periods`, `prepare_question_cache`, `_summary_tables`, `schema_context`, `execute`,
`_datasus_view_exists`, `is_ready`, `startup_status`, `mark_ready`, `status`, `_cached_row_count`,
`_store_row_count`, `discover_files`, `_ingest_locked`, `_remove_invalid_file`,
`_build_summary_tables_locked`, `_drop_summary_source`.

**`dev_tools/rebuild_group_cache.py` (3):** `_parquet_files`, `rebuild_group`, `main`.

## 6. Rollback

```powershell
# Restaurar arquivos-fonte a partir do backup integral (antes de nova tentativa):
$bkp = "_backups\cache_duplicado_20261012_113100"
Copy-Item "$bkp\db__group_cache.py"                 "db\group_cache.py" -Force
Copy-Item "$bkp\db__cache.py"                       "db\cache.py" -Force
Copy-Item "$bkp\dev_tools__rebuild_group_cache.py"  "dev_tools\rebuild_group_cache.py" -Force
python -m py_compile db\group_cache.py db\cache.py dev_tools\rebuild_group_cache.py
# Integridade: comparar Get-FileHash -Algorithm SHA256 com _backups\...\SHA256SUMS.txt
```

* **Rollback de dados:** o `DELETE` é transacional → se falhar, a tabela fica intacta;
  o `VACUUM` só compacta (não altera conteúdo). Se o `.duckdb` ficar estranho → reconstruir do
  Parquet (F5). **Os Parquet nunca são alterados**, portanto o pior caso é "tabela vazia" →
  sempre recuperável.
* **Rollback de código:** cópia do backup (acima) + `py_compile` + suíte focada (F7).
* **Regra de ouro:** nunca aplicar `--apply` sem `--dry-run` prévio e sem `keep_rows > 0`.

## 7. Critérios de aceite

1. Para cada grupo reparado: `linhas(tabela) / linhas(Parquet) == 1,000` e
   `parquet_path` distintos == nº de Parquet do grupo.
2. `file_manifest`: sem entradas apontando para caminho inexistente; total == nº de Parquet.
3. `datasus_cache` segue consultável (período nativo e comentários de coluna aplicados).
4. Nenhum Parquet criado/removido: `Flat=0` e contagem recursiva inalterada por grupo.
5. Suíte: coleta **193** sem `INTERNALERROR`; testes focados verdes;
   `test/test_repair_group_cache.py` verde.
6. Sem regressão de comportamento quando o manifesto está íntegro (guardas best-effort).

## 8. Registro de execução

| Fase | Estado | Evidência |
|---|---|---|
| F0 Inventário | ✅ | `inventario.txt` (métodos/docstrings/mojibake) |
| F1 Backups integrais | ✅ | `_backups/cache_duplicado_20261012_113100/` + `SHA256SUMS.txt` |
| F2 Ferramenta de reparo | ✅ | `dev_tools/repair_group_cache_duplication.py` (`--dry-run/--apply/--rewrite/--rebuild`) |
| F3 Reparo dos dados | ✅ 11 de 14 grupos | ver tabela abaixo (`sp` em execução; `bi`/`pa` bloqueados por espaço) |
| F4 Blindagem da ingestão | ✅ | `import_batch` e `ingest`: `DELETE FROM <cache_table> WHERE parquet_path IN (SELECT DISTINCT parquet_path FROM temp_batch)` antes do INSERT |
| F5 Rebuild (fallback) | ✅ | `CACHE_DIR = setting.cache_dir` (antes `<projeto>/cache`, vazio) + docstrings |
| F6 Docstrings | ✅ L1+L2 | `db/group_cache.py` 10, `db/cache.py` 24, `rebuild_group_cache.py` 3, ferramenta nova 100% |
| F7 Testes | ✅ (parcial) | `test/test_repair_group_cache.py` (6 testes) + 31 testes focados verdes |
| F8 Fechamento | ⏳ | docs + commit |

### 8.1 Resultado do reparo (medido, `--apply`)

| grupo | linhas antes | linhas depois | manifesto | razão | `.duckdb` antes → depois |
|---|---|---|---|---|---|
| `er` | 190.744 | 95.372 | 54 → 27 | 1,000 | 7,6 → 7,3 MiB |
| `acf` | 455.306 | 227.653 | 4.126 → 2.063 | 1,000 | 39,1 → 45,8 MiB |
| `rj` | 595.728 | 297.864 | 170 → 85 | 1,000 | 72,1 → 68,8 MiB |
| `ar` | 2.003.966 | 1.001.983 | 3.860 → 1.930 | 1,000 | 166,2 → 166,2 MiB |
| `amp` | 376.074 | 188.037 | 1.472 → 736 | 1,000 | 53,2 → 53,2 MiB |
| `atd` | 19.349.548 | 9.674.774 | 4.212 → 2.106 | 1,000 | 1.752,7 → 1.752,7 MiB |
| `ps` | 54.754.486 | 27.377.243 | 2.002 → 1.001 | 1,000 | 2.851,6 → 2.851,6 MiB |
| `rd` | 21.320.534 | 10.660.267 | 648 → 324 | 1,000 | 2.250,5 → 2.400,7 MiB |
| `aq` | 56.315.370 | 28.157.685 | 4.180 → 2.090 | 1,000 | 5.613,6 → 3.567,8 MiB |
| `am` | 263.666.606 | 131.833.303 | 3.865 → 1.949 | 1,000 | 14.199 → 8.002,7 MiB |
| `sp` | 1.257.474.998 | (em execução) | 2.200 → 1.100 | — | 47.294 MiB → — |

Em todos: `parquet_path` distintos = nº de Parquet, `file_manifest` sem órfãos e **nenhum Parquet criado/removido**.

### 8.2 Descobertas relevantes da execução

1. **`DELETE` corrige a duplicação; `VACUUM` NÃO devolve o espaço.** Medido com
   `python -u _diag_db_size.py`: `acf` ficou com 68 blocos usados e **115 livres**
   (de 183) — o arquivo mantém os blocos liberados. Onde as linhas duplicadas eram
   contíguas no FIM do arquivo, o checkpoint truncou (ex.: `aq` −2.045 MB,
   `am` −6.196 MB); nos demais o arquivo não encolhe (e pode crescer um pouco).
2. Para devolver espaço ao sistema é preciso **reescrever o arquivo**
   (`--rewrite`, `COPY FROM DATABASE` + troca com backup `.bak_<ts>` + validação),
   que exige espaço livre ≈ blocos usados do grupo.
3. **Restrição de disco:** `D:` tinha **12,2 GB livres** no início. Por isso:
   `sp` foi liberado após o `am` (ficou ~19,7 GB), mas **`bi` (≈44 GB) e `pa`
   (≈74 GB) NÃO cabem** no espaço livre atual — ficam pendentes de mais espaço
   (ou de reescrita em outro volume) antes de `--apply`/`--rewrite`.
4. A guarda `keep_rows > 0` (nunca esvaziar a tabela) e a validação de contagem na
   reescrita funcionaram como previsto; `--dry-run` foi idempotente (2ª passada: 0
   removidas, razão já 1,000).
5. **Mojibake preexistente** (62 linhas em `db/group_cache.py`, 3 em
   `rebuild_group_cache.py`) permanece — são comentários/logs ilegíveis, sem efeito
   funcional; correção opcional e separada (re-encode com backup + suíte).

### 8.3 Pendências explícitas

* `bi` e `pa`: `--apply` + `--rewrite` quando houver espaço livre
  (≈44 GB e ≈74 GB, +15% de margem).
* `--rewrite` nos grupos já reparados: opcional, para devolver os blocos livres
  (ex.: `python dev_tools/repair_group_cache_duplication.py --rewrite acf ar atd`).
* S1 (`DATASUS_CACHE_AS_VIEW=true`): exige ajustar `groups_needing_ingest()` para
  aceitar VIEW (senão o boot falha em 503) — follow-up, não faz parte deste reparo.
* Correção do mojibake (§8.2.5).

*Fim do plano.*
