# PLANO — Simplificação de `db/cache.py` e do acesso aos caches de grupo

> **Escopo**: reduzir a superfície de métodos de `DatasusCache`/`_GroupCache`,
> unificar a **criação** e o **acesso** dos caches por grupo e remover as
> versões legadas (modo "sem grupos", wrappers mortos, modo S1 removido).
>
> **Base das medições**: código em `main` no momento da análise
> (`db/cache.py` 2.848 linhas / `db/group_cache.py` 1.487 linhas) e cache real
> em `D:\datasus\cache` (inspeção somente-leitura via catálogo DuckDB).
>
> **Nada neste documento foi aplicado ao código** — é um plano de execução.

---

## 0. Sumário executivo

| Métrica | Hoje | Alvo | Δ |
|---|---|---|---|
| Métodos em `DatasusCache` | **63** | ~28 | **−35 (−56%)** |
| Métodos em `_GroupCache` | **35** | ~25 | **−10 (−29%)** |
| Total de métodos | **98** | ~53 | **−45 (−46%)** |
| `db/cache.py` (linhas) | 2.848 | ~1.250 | −56% |
| `db/group_cache.py` (linhas) | 1.487 | ~1.150 | −23% |
| Caminhos distintos de `ATTACH` | **3** | **1** | −2 |
| Métodos que testam existência de relação | **6** | **1** | −5 |
| Wrappers que só delegam | 5 | 0 | −5 |
| Métodos sem nenhum uso | 5 | 0 | −5 |
| Linhas de legado removíveis | ~715 | 0 | −715 |
| Flags de modo (`DATASUS_CACHE_AS_VIEW`, `DATASUS_UNIFIED_VIEW`) | 2 | **0** | −2 |

### Decisões fechadas (vinculantes para a execução)

1. **`DATASUS_CACHE_AS_VIEW` será REMOVIDO.** O cache de grupo e sempre uma
   **TABELA fisica no DuckDB** (`<grupo>.datasus_cache`). Nenhum codigo cria
   view sobre `read_parquet` como fonte de consulta.
2. **Backup primeiro.** Nenhuma fase comeca sem o backup da F0 concluido
   (arquivos de codigo + banco principal + `.env` + `SHA256SUMS.txt`).
3. **Todo metodo documentado ao final.** Apos as alteracoes, cada metodo
   (publico e privado) tem docstring dizendo o que faz, recebe e devolve.
4. **UTF-8, sem acentos em comentarios/docstrings.** Arquivos salvos em UTF-8
   (sem BOM); comentarios e docstrings em portugues sem acentuacao
   (ex.: `conexao`, `periodo`, `nao`, `grupo`). Mensagens de log e textos de UI
   permanecem como estao (podem ter acentos).
5. **Nunca editar o original: trabalhar em `<nome>_new.py`.** Todo arquivo que
   substitui um existente e criado/ajustado como `_new`, com checkpoint e
   confirmacao de estado ao final de **cada** ajuste. O original so muda no
   switch (2 comandos), depois de validado (ver §7.0).
6. **Testes somente no fim.** Nenhum `pytest` durante F0–F7.3. Em cada etapa
   valida-se apenas que o ajuste **foi implementado no codigo**: `py_compile` +
   confirmacao dos simbolos (`--absent`/`--present` em
   `dev_tools/checkpoint_etapa.py`) + `check_new_state.py`. Nenhum `import` de
   modulo e nenhuma suite sao executados no meio. A **unica** execucao de testes
   e rapida (`-m "not requires_llm and not requires_data and not slow"`) e
   acontece no **F7.4**, quando tudo estiver completo.

Ganhos além da contagem: **1 catálogo de grupos** (fim do *glob* duplo em disco),
**1 query de catálogo** em vez de 42 no *boot* e por invalidação, `status()`
sem `COUNT(*)` em 3,3 bilhões de linhas e correção de **2 grupos invisíveis**
(`ab`, `abo`) e **1 grupo em formato legado** (`bi`).

---

## 1. Inventário medido

### 1.1 Tamanho por arquivo

| Arquivo | Linhas | Métodos | Maiores métodos (linhas) |
|---|---|---|---|
| `db/cache.py` | 2.848 | 63 | `_ingest_locked` 201, `execute` 134, `_build_summary_tables_locked` 132, `_parse_periods` 118 |
| `db/group_cache.py` | 1.487 | 35 | `ingest` 227, `import_batch` 183, `_verify_period_integrity` 74, `_ingest_as_view` 61 |
| **Total** | **4.335** | **98** | — |

### 1.2 Métodos sem nenhum uso (verificado por AST + busca textual em todo o repo)

| Método | Arquivo:linha | Linhas |
|---|---|---|
| `clear_cache` | `cache.py:1114` | 15 |
| `_indexed_parquet_files` | `cache.py:1208` | 7 |
| `indexed_parquet_records` | `cache.py:1216` | 7 |
| `_file_periods` | `cache.py:1536` | 15 |
| `_ensure_period_columns_in_table` | `group_cache.py:298` | 26 |
| **Total** | | **70** |

### 1.3 Estado morto (atribuído e nunca lido)

| Atributo | Onde | Observação |
|---|---|---|
| `_unified_view_enabled` | `cache.py:126` | `DATASUS_UNIFIED_VIEW` não é lido em lugar nenhum; a view é **sempre** criada (`cache.py:1786`) |
| `_last_question_years/months/recent/agrupar_mes` | `cache.py:1460-1463` | *write-only* (nenhum leitor) |
| `_last_sql` | `cache.py:121` | lido apenas para repassar a `prepare_question_cache` (`cache.py:1994`) |

### 1.4 Código só usado pelo "modo legado" (sem grupos)

`self.groups` vazio nunca acontece na instalação real (14 bancos `.duckdb`),
mas o caminho continua no código, nos testes e no `lifespan`:

| Método | Linhas |
|---|---|
| `_ingest_locked` | 201 |
| `_cache_table_exists` | 35 |
| `select_files_for_question` | 24 |
| `_remove_invalid_file` | 21 |
| `_refresh_period_index` | 20 |
| `question_scope` | 19 |
| `_file_periods` | 15 |
| `clear_cache` | 15 |
| `_save_checkpoint` | 14 |
| `discover_files` | 12 |
| `_datasus_view_exists` | 11 |
| `_indexed_parquet_records` | 11 |
| `_load_checkpoint` | 9 |
| `_cached_parquet_files` | 9 |
| `_mark_in_db` | 8 |
| `_cached_row_count` | 8 |
| `_indexed_parquet_files` | 7 |
| `indexed_parquet_records` | 7 |
| `_store_row_count` | 7 |
| `_validate_parquet` | 3 |
| **Total** | **456** |

Somam-se ~120 linhas de ramos `if not self.groups:`/`if self.groups:` em
`__init__`, `_get_canonical_schema`, `_build_grupo_columns_cache`,
`prepare_question_cache`, `status`, `ingest` e `api/routes.py:56-83`.

---

## 2. Estado real do cache (medido)

`D:\datasus\cache` — inspeção via `duckdb_tables()`/`duckdb_columns()` (sem
`COUNT(*)`, usando `estimated_size`):

| grupo | `.duckdb` | linhas (est.) | `datasus_cache` | `atend_ano` | `file_manifest` | diagnóstico |
|---|---|---|---|---|---|---|
| `pa` | 79.371 MB | 1.499.143.297 | TABLE | Y | 958 | ok |
| `bi` | 37.993 MB | 929.218.917 | TABLE | **N** | **tabela ausente** | **schema v0 (legado)** |
| `sp` | 45.105 MB | 628.737.499 | TABLE | Y | 2.200 | ok |
| `am` | 7.632 MB | 131.833.303 | TABLE | Y | 3.893 | ok |
| `ad` | 3.168 MB | 45.776.372 | TABLE | Y | 4.212 | ok |
| `aq` | 3.403 MB | 28.157.685 | TABLE | Y | 4.180 | ok |
| `ps` | 2.720 MB | 27.383.074 | TABLE | Y | 2.002 | ok |
| `rd` | 2.290 MB | 10.660.267 | TABLE | Y | 648 | ok |
| `atd` | 1.672 MB | 9.704.447 | TABLE | Y | 4.212 | ok |
| `ar` | 159 MB | 1.090.680 | TABLE | Y | 3.860 | ok |
| `rj` | 69 MB | 349.968 | TABLE | Y | 170 | ok |
| `amp` | 51 MB | 253.194 | TABLE | Y | 1.472 | ok |
| `acf` | 46 MB | 227.653 | TABLE | Y | 4.126 | ok |
| `er` | 7 MB | 95.372 | TABLE | Y | 54 | ok |
| **`ab`** | — | — | — | — | — | **pasta de Parquet sem `.duckdb` → invisível** |
| **`abo`** | — | — | — | — | — | **pasta de Parquet sem `.duckdb` → invisível** |
| `datasus` (principal) | 83 MB | — | **VIEW** (`/mnt/d/datasus/...`) | N | 0 linhas | **lixo legado** |

**Total**: ~3,31 bilhões de linhas e ~180 GiB (183.683 MB) de `.duckdb` nos 14
grupos — mais 83 MB do banco principal.

Consequências que o plano precisa resolver:

1. **`ab`/`abo` nunca são ingeridos.** `_discover_groups` (`cache.py:905`) só
   varre `data_dir` quando **nenhum** `.duckdb` foi encontrado
   (`if not group_names and base.is_dir()`, `cache.py:952`). Como existem 13
   bancos, o *fallback* nunca roda e as pastas `cache_dir/ab` e `cache_dir/abo`
   (que existem) ficam fora do `self.groups`. Há checkpoints antigos de `ab`
   e `abo` em `main.cache_checkpoint` provando que já foram ingeridos.
2. **`bi.duckdb` está no formato antigo**: só tem `datasus_cache` (sem
   `file_manifest`, `period_index`, `group_checkpoint`, `atend_ano/atend_mes`).
   Ao reabrir, `reopen()` cria as tabelas e o manifesto nasce vazio →
   `pending_batch` considera **todos** os Parquet como novos → reingestão de
   38 GB.
3. **`datasus.duckdb` carrega duas views legadas** (`main.datasus_cache` e
   `main.datasus`) apontando para caminhos Linux inexistentes
   (`/mnt/d/datasus/cache/parquet/*.parquet`), sobra da migração de máquina
   descrita em `PLANO_REPARO_CACHE_DUPLICADO.md`.
4. **Tabelas mortas no banco principal**: `file_manifest` (0 linhas),
   `parquet_period_index` (18.682 linhas de caminhos que não existem) e
   `cache_checkpoint` (51 chaves `grupo:last_batch_index|total_files|last_updated_at`,
   duplicando `<grupo>.group_checkpoint`).
5. **`status()` é O(n) no total de linhas**: `cache.py:2231` executa
   `SELECT COUNT(*) FROM datasus_cache` por grupo (inclui `pa` 1,5 B e `bi`
   929 M). Um `GET /api/status` pode levar minutos.
6. **`select_groups_periods_for_question` (75 linhas) roda
   `SELECT DISTINCT <ano>,<mes> FROM <grupo>.datasus_cache` a cada pergunta**
   (`cache.py:1427`) — ~14 varreduras de 3,3 B de linhas por requisição.



---

## 3. Diagnóstico: onde o código duplica

### 3.1 `ATTACH` implementado 3 vezes (com 3 comportamentos diferentes)

| Local | Linhas | O que faz |
|---|---|---|
| `DatasusCache.__init__` (`cache.py:154-210`) | 57 | loop inline: `duckdb_databases()` + `ATTACH` RW + fallback RO + `ATTACH IF NOT EXISTS` para banco inexistente |
| `DatasusCache._ensure_groups_attached` (`cache.py:369-411`) | 43 | repete o mesmo loop, mensagens e fallback |
| `_GroupCache._ensure_attached` (`group_cache.py:189-252`) | 64 | faz o mesmo, mas com diagnóstico de *lock* e `duckdb.IOException` |

Efeitos colaterais: mensagens de erro divergentes para a mesma falha, 1 query
de catálogo por grupo no `__init__` **e** a cada pergunta, e três lugares para
corrigir quando o DuckDB mudar.

### 3.2 "A relação existe?" implementado 6 vezes

| Método | Arquivo | Tipo |
|---|---|---|
| `_cache_table_exists` | `group_cache.py:550` | tabela |
| `_cache_view_exists` | `group_cache.py:584` | view |
| `_cache_relation_exists` | `group_cache.py:610` | tabela **ou** view |
| `_datasus_view_exists` | `group_cache.py:619` | view `datasus` |
| `_cache_table_exists` | `cache.py:856` | tabela no banco **principal** (legado) |
| `_datasus_view_exists` | `cache.py:2164` | view no principal (legado) |

Cada um repete `duckdb_tables()`/`duckdb_views()` **e** o fallback
`information_schema`, com regras de `LOWER()` diferentes.

### 3.3 `_get_canonical_schema` custa 42 consultas de catálogo

Para 14 grupos: 2 queries de catálogo por grupo (tabela + view) + 1 `DESCRIBE`
por grupo = **42 idas ao catálogo** a cada invalidação (e invalidação ocorre em
`_discover_groups`, `ingest`, `register`...). O mesmo resultado sai de **uma**
consulta a `duckdb_columns()`.

### 3.4 Wrappers que só delegam (e o wrapper é o morto)

| Público | Privado | Situação |
|---|---|---|
| `indexed_parquet_records` | `_indexed_parquet_records` | **público sem uso**; o privado é usado por `ask.py` |
| `summary_tables` | `_summary_tables` | ambos usados; um só basta |
| `DatasusCache._extract_period_from_path` | `core.path_utils` | delegate de 6 linhas |
| `_GroupCache._grouped_cache_path` / `_extract_period_from_path` | `core.path_utils` | delegates de 3 e 6 linhas |
| `_GroupCache._all_cached_parquets` | `_cached_group_files` | mesmo resultado |

### 3.5 Dois sistemas de checkpoint para o mesmo dado

* `DatasusCache._save_checkpoint` grava em `main.cache_checkpoint` (chave
  `"<grupo>:last_batch_index"`) — usado só como telemetria dentro de
  `ingest()` (`cache.py:2651`).
* `_GroupCache._save_checkpoint/_load_checkpoint` grava em
  `<grupo>.group_checkpoint` — este é o checkpoint **de verdade**, usado por
  `pending_batch`/`import_batch`.

O primeiro está órfão (51 chaves antigas em `main.cache_checkpoint`) e pode ser
substituído por um `ingest_log` no catálogo.

### 3.6 Lógica de NLP/parse de período dentro do cache

`_parse_periods` (118 linhas) + `question_wants_monthly_grouping` (35 linhas)
são funções **puras** sobre texto (regex, normalização NFD, números por
extenso). Não tocam em banco, não usam estado — só estão no lugar errado.
`_fix_unterminated_sql_strings` (73) + `_fix_incomplete_sql_static` (31) idem.

### 3.7 Criação de grupo como efeito colateral de `__init__`

`__init__` faz, em sequência: descobre grupos → anexa bancos → cria 7 tabelas
no principal → carrega `cid10` → constrói todos os caches de colunas (14
arquivos JSON) → consolida o dicionário → registra conexão no `cid_map`.
São **7 responsabilidades** no construtor; `_create_group_pa.py:40` contorna
isso escrevendo direto em `cache.groups[...]` e chamando `_open_group_if_needed`
(privado).

### 3.8 Acesso ao grupo é feito por 4 portas diferentes

```python
cache.groups["pa"].ingest()                  # _create_group_pa.py
cache._open_group_if_needed("pa")            # cache.py (ingest/groups_needing_ingest)
group.reopen()                               # dev_tools, testes
group.connection.execute(...)                # status, finalize, _verify_period_integrity
```

Não há um único ponto que garanta "grupo pronto" (anexado + tabelas +
migrações + conexão), por isso existe a duplicação de `ATTACH`.

---

## 4. Arquitetura alvo

### 4.0 Regras de ouro (cache sempre materializado)

| Regra | Detalhe |
|---|---|
| `datasus_cache` e **TABELA** | Em `<grupo>.duckdb`, com `atend_ano`/`atend_mes` fisicos. **Nunca** view sobre Parquet. |
| `read_parquet` so na ingestao | Permitido para validar/carregar arquivo (`SELECT ... LIMIT 0`, `INSERT ... SELECT`). **Proibido** criar view (temp ou permanente) que sirva de fonte de consulta. |
| Views permitidas | Somente sobre TABELAS DuckDB: `<grupo>.datasus` (compatibilidade, `SELECT * FROM datasus_cache`) e `datasus_query_cache` (TEMP VIEW com o `UNION ALL` das tabelas). |
| Flag de modo | Nenhuma. O comportamento e unico: cache materializado no DuckDB. |

### 4.1 Um catálogo, N bancos (schema por grupo preservado)

```
datasus.duckdb  (principal — só metadados e agregados)
├── group_registry(name PK, db_path, source_dir, period_column,
│                  schema_version, rows_est, files, status, updated_at)
├── group_periods(grupo, atend_ano, atend_mes, PRIMARY KEY(grupo, atend_ano, atend_mes))
├── column_dictionary  (já existe: grupo, coluna, tipo)
├── cid10 / cid_prefixes / resumo_*
└── ingest_log(id, grupo, started_at, files, invalid, rows, status)

<grupo>.duckdb  (inalterado — dados)
├── datasus_cache (TABLE fisica; atend_ano/atend_mes materializados; SEM view sobre Parquet)
├── file_manifest / period_index / group_checkpoint / metadata
└── datasus (VIEW de compatibilidade sobre a TABELA — nao sobre Parquet)
```

`group_registry` e `group_periods` são **pequenas** (≈14 × meses). Passam a ser
a fonte de verdade para: descoberta, "precisa ingerir?", recorte temporal e
status — sem abrir conexão de grupo.

### 4.2 Criação: de 5 etapas para 2

**Hoje**

```
_discover_groups (glob cache/*.duckdb, fallback data_dir)
  → _GroupCache(...)                       # 1 objeto, sem validar nada
  → __init__: loop ATTACH (57 linhas)
  → reopen(): _ensure_attached + 4 CREATE TABLE + 2 migrações + comentários
  → ingest()/_open_group_if_needed()
```

**Proposta**

```python
# 1) reconciliação (uma vez, no boot) — só metadados
cache.sync_groups()          # registry ← disco (cache_dir/*.duckdb + cache_dir/<grupo>)

# 2) obtenção sob demanda — um único caminho
grupo = cache.group("pa")    # _GroupCache.ensure(): attach + DDL + migrações + conexão
```

`_GroupCache.ensure()` absorve `reopen()` + `_ensure_attached()` +
`_migrate_period_columns()` + `_apply_column_comments()`; é **idempotente** e é
o único lugar que faz `ATTACH`. `DatasusCache.__init__` deixa de anexar bancos.

### 4.3 Acesso: uma porta só

```python
def group(self, name: str) -> _GroupCache:
    """Devolve o cache do grupo ``name`` garantidamente pronto.

    Abre/anexa sob demanda (idempotente). Único ponto de acesso a um grupo.
    """
    try:
        grupo = self._groups[name]
    except KeyError:
        raise KeyError(f"grupo inexistente: {name!r}; registre com register_group()") from None
    grupo.ensure()
    return grupo
```

* `self.groups` passa a ser exposto como `MappingProxyType` (leitura somente);
  quem escrevia em `cache.groups[...]` usa `cache.register_group(...)`.
* `_GroupCache.connection` continua público por compatibilidade com
  `dev_tools/` e testes; `_GroupCache.scalar(sql)` é o caminho recomendado para
  leituras de um valor (aplica `_qualify_sql` automaticamente).
* `_open_group_if_needed` **desaparece** (3 usos internos + 1 em script → 4
  substituições por `cache.group(name)`).

### 4.4 Existência de relação: 1 método

```python
class _GroupCache:
    def exists(self, relation: str = "datasus_cache", kind: str = "any") -> bool:
        """True se ``relation`` existir como TABELA ou VIEW no banco do grupo.

        Uma única consulta a duckdb_tables()/duckdb_views() (com fallback
        information_schema apenas quando a conexão é isolada/:memory:).
        """
```

Uso: `grupo.exists()`, `grupo.exists("datasus")`, e
`grupo.exists("datasus_cache", kind="table")` quando for preciso distinguir
TABELA de VIEW (caso S1). Substitui 6 métodos (`cache.py` 2 + `group_cache.py` 4)
e elimina as divergências de `LOWER()`.

### 4.5 Schema canônico em 1 consulta

```python
def canonical_schema(self) -> dict[str, Any]:
    """Colunas por grupo + união, a partir de UMA leitura de duckdb_columns().

    Retorna {"all_columns": set[str], "group_columns": {grupo: set[str]},
             "safe_names": [...], "safe_to_name": {...}}
    """
```

Substitui as 42 consultas de `_get_canonical_schema` (`cache.py:413`) por 1
`SELECT database_name, table_name, column_name FROM duckdb_columns()`
filtrada por `table_name = 'datasus_cache'`, agrupada em Python.

### 4.6 Recorte temporal em 1 consulta ao catálogo

```python
def periods_for_question(self, question: str) -> list[tuple[str, str, str]]:
    """(grupo, ano, mes) do recorte, lido de main.group_periods (não dos grupos)."""
    disponiveis = self.connection.execute(
        "SELECT grupo, atend_ano, atend_mes FROM group_periods"
    ).fetchall()
    anos, meses, recent, _ = parse_periods(question, {(a, m) for _, a, m in disponiveis})
    ...
```

`group_periods` é recalculado em `_GroupCache.finalize()`/`ingest()` (uma vez
por grupo) — hoje o mesmo dado é obtido com 14 `SELECT DISTINCT` sobre 3,3 B de
linhas **por pergunta**.

### 4.7 `status()` sem `COUNT(*)`

`group_registry.rows_est` (de `duckdb_tables().estimated_size`, atualizado no
`finalize`) alimenta `/api/status`. `rows_est` continua sendo estimativa;
`rows_exact` só é calculado sob demanda por um endpoint/CLI de diagnóstico.


---

## 5. Redução/unificação dos grupos: o que faz sentido

### 5.1 Os 16 grupos mapeados são 5 "famílias" de período

| Campo nativo | Grupos | Qtde |
|---|---|---|
| `AP_DTINIC` (AAAAMMDD) | `acf`, `ad`, `am`, `amp`, `aq`, `ar`, `atd`, `ab`, `abo` | 9 |
| `DT_INTER` (AAAAMMDD) | `rj`, `rd`, `er` | 3 |
| `DT_ATEND` (AAAAMM) | `ps`, `bi` | 2 |
| `SP_DTINTER` (AAAAMMDD) | `sp` | 1 |
| `PA_CMP` (AAAAMM) | `pa` | 1 |

Os grupos **não** são partições de schema: são partições de **fonte**
(pastas do DATASUS). Colunas variam de 19 (`er`) a 120 (`rd`) e os volumes de
95 mil (`er`) a 1,5 bilhão (`pa`) de linhas.

### 5.2 Opções avaliadas

| # | Opção | Reduz métodos? | Custo | Risco | Veredito |
|---|---|---|---|---|---|
| **A** | **Catálogo único (`group_registry`/`group_periods`) + schema por grupo** | **Sim (~15)** | 1–2 dias | baixo | **recomendada — Fase 3** |
| B | Um único `.duckdb` com 14 schemas | pouco (~5) | dias + ~180 GiB de cópia | alto (lock/WAL único, perda de isolamento de memória) | **não fazer** |
| C | Uma tabela `datasus_cache(grupo, …)` | ~10 | reingestão de 3,3 B linhas (~horas/dias) | alto (checkpoint/OOM, perda de pruning por arquivo) | **não recomendada agora**; reavaliar se houver reingestão planejada |
| D | Fundir grupos da mesma família (ex.: `rj`+`rd`+`er`) | ~0 | reingestão | médio | **não compensa** (não reduz linhas/colunas; quebra ingestão incremental por pasta) |

**Conclusão**: o ganho de "reduzir grupos" está em **unificar os metadados e o
código**, não os arquivos. A opção A entrega praticamente toda a simplificação
visível (descoberta, "precisa ingerir?", período, status, colunas) sem tocar em
~180 GiB de dados e sem reingestão.

A opção C só passa a valer a pena se, no futuro, for necessário **descartar os
14 `.duckdb`** (ex.: ir para Parquet + views). Nesse cenário o caminho correto é
`read_parquet(..., union_by_name=true)` por grupo com uma **view** unificada —
que é exatamente o que `prepare_question_cache` já gera em memória — e não uma
tabela física única.

### 5.3 O que a opção A elimina de fato

| Item | Hoje | Depois |
|---|---|---|
| Descoberta de grupos | 78 linhas, 2 fontes inconsistentes, `ab`/`abo` invisíveis | ~20 linhas: `registry ⟷ disco` (união de `*.duckdb` **e** subpastas com Parquet) |
| "Precisa ingerir?" | 56 linhas, abre/fecha 14 conexões (`_cache_table_exists` + `pending_batch`) | ~10 linhas: 1 `SELECT` em `group_registry` |
| Recorte temporal | 75 linhas, 14 `SELECT DISTINCT` por pergunta | ~15 linhas: 1 `SELECT` em `group_periods` |
| Schema canônico | 83 linhas, 42 consultas de catálogo | ~25 linhas, 1 consulta |
| `/api/status` | `COUNT(*)` por grupo (minutos) | `SELECT` no registry (ms) |
| `register_group` | inexistente (scripts escrevem em `cache.groups`) | 1 método público |


---

## 6. Mapa método → destino

### 6.1 `db/cache.py` (63 → ~28)

| Método atual | Ação | Destino |
|---|---|---|
| `__init__` | enxugar (7 → 3 responsabilidades) | mantém |
| `database_path` (get/set) | mantém | — |
| `_configure_memory` | mantém | — |
| `close`, `status`, `is_ready`, `startup_status`, `mark_ready` | mantém | — |
| `_discover_groups` | **reescrever** (~78 → ~20) | `sync_groups()` |
| `groups_needing_ingest` | **reescrever** (~56 → ~10, registry) | mantém |
| `ensure_ready_sync` | mantém | — |
| `_open_group_if_needed` | **remover** | `group(name)` |
| — | **novo** | `group(name)`, `register_group(name)` |
| `_ensure_groups_attached` | **remover** (43) | `_GroupCache.attach()` |
| `_get_canonical_schema` | **reescrever** (83 → ~25) | `canonical_schema()` |
| `_invalidate_canonical_schema` | renomear | `invalidate_schema()` |
| `_grupo_source_signature` | mover | `_GroupCache.column_signature()` |
| `_build_grupo_columns_cache` | **remover** (163) | `_GroupCache.columns()` + `ColumnDictionaryStore` |
| `_build_all_grupo_columns_caches` | **remover** (12) | inline em `refresh_column_dictionary` |
| `get_grupo_columns` | renomear | `columns(grupo=None)` |
| `get_column_dictionary` + `refresh_column_dictionary` | **unificar** | `column_dictionary(refresh=False)` |
| `_parse_periods` | **mover** (118) | `db/period_parser.py` |
| `question_wants_monthly_grouping` | **mover** (35) | `db/period_parser.py` |
| `select_groups_periods_for_question` | **reescrever** (75 → ~15) | `periods_for_question()` |
| `prepare_question_cache` | delegar o SQL | `db/unified_view.py` |
| `_referenced_columns` | **mover** (25) | `db/unified_view.py` |
| `execute` | mantém (chama `sql_repair`) | — |
| `_fix_unterminated_sql_strings`, `_fix_incomplete_sql_static` | **mover** (104) | `db/sql_repair.py` |
| `schema_context` | mantém | — |
| `_load_cid10_table`, `_cid_diagnosis_columns`, `_populate_cid_prefixes`, `get_cid_prefixes` | **mover** (150) | `db/cid_prefixes.py` |
| `ingest` | enxugar (remove `_save_checkpoint`, adiciona registry/log) | mantém |
| `build_summary_tables`, `_build_summary_tables_locked`, `summary_tables`, `_summary_tables` | **mover** (205) | `db/summary.py` (lê as TABELAS de grupo, não Parquet; delegates de 1 linha ficam) |
| `_drop_summary_source` | **remover** (10) | — (não existe mais view `summary_source`) |
| `_save_checkpoint`, `_load_checkpoint` | **remover** (23) | registry/`ingest_log` |
| `_ingest_locked`, `discover_files`, `_mark_in_db`, `_remove_invalid_file`, `_validate_parquet`, `_cached_row_count`, `_store_row_count` | **remover** (260) | — (legado) |
| `_cache_table_exists`, `_datasus_view_exists` | **remover** (46) | `_GroupCache.exists()` |
| `_cached_parquet_files`, `_indexed_parquet_files`, `indexed_parquet_records`, `_indexed_parquet_records`, `_refresh_period_index`, `_file_periods` | **remover** (69) | — (legado) |
| `select_files_for_question`, `question_scope` | **remover** (43) | `periods_for_question()` / `groups` |
| `clear_cache` | **remover** (15) | `dev_tools/` se necessário |
| `query_kb`, `_fetch_scalar` | mantém | — |

### 6.2 `db/group_cache.py` (35 → ~25)

| Método atual | Ação | Destino |
|---|---|---|
| `__init__`, `close`, `discover_files`, `_configure_memory`, `_qualify_sql`, `_apply_column_comments` | mantém | — |
| `reopen` | renomear/enxugar | `ensure()` |
| `_ensure_attached` | **tornar público** e único `ATTACH` | `attach()` |
| `_cache_table_exists`, `_cache_view_exists`, `_cache_relation_exists`, `_datasus_view_exists` | **unificar** (86 → ~25) | `exists(relation, kind="any")` |
| `_migrate_period_columns` + `_backfill_period_columns` | **unificar** | `_migrate_period_columns()` |
| `_period_col_projection` + `_period_projection_exprs` | **unificar** (42 → ~25) | `period_projection(col)` |
| `_ensure_period_columns_in_table` | **remover** (26, sem uso) | — |
| `_grouped_cache_path`, `_extract_period_from_path` | **remover** (9) na F5.1 | usar `core/path_utils` direto |
| `_refresh_period_index` | renomear | `period_index(files=None)` |
| `_save_checkpoint` + `_load_checkpoint` | **unificar** (26 → ~12) | `checkpoint(value=None)` |
| `_scalar` | renomear | `scalar()` |
| `_cached_group_files` + `_all_cached_parquets` | **unificar** (13 → ~6) na F5.1 | `cached_files()` |
| `_cache_as_view_enabled`, `_materialize_as_view`, `_ingest_as_view`, `_view_period_native_expr`, `_VIEW_PERIOD_EXPR` | **remover** (~140) — decisao fechada: cache sempre materializado (§4.0) | ver F5 |
| `_verify_period_integrity` | mantém | — |
| `_ensure_view_and_index` + `finalize` | **unificar** (finalize delega) | `finalize()` |
| `ingest`, `pending_batch`, `import_batch` | mantém (núcleo) | — |
| — | **novos** | `columns()`, `periods()`, `status()`, `column_signature()` |

**Resultado**: `DatasusCache` 63 → 28 métodos; `_GroupCache` 35 → 25.
Total **98 → ~53 (−46%)**.

### 6.3 Contagem exata de remoções (o que "morre" de fato)

| Categoria | `cache.py` | `group_cache.py` | **Total** |
|---|---|---|---|
| **Saem da classe** (deixam de existir nela) | 37 | 15 | **52** |
| ├─ descartados por completo (nada é reaproveitado) | 22 | 7 | **29** |
| ├─ absorvidos por outro método (fusão) | 6 | 8 | **14** |
| └─ movidos para módulo novo (continuam vivos) | 9 | 0 | **9** |
| Renomeados (não morrem: mesmo corpo, outro nome) | 7 | 4 | **11** |
| Métodos novos | 2 | 5 | **7** |
| **Saldo** (63 / 35 → 28 / 25) | −35 | −10 | **−45** |

Em resumo: **52 métodos saem das classes** — **43 deixam de existir**
(29 descartados + 14 absorvidos por fusão) e **9 mudam para módulos novos**.
Além disso, **11 são renomeados** (não morrem) e **7 são novos** (2 + 5).

Detalhe dos **29 descartados por completo**:

* **5 nunca usados** (verificado por AST + busca): `clear_cache`,
  `_indexed_parquet_files`, `indexed_parquet_records`, `_file_periods`,
  `_ensure_period_columns_in_table`;
* **17 do modo legado** (sem grupos): `_ingest_locked`, `discover_files`,
  `_mark_in_db`, `_remove_invalid_file`, `_validate_parquet`,
  `_save_checkpoint`, `_load_checkpoint`, `_cached_row_count`,
  `_store_row_count`, `_cached_parquet_files`, `_indexed_parquet_records`,
  `_refresh_period_index`, `select_files_for_question`, `question_scope`,
  `_cache_table_exists`, `_datasus_view_exists`, `_extract_period_from_path`;
* **2 delegates triviais**: `_grouped_cache_path`, `_extract_period_from_path`
  (o de `_GroupCache` — o homônimo de `cache.py` já está no bloco de 17);
* **4 do modo S1** (`DATASUS_CACHE_AS_VIEW`, **removidos por decisao** — cache
  sempre materializado em tabela DuckDB): `_cache_as_view_enabled`,
  `_materialize_as_view`, `_ingest_as_view`, `_view_period_native_expr`;
* **1 pela regra "sem view sobre Parquet"**: `_drop_summary_source` (os resumos
  passam a ler `<grupo>.datasus_cache`, não `read_parquet`).

Os **14 fundidos** (código absorvido, não perdido):

| Fusão | Métodos que somem |
|---|---|
| `_ensure_groups_attached` + `__init__` → `_GroupCache.attach()` | 1 |
| `_open_group_if_needed` → `group(name)` | 1 |
| `_build_all_grupo_columns_caches` + `_build_grupo_columns_cache` → `_GroupCache.columns()` | 2 |
| `_summary_tables` → `summary_tables` | 1 |
| `get_column_dictionary` → `column_dictionary` | 1 |
| 4 sondas de relação → `exists()` | 3 |
| `_period_col_projection` + `_period_projection_exprs` → `period_projection` | 1 |
| `_save_checkpoint` + `_load_checkpoint` → `checkpoint` | 1 |
| `_all_cached_parquets` + `_cached_group_files` → `cached_files` | 1 |
| `_backfill_period_columns` → `_migrate_period_columns` | 1 |
| `_ensure_view_and_index` → `finalize` | 1 |
| **Total** | **14** |

> **Decisao fechada**: S1 removido. O `ingest` sempre materializa TABELA; nao ha
> caminho alternativo. Os 4 metodos e o atributo `_VIEW_PERIOD_EXPR` saem na F5,
> junto com `test/test_cache_as_view_s1.py` e a linha `DATASUS_CACHE_AS_VIEW`
> do `.env`.



---

## 7. Execução em fases

**F0 é pré-requisito obrigatório** (sem backup verificado, nada começa).
As fases F1–F6 são reversíveis e a ordem indicada minimiza retrabalho (as
dependências reais estão na matriz §7.1); **F7 fecha a entrega** (documentação de
todos os métodos). Cada fase é executada como uma sequência de **etapas atômicas**
(§7.1), com checkpoint (§7.0.2) e registro em `progresso.json` (§7.2) — se algo
travar, retoma-se da primeira etapa não concluída.

### 7.0 Regra do arquivo `_new` (obrigatória em todas as fases)

**Nunca editar o arquivo original durante F1–F7.** Todo arquivo que substitui um
existente é criado como `<nome>_new.py`, recebe os ajustes ali e só vira o
arquivo definitivo no *switch* (2 comandos), depois de validado.

> Nas fases abaixo, "apagar", "reescrever", "renomear" e "mover" referem-se
> sempre ao **arquivo `_new` correspondente** (ex.: `db/cache_new.py`), nunca ao
> original.

| Original | Arquivo de trabalho | Vira definitivo no fim de |
|---|---|---|
| `db/cache.py` | `db/cache_new.py` | F6 |
| `db/group_cache.py` | `db/group_cache_new.py` | F6 |
| `api/routes.py` | `api/routes_new.py` | F5 |
| `api/llm_service_parts/base.py` | `api/llm_service_parts/base_new.py` | F5 |
| `api/llm_service_parts/ask.py` | `api/llm_service_parts/ask_new.py` | F5 |
| `_create_group_pa.py` | `_create_group_pa_new.py` | F3 |
| `dev_tools/rebuild_group_cache.py` | `dev_tools/rebuild_group_cache_new.py` | F3 |
| `test/test_*.py` (alterados) | `test/test_*_new.py` | junto com a fase |

Arquivos **genuinamente novos** (`db/period_parser.py`, `db/sql_repair.py`,
`db/summary.py`, `db/cid_prefixes.py`, `db/unified_view.py`,
`dev_tools/check_new_state.py`) não têm versão anterior para proteger e usam o
nome definitivo. Se preferir padronizar tudo, crie `<nome>_new.py` e renomeie no
switch — o protocolo é o mesmo.

#### 7.0.1 Ciclo de cada ajuste

1. editar **somente** `X_new.py` (o original permanece byte-a-byte intacto);
2. rodar o checkpoint (§7.0.2);
3. **confirmar o estado** no relatório (§7.0.3) — sem esse relatório o ajuste
   **não** é considerado concluído;
4. salvar o snapshot em `_backups/wip/<AAAAMMDD_HHMMSS>/X_new.py` + `estado.txt`.

#### 7.0.2 Checkpoint (executar ao final de cada ajuste)

Forma recomendada (um comando, sem quoting de shell):

```powershell
python -u dev_tools/checkpoint_etapa.py F1.2 db/cache_new.py --absent _unified_view_enabled _last_question_years
```

O `--absent` confirma que o simbolo **saiu** do codigo; o `--present` confirma
que o novo metodo/atributo **entrou**. Se qualquer verificacao falhar, a etapa
**nao** e marcada como `done`.

Equivalente manual (quando precisar de passo a passo):

```powershell
$ts  = Get-Date -Format 'yyyyMMdd_HHmmss'
$wip = "_backups/wip/$ts"
$arquivo = "db/cache_new.py"                     # arquivo da etapa atual
New-Item -ItemType Directory -Force $wip | Out-Null

python -m py_compile $arquivo                    # sintaxe
python -u dev_tools/check_new_state.py $arquivo | Tee-Object "$wip/estado.txt"
Copy-Item $arquivo (Join-Path $wip ($arquivo -replace '[:\\/]','__')) -Force
Get-FileHash $arquivo -Algorithm SHA256 | Out-File "$wip/estado.txt" -Append
```

#### 7.0.3 Relatório obrigatório (colar no chat e salvar em `estado.txt`)

| Campo | Exemplo |
|---|---|
| arquivo | `db/cache_new.py` |
| linhas | 2.812 |
| métodos | 63 |
| docstrings faltando | 0 |
| acentos em comentários/docstrings | 0 |
| sintaxe (`py_compile`) | OK |
| UTF-8 sem BOM | OK |
| SHA256 | `a1b2…` |
| mtime | `2026-10-18 14:32:07` |
| testes | **não executados** (só no F7.4) |
| símbolos ausentes/presentes | OK (conforme `--absent`/`--present`) |
| status | **OK** / FALHA |

#### 7.0.4 Recuperação (1 comando)

| Situação | Comando |
|---|---|
| desfazer o **último ajuste** | `Copy-Item _backups/wip/<ts_anterior>/db__cache_new.py db/cache_new.py -Force` |
| voltar ao **zero** (original intacto) | `Remove-Item db/cache_new.py` — o original nunca foi tocado |
| desfazer o **switch** | `Move-Item _backups/switch/<ts>/db__cache.py db/cache.py -Force` |
| recuperar de corrupção total | restaurar a F0 (`_backups/simplificacao_cache_<ts>/`) + `git checkout -- <arquivo>` |

#### 7.0.5 Switch (único, no fim — F7.4)

Durante F1–F7 o app continua usando os **originais**. O `cache_new` importa
`db.group_cache_new` (e os demais `_new`), de modo que o par novo é testado
junto. No fim, F7.4 troca os imports (`_new` → definitivo) e move todos os
`_new` de uma só vez — não existe estado "meio trocado".

```powershell
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$sw = "_backups/switch/$ts"; New-Item -ItemType Directory -Force $sw | Out-Null

# 1) antes: ajustar os imports nos _new (db.cache_new -> db.cache, etc.)
# 2) backup do original + troca, um arquivo por vez, no mesmo comando
Get-ChildItem -Recurse -Filter *_new.py | ForEach-Object {
  $orig = $_.FullName -replace '_new\.py$', '.py'
  Copy-Item $orig (Join-Path $sw ($orig -replace '[:\\/]','_')) -Force
  Move-Item $_.FullName $orig -Force
}
python -m pytest -q -m "not requires_llm and not requires_data and not slow"  # unica execucao
# falhou? rodar o loop inverso a partir de $sw
```

Regras adicionais:

* nenhum módulo de produção importa `*_new` — o switch é o **único** ponto de
  troca e acontece uma vez (F7.4);
* `*_new.py` no `.gitignore` (não versionar WIP);
* `_backups/wip/`, `_backups/switch/` e `_backups/` já são ignorados pelo git;
* antes do switch: `py_compile` + `import db.cache_new, db.group_cache_new`
  verdes (checagem estatica, **sem pytest**) — os testes rodam uma unica vez no
  fim do F7.4.

#### 7.0.6 Princípio das etapas atômicas

Cada fase abaixo é uma sequência de **etapas atômicas** (§7.1). Regra: **1
etapa = 1 entrega = 1 checkpoint**. Nenhuma etapa edita mais de um arquivo `_new`
e nenhuma deve passar de ~45 min. Se estourar o tempo, **pare e faça o
checkpoint** — o `_new` é sempre um estado válido.

### 7.1 Matriz de etapas (separação, duração e retomada)

Bloco 1 — preparação, limpeza e unificação (F0–F3):

| ID | Entrega (uma só) | Arquivo | ~min | Depende de | Checkpoint ao final |
|---|---|---|---|---|---|
| F0.1 | backup dos arquivos | — | 10 | — | `SHA256SUMS.txt` |
| F0.2 | export do banco principal | — | 5 | — | `_backups/main_db` |
| F0.3 | baseline (latências; `pytest` **opcional**, antes de começar) | — | 10 | F0.1 | `baseline.txt` |
| F0.4 | criar os `_new` + `.gitignore` | `*_new.py` | 5 | F0.1 | `wip_inicial.txt` |
| F1.1 | remover 4 métodos mortos | `db/cache_new.py` | 20 | F0.4 | wip + estado |
| F1.2 | remover estado morto (`_unified_view_enabled`, `_last_question_*`) | `db/cache_new.py` | 10 | F1.1 | wip |
| F1.3 | remover `_ensure_period_columns_in_table` | `db/group_cache_new.py` | 10 | F1.1 | wip |
| F1.4 | checkpoint estático (compile + import, **sem testes**) | — | 5 | F1.1–F1.3 | relatório |
| F2.1 | `attach()` (único ATTACH) | `db/group_cache_new.py` | 30 | F1.4 | wip |
| F2.2 | `__init__` + `_ensure_groups_attached` via `attach()` | `db/cache_new.py` | 30 | F2.1 | wip |
| F2.3 | `exists()` substitui 4 sondas | `db/group_cache_new.py` | 40 | F2.2 | wip |
| F2.4 | `canonical_schema()` em 1 query | `db/cache_new.py` | 30 | F2.3 | wip |
| F2.5 | checkpoint estático (**sem testes**) | — | 5 | F2.4 | relatório |
| F3.1 | `db/group_registry.py` (DDL + backfill) | novo | 60 | F2.5 | wip |
| F3.2 | `sync_groups()` (corrige `ab`/`abo`) | `db/cache_new.py` | 45 | F3.1 | wip |
| F3.3 | `group()` + `register_group()` | `db/cache_new.py` | 30 | F3.2 | wip |
| F3.4 | `groups_needing_ingest`/`status` via registry | `db/cache_new.py` | 30 | F3.3 | wip |
| F3.5 | `periods_for_question` + `group_periods` | `db/cache_new.py` | 45 | F3.4 | wip |
| F3.6 | checkpoint estático + smoke do registry (**sem testes**) | — | 10 | F3.5 | relatório |

Bloco 2 — extração, remoção do legado, documentação e switch (F4–F7):

| ID | Entrega (uma só) | Arquivo | ~min | Depende de | Checkpoint ao final |
|---|---|---|---|---|---|
| F4.1 | `db/period_parser.py` | novo | 45 | F3.6 | wip |
| F4.2 | `db/sql_repair.py` | novo | 30 | F3.6 | wip |
| F4.3 | `db/summary.py` (extrai como está) | novo | 45 | F3.6 | wip |
| F4.4 | `db/cid_prefixes.py` | novo | 45 | F3.6 | wip |
| F4.5 | `db/unified_view.py` | novo | 45 | F3.6 | wip |
| F4.6 | checkpoint estático (**sem testes**) | — | 5 | F4.1–F4.5 | relatório |
| F5.1 | remover S1 (código) | `db/group_cache_new.py` | 45 | F4.6 | wip |
| F5.2 | `read_parquet` só na ingestão + resumos leem tabelas | `group_cache_new` + `db/summary.py` | 45 | F5.1, F4.3 | wip |
| F5.3 | remover modo legado (código + api + testes) | `cache_new`, `routes_new`, `base_new`, `ask_new` | 60 | F5.1 | wip |
| F5.4 | checkpoint estático (**sem testes**) | — | 5 | F5.1–F5.3 | relatório |
| F5.5 | limpeza do banco principal (views/tabelas mortas) | banco | 10 | F5.4 + F0.2 | `limpeza.txt` |
| F5.6 | rebuild de `bi` (longo, com `--resume`) | banco | 60–240 | F5.4 | `group_checkpoint` + log |
| F6.1 | renomes/aliases + README | `cache_new`, `group_cache_new` | 30 | F5.4 | wip |
| F7.1 | docstrings de `cache_new` | `db/cache_new.py` | 45 | F6.1 | wip |
| F7.2 | docstrings de `group_cache_new` | `db/group_cache_new.py` | 45 | F6.1 | wip |
| F7.3 | docstrings dos módulos novos + api | 6 arquivos | 45 | F7.1 | wip |
| F7.4 | imports `_new`→final + switch único + **testes rápidos (única execução)** + tag | originais | 30 | F7.1–F7.3 | `switch/final` + `git tag` |

**Paralelizável**: F4.1–F4.5 são independentes entre si (podem ser feitas em
qualquer ordem ou por pessoas diferentes). **Crítico** (não paralelizar):
F2.1→F2.3, F3.1→F3.5, F5.1→F5.3 e F7.1→F7.4.

**Validação da etapa** (F1.4, F2.5, F3.6, F4.6, F5.4) = **sem pytest**:

```powershell
python -u dev_tools/checkpoint_etapa.py F1.4 db/cache_new.py --absent _file_periods
```

ou, manualmente:

```powershell
python -m py_compile db/cache_new.py db/group_cache_new.py
python -u dev_tools/check_new_state.py
```

> **Regra (§0.6): nenhum teste e nenhum import durante o ajuste.** A validação
> confirma apenas que o codigo mudou como planejado (`--absent`/`--present`) e
> que continua sintaticamente valido. A suite roda **uma única vez**, no
> **F7.4**, e apenas o subconjunto rápido:
> `python -m pytest -q -m "not requires_llm and not requires_data and not slow"`.
> A suite completa (opcional) fica para depois da entrega.
> Testes que abrem o cache real (`D:\datasus\cache`) nunca entram no fluxo — os
> testes do repositório já usam diretórios temporários.

> **Wrappers remanejados para a etapa em que o chamador desaparece** (evita
> quebrar S1/legado antes da hora):
> `_summary_tables` → F4.3; `_GroupCache._grouped_cache_path`,
> `_GroupCache._extract_period_from_path` e `_all_cached_parquets` → F5.1;
> `DatasusCache._extract_period_from_path` → F5.3. A contagem em §6.3 não muda
> (continuam sendo 5 métodos removidos, em etapas diferentes).

### 7.2 Protocolo de retomada (`progresso.json`)

Todo o estado de execução vive em **um arquivo**, fora do git:

```
_backups/simplificacao_cache_<ts>/progresso.json
```

```json
{
  "versao": 1,
  "iniciado_em": "2026-10-18T14:00:00",
  "ts_backup": "20261018_140000",
  "etapas": {
    "F0.1": "done", "F0.2": "done", "F0.3": "done", "F0.4": "done",
    "F1.1": "doing", "F1.2": "pending"
  },
  "wip_atual": "_backups/wip/20261018_143207",
  "ultimo_erro": null
}
```

Regras:

0. criar uma vez com
   `python -u dev_tools/refactor_status.py --init <...>/progresso.json` (F0.1) —
   idempotente: se já existe, não sobrescreve;
1. ao **começar** uma etapa: `"doing"`; ao terminar + checkpoint: `"done"`;
2. se falhar: `"bloqueada"` + `ultimo_erro` (mensagem curta) — a etapa seguinte
   não começa;
3. **retomar = executar a primeira etapa que não está `done`** (nunca reiniciar
   do zero);
4. o `progresso.json` é atualizado **no mesmo comando** do checkpoint (§7.0.2).

```powershell
$p = "_backups/simplificacao_cache_<ts>/progresso.json"
$j = Get-Content $p -Raw | ConvertFrom-Json
$j.etapas.'F1.1' = 'done'
$j.wip_atual      = "_backups/wip/<ts_do_checkpoint>"
$j | ConvertTo-Json -Depth 5 | Out-File $p -Encoding utf8
python -u dev_tools/refactor_status.py      # mostra a próxima etapa
```

`dev_tools/refactor_status.py` (novo, ~40 linhas — Anexo G) imprime:
etapa atual, próxima pendente, `wip_atual`, hash/linhas dos `_new` e o comando de
retomada. Se o `progresso.json` não existir, ele assume tudo `pending` e sugere
começar pela F0.1.

#### 7.2.1 Como retomar em cada cenário

| Interrupção em | Estado do projeto | Retomada |
|---|---|---|
| edição de um `_new` | originais intactos | abrir o `_new`, rodar checkpoint, seguir do próximo item (F<sub>x.y</sub>) |
| testes finais falharam (F7.4) | switch feito | rodar o loop inverso de `_backups/switch/<ts>/` e corrigir no `_new` |
| durante o switch (F7.4) | parte trocada | rodar o loop inverso a partir de `_backups/switch/<ts>/` |
| limpeza do banco (F5.5) | views/tabelas dropadas | `RESTORE` do export F0.2 (ou recriar o DDL salvo) |
| rebuild de `bi` (F5.6) | ingestão parcial | `python dev_tools/rebuild_group_cache.py --only bi --resume` |
| build dos resumos | tabelas `resumo_*` incompletas | reexecutar `build_summary_tables()` (DROP + CREATE, idempotente) |
| terminal/sessão perdida | tudo em disco | `python -u dev_tools/refactor_status.py` e seguir a próxima pendente |

### 7.3 Regras anti-travamento

| Regra | Por quê |
|---|---|
| Nenhuma etapa > 45 min; se estourar, checkpoint e parar | evita sessão longa sem estado salvo |
| Um arquivo `_new` por etapa | rollback de 1 comando, diff pequeno |
| Nenhuma operação longa dentro do `lifespan`/boot | não bloqueia a API; usar `dev_tools` |
| App **parado** antes de tocar em `.duckdb` | evita `IOException: Could not set lock` |
| Nenhum `pytest` durante F0–F7.3 (só `py_compile`/`import`) | evita paradas longas no meio do ajuste; testes só no F7.4 (§0.6) |
| Tempo-limite em comando externo | `Start-Process` + `Wait-Process -Timeout` (§7.4) |
| Falha de lock: parar e reportar (sem retry infinito) | retry infinito é o principal sintoma de travamento |
| `progresso.json` sempre atualizado | retomada sem redescobrir o estado |

### 7.4 Operações longas (fora do caminho crítico)

`rebuild de bi` (38 GB), `build_summary_tables()` e a primeira ingestão **nunca**
entram no boot nem no meio de uma etapa de código. Executar em processo separado,
com log e timeout:

```powershell
$log = "_backups/rebuild_bi_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
$proc = Start-Process python -ArgumentList @(
  "-u","dev_tools/rebuild_group_cache.py","--only","bi","--resume"
) -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"

try { $proc | Wait-Process -Timeout 14400 -ErrorAction Stop }   # 4 h
catch { $proc | Stop-Process -Force; "TIMEOUT - ver $log" }
```

Enquanto isso, a aplicação pode subir com `group_registry.status='legacy'` (o
`/api/ask` responde 503 **apenas** para consultas que dependam de `bi`). A
retomada usa o `group_checkpoint` já existente (`pending_batch` não reprocessa o
que foi importado).

### F0 — Backup obrigatório e baseline (0,5 dia, risco ~0)

> **Nenhuma alteração de código antes de concluir esta fase.** Sem backup
> verificado, a fase seguinte não começa.

#### F0.1 Backup dos arquivos que serão alterados

Criar `_backups/simplificacao_cache_<AAAAMMDD_HHMMSS>/`, preservando a árvore
no nome (`db__cache.py`, `api__routes.py`, ...), com `SHA256SUMS.txt` e
`MANIFEST.txt` (data + `git rev-parse HEAD`). `_backups/` e `.env` já estão no
`.gitignore`, então o backup (inclusive do `.env`) não vai para o repositório.

| Arquivo | Fase que altera | Motivo |
|---|---|---|
| `db/cache.py` | F1–F7 | refactor principal |
| `db/group_cache.py` | F1–F7 | ATTACH/exists/remoção do S1 |
| `api/routes.py` | F5 | remover ramos do modo legado |
| `api/llm_service_parts/base.py` | F5 | `_determine_relevant_grupos` |
| `api/llm_service_parts/ask.py` | F5 | fallbacks `_indexed_parquet_records` |
| `_create_group_pa.py` | F3 | usar `register_group`/`group` |
| `dev_tools/rebuild_group_cache.py` | F3 | API pública de grupos |
| `test/test_cache_as_view_s1.py` | F5 | **removido** (S1) |
| `test/test_atend_columns_f3.py` | F2/F5 | ajustes de API |
| `test/test_canonical_schema_s2.py` | F2/F5 | ajustes de API |
| `test/test_unified_view_s3.py` | F1/F5 | flag morta / modo legado |
| `test/test_limite_execute_f7.py` | F2 | `_discover_groups`/API de grupos |
| `.env` | F5 | remover `DATASUS_CACHE_AS_VIEW` |

```powershell
$ts  = Get-Date -Format 'yyyyMMdd_HHmmss'
$dst = "_backups/simplificacao_cache_$ts"
New-Item -ItemType Directory -Force $dst | Out-Null
$files = @(
  'db/cache.py','db/group_cache.py','api/routes.py',
  'api/llm_service_parts/base.py','api/llm_service_parts/ask.py',
  '_create_group_pa.py','dev_tools/rebuild_group_cache.py',
  'test/test_cache_as_view_s1.py','test/test_atend_columns_f3.py',
  'test/test_canonical_schema_s2.py','test/test_unified_view_s3.py',
  'test/test_limite_execute_f7.py','.env'
)
foreach ($f in $files) {
  Copy-Item $f (Join-Path $dst ($f -replace '[\\/]','__')) -Force
}
Get-FileHash $files -Algorithm SHA256 | Out-File "$dst/SHA256SUMS.txt"
git rev-parse HEAD              | Out-File "$dst/MANIFEST.txt"
"backup=$ts"                    | Out-File "$dst/MANIFEST.txt" -Append

# cria o progresso (todas as etapas pending) para retomada desde a F0
python -u dev_tools/refactor_status.py --init "$dst/progresso.json"
```

#### F0.2 Backup do banco principal (metadados)

`datasus.duckdb` tem 83 MB e guarda `cid10`, `column_dictionary`, `resumo_*` e
as views/tabelas legadas que serão removidas. Exportar antes:

```powershell
python -c "import duckdb; c=duckdb.connect(r'D:\datasus\cache\datasus.duckdb', read_only=True); c.execute(r'''EXPORT DATABASE '_backups/main_db' (FORMAT PARQUET)''')"
```

Guardar também o DDL das views legadas (para recriar se necessário):

```sql
SELECT database_name, schema_name, view_name, sql
FROM duckdb_views() WHERE database_name = 'datasus';
```

Os `.duckdb` dos grupos (~180 GiB) **não** são copiados: o refactor não altera
dados, apenas metadados. O rebuild de `bi` é a única operação sobre dados e tem
procedimento próprio na F5.

#### F0.3 Baseline

1. (Opcional, **antes** de qualquer alteração) `python -m pytest -q` apenas para
   registrar o nº de testes verdes. **Não repetir** durante F1–F7.3.
2. Medir `GET /api/status` e 5 perguntas de referência (tempo + linhas).
3. Salvar em `_backups/simplificacao_cache_<ts>/baseline.txt`.

#### F0.4 — Criar os arquivos de trabalho `_new`

Regra §7.0: os originais não são editados; o trabalho ocorre nos `_new`.

```powershell
Copy-Item db/cache.py                        db/cache_new.py
Copy-Item db/group_cache.py                  db/group_cache_new.py
Copy-Item api/routes.py                      api/routes_new.py
Copy-Item api/llm_service_parts/base.py      api/llm_service_parts/base_new.py
Copy-Item api/llm_service_parts/ask.py       api/llm_service_parts/ask_new.py
Copy-Item _create_group_pa.py                _create_group_pa_new.py
Copy-Item dev_tools/rebuild_group_cache.py   dev_tools/rebuild_group_cache_new.py
Copy-Item test/test_cache_as_view_s1.py      test/test_cache_as_view_s1_new.py
Copy-Item test/test_atend_columns_f3.py      test/test_atend_columns_f3_new.py
Copy-Item test/test_canonical_schema_s2.py   test/test_canonical_schema_s2_new.py
Copy-Item test/test_unified_view_s3.py       test/test_unified_view_s3_new.py
Copy-Item test/test_limite_execute_f7.py     test/test_limite_execute_f7_new.py
Add-Content .gitignore "`n*_new.py"
```

Primeiro checkpoint (tem que refletir **exatamente** o original):

```powershell
python -u dev_tools/check_new_state.py    # mesmas linhas/metodos/hash do original
```

Copiar esse relatório para `_backups/simplificacao_cache_<ts>/wip_inicial.txt`.

**Saída**: backup completo + `baseline.txt` + `SHA256SUMS.txt` + `_new` criados.

**Restauração**: `Copy-Item` de volta (ou `git checkout -- <arquivo>`) e
`RESTORE` do export do banco principal.

### F1 — Remover o que já está morto (0,5 dia, risco baixo)

* Apagar: `clear_cache`, `_indexed_parquet_files`, `indexed_parquet_records`,
  `_file_periods`, `_ensure_period_columns_in_table`.
* Apagar estado morto: `_unified_view_enabled` (+ comentário de
  `DATASUS_UNIFIED_VIEW`), `_last_question_*`.
* Apagar wrappers: `_extract_period_from_path` (cache e group),
  `_grouped_cache_path`, `_all_cached_parquets` (usar `cached_files`).
* Unificar `summary_tables`/`_summary_tables` (manter o público).

**Ganho**: −10 métodos, ~105 linhas. **Testes**: suíte completa.
**Risco**: um teste pode mockar `_summary_tables`/`_indexed_parquet_records`;
ajustar `test/test_escopo_llm.py`, `test/test_lexical_rank.py`,
`test/test_schema_comments_prompt.py`.

### F2 — Um único `ATTACH` e uma única sonda de relação (1 dia, risco médio)

* `_GroupCache.attach()` passa a ser o **único** código de `ATTACH`
  (RW → RO → `IF NOT EXISTS`, diagnóstico de lock).
* `DatasusCache.__init__` chama `grupo.attach()` no lugar do loop inline (57
  linhas → 3); `_ensure_groups_attached` some.
* `_GroupCache.exists()` substitui os 4 probes; `cache.py` deixa de sondar
  relação (os dois probes legados saem na F5).
* `_get_canonical_schema` → `canonical_schema()` com 1 `duckdb_columns()`.
* Marcar `self._groups_attached = True` após o primeiro `attach_all()` para
  não consultar catálogo a cada pergunta.

**Testes**: `test_attach_isolado.py`, `test_cache_as_view_s1.py`,
`test_canonical_schema_s2.py`, `test_atend_columns_f3.py`.
**Risco**: um grupo bloqueado por outro processo passa a falhar no `boot`
(intencional — hoje é mascarado por 3 fallbacks distintos).

### F3 — Catálogo e API de grupos (1,5 dia, risco médio)

* DDL de `group_registry`, `group_periods`, `ingest_log` (Anexo B).
* `sync_groups()`: reconcilia `registry ⟷ disco` incluindo
  `cache_dir/<grupo>` **e** `data_dir/<grupo>` → corrige `ab`/`abo`.
* `register_group(name, source_dir=None)` público; `_create_group_pa.py`
  reescrito para usá-lo (some o acesso a `cache.groups[...]`).
* `group(name)` único acesso; `self.groups` como `MappingProxyType`.
* `groups_needing_ingest` → 1 `SELECT`; `periods_for_question` →
  `group_periods`; `status()` → `rows_est` do registry.
* `_GroupCache.finalize()` grava `rows_est`, `files`, `schema_version` e os
  `periods` de volta no catálogo.

**Testes**: os 8 arquivos que chamam `_discover_groups`/`_open_group_if_needed`
(`_create_group_pa.py`, `test_atend_columns_f3.py`, `test_cache_as_view_s1.py`,
`test_canonical_schema_s2.py`, `test_limite_execute_f7.py`,
`test_unified_view_s3.py`, `dev_tools/rebuild_group_cache.py`, `api/routes.py`).
**Risco**: ordem de criação de tabelas no banco principal; usar
`CREATE TABLE IF NOT EXISTS` e migração idempotente.

### F4 — Extrair módulos focados (1,5 dia, risco baixo)

| Módulo novo | Conteúdo | Linhas |
|---|---|---|
| `db/period_parser.py` | `parse_periods`, `wants_monthly_grouping` | ~160 |
| `db/sql_repair.py` | `fix_unterminated_strings`, `fix_incomplete_sql` | ~115 |
| `db/summary.py` | `build_summary_tables(conn, files)`, `summary_tables(conn)` | ~215 |
| `db/cid_prefixes.py` | `load_cid10`, `populate`, `read` | ~150 |
| `db/unified_view.py` | `build_union_sql(...)`, `referenced_columns(...)` | ~200 |

`cache.py` mantém métodos-fachada de 1–3 linhas apenas onde a API é usada por
`api/`/testes (`summary_tables`, `get_cid_prefixes`, `refresh_column_dictionary`).
**Testes**: suíte completa (nada muda de comportamento).

### F5 — Remover o modo legado (1 dia, risco médio)

* Apagar os 20 métodos listados no mapa de §6.1 (456 linhas) e todos os ramos
  `if not self.groups:`.
* `api/routes.py`: `lifespan` passa a chamar só `cache.ensure_ready_sync()`
  (sai o bloco `if not cache.groups` das linhas 56-64 e 79-83).
* `api/llm_service_parts/base.py::_determine_relevant_grupos`: usar
  `cache.groups` (sem varrer Parquet).
* `api/llm_service_parts/ask.py`: trocar os dois fallbacks
  `_indexed_parquet_records()` por `cache.periods_for_question(question)` /
  `cache.groups`.
* Limpeza de dados: `DROP VIEW main.datasus` e `main.datasus_cache`;
  `DROP TABLE main.parquet_period_index`; `DROP TABLE main.cache_checkpoint`;
  **rebuild de `bi`** (formato v0).

#### F5.1 — Remoção do S1 (`DATASUS_CACHE_AS_VIEW`) — obrigatória

O cache passa a ter **um único modo**: tabela física no DuckDB.

| Item | Ação |
|---|---|
| `_GroupCache._cache_as_view_enabled` | apagar |
| `_GroupCache._ingest_as_view` | apagar |
| `_GroupCache._materialize_as_view` | apagar |
| `_GroupCache._view_period_native_expr` | apagar |
| `_GroupCache._VIEW_PERIOD_EXPR` (atributo) | apagar |
| `ingest()` — ramo `if self._cache_as_view_enabled():` | apagar |
| `_GroupCache._cache_view_exists` | apagar (sem S1, `datasus_cache` é sempre TABELA) |
| `_verify_period_integrity` | remover o ramo `if self._cache_view_exists() and not self._cache_table_exists(): return` (não existe mais view) |
| `exists(relation, kind)` | `kind` aceita apenas `"table"`/`"any"`; `datasus_cache` nunca é VIEW |
| `test/test_cache_as_view_s1.py` | apagar (6 testes) |
| `.env` | apagar a linha `DATASUS_CACHE_AS_VIEW=false` |
| Comentários/docstrings | remover todas as menções ao S1 |

Se algum `<grupo>.duckdb` estiver com `datasus_cache` como VIEW (herança de
teste/experimento), o `ensure()` faz a migração única:
`DROP VIEW IF EXISTS <grupo>.datasus_cache` + `CREATE TABLE ... AS SELECT` a
partir dos Parquet (caminho de rebuild já existente em
`dev_tools/rebuild_group_cache.py`). **Nunca** recriar a view.

#### F5.2 — `read_parquet` só na ingestão

| Local | Hoje | Depois |
|---|---|---|
| `_GroupCache.import_batch` | `CREATE VIEW temp_batch AS SELECT * FROM read_parquet(...)` | `INSERT INTO <grupo>.datasus_cache SELECT ... FROM read_parquet([...], union_by_name=true)` direto |
| `_build_summary_tables_locked` | `CREATE TEMP VIEW summary_source AS SELECT * FROM read_parquet(...)` (1 por arquivo) | `INSERT INTO resumo_diagnosticos SELECT ... FROM <grupo>.datasus_cache` (lê a **tabela**, não o Parquet) |
| `datasus_query_files` (legado) | view sobre `read_parquet` | removido com o modo legado (§6.1) |
| validação no ingest | `SELECT * FROM read_parquet(?) LIMIT 0` | mantido (valida o arquivo copiado; não é fonte de consulta) |

Efeitos: `_drop_summary_source` deixa de existir (não há view temporária) e
`_cached_parquet_files` também (os resumos leem as tabelas).

> `bi` (schema v0, sem `atend_ano`) precisa de rebuild **antes** de
> `build_summary_tables()` passar a ler as tabelas.

#### F5.3 — Ordem de execução da fase

| Etapa | O que faz | Pode rodar isolada? |
|---|---|---|
| F5.1 | remover S1 do código | sim (após F4.6) |
| F5.2 | `read_parquet` só na ingestão + resumos das tabelas | depende de F5.1 e F4.3 |
| F5.3 | remover modo legado (código + api + testes) | depende de F5.1 |
| F5.4 | checkpoint estático (**sem testes**) | depende de F5.1–F5.3 |
| F5.5 | limpeza do banco principal | depende de F5.4 + export F0.2 |
| F5.6 | rebuild de `bi` (longo) | depende de F5.4; roda fora do boot |

Cada etapa tem checkpoint próprio (§7.0.2) e entra no `progresso.json` (§7.2).

Ajuste dos testes que ainda citam símbolos removidos (feito **sem executá-los**):

```powershell
Select-String -Path test\*.py -Pattern "_cache_table_exists|_indexed_parquet_records|question_scope|_ingest_locked|_cache_as_view_enabled|_materialize_as_view|_ingest_as_view"
```

Cada ocorrência é removida/reescrita na etapa correspondente; a **execução**
desses testes fica para o F7.4.

#### F5.5 — Limpeza do banco principal + backfill do catálogo

```powershell
python -u dev_tools/migrate_group_catalog.py --limpar-legado --backfill
```

Executa o bloco 7 do §8 (`DROP VIEW datasus/datasus_cache`,
`DROP TABLE parquet_period_index/cache_checkpoint`) e **popula o catálogo**
(`files`, `rows_est`, `group_periods`, `status='ok'`) para todos os grupos.
Idempotente; se falhar no meio, basta reexecutar. **Só depois** de o export da
F0.2 estar verificado.

> **Obrigatório antes do switch (F7.4).** Com `groups_needing_ingest()` lendo o
> catálogo, um grupo com `status='unknown'` é considerado pendente. Sem o
> backfill, o primeiro boot após o switch chamaria `ingest()`/`finalize()` para
> os 14 grupos dentro do `lifespan` — incluindo o `SELECT DISTINCT
> atend_ano/atend_mes` sobre bilhões de linhas. O backfill roda **com a
> aplicação parada**, uma vez, e deixa todos com `status='ok'`.

#### F5.6 — Rebuild de `bi` (longo)

```powershell
python -u dev_tools/rebuild_group_cache.py --only bi --resume
```

Roda **fora** do boot, com log e timeout (§7.4). Retomada via
`group_checkpoint`; o `group_registry.status` fica `legacy` até concluir.

**Testes**: os que exercitam o modo legado (`test_integracao_completa.py`,
`test_unified_view_s3.py::test_flag_ligada`) precisam ser reescritos para o
modo grupos.

### F6 — Consolidação final (0,5 dia)

* Renomear `_scalar`→`scalar`, `reopen`→`ensure` (aliases temporários
  `reopen = ensure` por 1 release).
* Atualizar `README.md` (seção de arquitetura do cache) e este plano (tabela
  de antes/depois).
* Medir novamente (F0) e anexar o resultado ao final deste plano.

### F7 — Documentação de todos os métodos (obrigatória, 0,5–1 dia)

> Diretriz vinculante: **todo método** (público e privado, incluindo
> `@property`, `@staticmethod` e `@classmethod`) tem docstring dizendo o que
> faz/serve, o que recebe e o que devolve. Comentários e docstrings em UTF-8,
> **sem nenhuma acentuação**.

Regras:

1. 1ª linha imperativa e curta: `"""Garante que o grupo esteja pronto para uso."""`;
2. `Args:` para cada parâmetro relevante;
3. `Returns:`/`Raises:` quando houver;
4. efeitos colaterais explícitos (abre conexão, grava em disco, muda estado);
5. comentários internos explicam **por que** (não o que), também sem acentos.

Template:

```python
def ensure(self) -> "_GroupCache":
    """Garante que o grupo esteja pronto para uso.

    Executa de forma idempotente: ATTACH de <grupo>.duckdb na conexao
    principal, criacao das tabelas internas, migracoes de schema e abertura
    da conexao.

    Returns:
        _GroupCache: a propria instancia (permite encadeamento).

    Raises:
        RuntimeError: quando o ATTACH falha no modo isolado.
    """
    # idempotente: chamadas repetidas nao refazem o ATTACH
    ...
```

Escopo: `db/cache.py`, `db/group_cache.py`, os 5 módulos novos
(`db/period_parser.py`, `db/sql_repair.py`, `db/summary.py`,
`db/cid_prefixes.py`, `db/unified_view.py`), `api/routes.py` e os helpers
tocados.

Verificação (critério de pronto). Criar `dev_tools/check_comment_style.py`
(~35 linhas) e rodar:

```python
"""Falha se comentario/docstring tiver acento ou se o arquivo nao for UTF-8."""
import ast, io, sys, tokenize
from pathlib import Path

ACENTOS = set("áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ")
ALVOS = [*Path("db").glob("*.py"), Path("api/routes.py")]
problemas: list[str] = []
for path in ALVOS:
    src = path.read_text(encoding="utf-8")          # falha se nao for UTF-8
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and any(c in ACENTOS for c in tok.string):
            problemas.append(f"{path}:{tok.start[0]} comentario: {tok.string}")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False) or ""
            if any(c in ACENTOS for c in doc):
                problemas.append(f"{path}:{node.lineno} docstring")
print("\n".join(problemas) or "OK: comentarios/docstrings sem acentos")
sys.exit(1 if problemas else 0)
```

```powershell
python -u dev_tools/check_comment_style.py      # deve imprimir "OK"
python -u _inventario_metodos.py                # 0 [FALTA]
git diff --check                                # sem espacos/CRLF invalidos
```

> Mensagens de log e textos de UI **permanecem** com acentuação normal — a regra
> vale para comentarios e docstrings, por isso o script usa `tokenize`/`ast`
> (e nao um grep no arquivo inteiro).

Observação: `db/group_cache.py` hoje tem **BOM** e mojibake (`conexÃ£o`).
Normalizar para UTF-8 sem BOM e corrigir o texto (sem acentos) nos comentários
e docstrings. Mensagens de log e de UI permanecem com acentuação normal.

#### F7.4 — Switch final (uma vez, com rollback)

Sequência (leva segundos no meio; o rollback está em `_backups/switch/<ts>/`):

1. smoke final: `py_compile` + `python -c "import db.cache_new, db.group_cache_new"`
   (**sem pytest**);
2. trocar os imports nos `_new` (`db.group_cache_new` → `db.group_cache`,
   `db.cache_new` → `db.cache`, ...) — poucas linhas;
3. `Move-Item` **nesta ordem**: `group_cache_new` → `group_cache.py`, depois
   `cache_new` → `cache.py`, depois os demais (api/testes/scripts);
4. **única execução de testes** (rápida):
   `python -m pytest -q -m "not requires_llm and not requires_data and not slow"`;
5. `git tag refactor-cache-$(Get-Date -Format 'yyyyMMdd')`;
6. (opcional, pós-entrega) suíte completa: `python -m pytest -q`.

Se o passo 4 falhar: desfazer na ordem inversa a partir de
`_backups/switch/<ts>/` (1 comando por arquivo) e voltar ao `_new`.


---

## 8. Migração de dados (DDL — Anexo B)

Executar em `dev_tools/migrate_group_catalog.py` (novo), idempotente, com a
aplicação parada.

```sql
-- 1) Catálogo de grupos (metadados; nunca dados)
CREATE TABLE IF NOT EXISTS group_registry (
    name           VARCHAR PRIMARY KEY,
    db_path        VARCHAR,
    source_dir     VARCHAR,
    period_column  VARCHAR,
    schema_version INTEGER DEFAULT 2,
    rows_est       BIGINT  DEFAULT 0,
    files          INTEGER DEFAULT 0,
    status         VARCHAR DEFAULT 'unknown',   -- ok | pending | legacy | error
    updated_at     TIMESTAMP
);

-- 2) Períodos disponíveis por grupo (alimenta periods_for_question)
CREATE TABLE IF NOT EXISTS group_periods (
    grupo      VARCHAR,
    atend_ano  INTEGER,
    atend_mes  VARCHAR,
    PRIMARY KEY (grupo, atend_ano, atend_mes)
);

-- 3) Histórico de ingestão (substitui main.cache_checkpoint)
CREATE TABLE IF NOT EXISTS ingest_log (
    id         BIGINT,
    grupo      VARCHAR,
    started_at TIMESTAMP,
    files      INTEGER,
    invalid    INTEGER,
    rows_est   BIGINT,
    status     VARCHAR
);

-- 4) Backfill do catálogo a partir do DISCO (não abre os .duckdb de dados)
INSERT OR REPLACE INTO group_registry (name, db_path, source_dir, status, updated_at)
SELECT stem, CAST(path AS VARCHAR), NULL, 'unknown', now()
FROM glob('<cache_dir>/*.duckdb')
WHERE stem NOT IN ('datasus', 'datasus_fallback', 'parquet', 'cache', 'tmp');

-- 5) Backfill de rows_est/files por grupo (1 conexão READ_ONLY por grupo,
--    apenas catálogo — ver dev_tools/group_catalog_stats.py, novo, ~40 linhas)
--    GROUP BY database_name em duckdb_tables()/duckdb_columns().

-- 6) Períodos: SELECT DISTINCT atend_ano, atend_mes por grupo (1x, na migração)
--    e depois mantido por _GroupCache.finalize().

-- 7) Limpeza do legado (F5; guardar backup antes)
DROP VIEW  IF EXISTS datasus;          -- main: aponta para /mnt/d/...
DROP VIEW  IF EXISTS datasus_cache;    -- main: idem
DROP TABLE IF EXISTS parquet_period_index;
DROP TABLE IF EXISTS cache_checkpoint;
DELETE FROM file_manifest;             -- 0 linhas hoje, mantém a tabela p/ compat
```

**Rollback**: `DROP TABLE group_registry/group_periods/ingest_log` — nenhum
dado de grupo é tocado. As views legadas podem ser recriadas a partir de
`_backups/` se necessário.


---

## 9. Critérios de aceite

| Critério | Como verificar |
|---|---|
| Métodos ≤ 53 | script AST (Anexo C) |
| `db/cache.py` ≤ 1.400 linhas | `(Get-Content db/cache.py).Count` |
| 1 `ATTACH` no código | `Select-String -Path db\*.py -Pattern "ATTACH "` → 1 ocorrência |
| Nenhum `COUNT(*) FROM datasus_cache` em `/api/status` | revisão + latência |
| `/api/status` < 300 ms | `Measure-Command { Invoke-RestMethod http://localhost:8000/api/status }` |
| `/api/ask` com período ≤ baseline (F0) | 5 perguntas de referência |
| `ab`/`abo` no registry | `SELECT name FROM group_registry` → 16 nomes |
| `bi` migrado (schema v0 → v2) | `SELECT status FROM group_registry WHERE name='bi'` → `ok` |
| **Testes rápidos verdes — única execução** | `python -m pytest -q -m "not requires_llm and not requires_data and not slow"` no **F7.4** |
| **Nenhum teste durante as etapas** | F0–F7.3 usam só `py_compile`/`import`/`check_new_state.py` |
| **S1 removido** | `Select-String -r -Path . -Pattern "DATASUS_CACHE_AS_VIEW\|_cache_as_view_enabled\|_ingest_as_view\|_materialize_as_view"` → **vazio** (fora de `_backups/`) |
| **Nenhuma view sobre Parquet** | `Select-String -Path db\*.py -Pattern "CREATE .*VIEW .*read_parquet\|read_parquet.*AS"` → **vazio** |
| `read_parquet` só na ingestão | `Select-String -Path db\*.py -Pattern "read_parquet"` → apenas validação/carga em `import_batch` |
| **100% dos métodos com docstring** | `python -u _inventario_metodos.py` → 0 `[FALTA]` nos arquivos do escopo |
| **0 acentos em comentários/docstrings** | `python -u dev_tools/check_comment_style.py` → `OK` (só comentários/docstrings; logs e UI mantêm acentos) |
| UTF-8 sem BOM | `read_bytes()[:3] != b'\xef\xbb\xbf'` em todos os arquivos alterados |
| Backup da F0 presente e íntegro | `Get-FileHash` confere com `SHA256SUMS.txt` |
| **Originais intactos até o switch** | `git diff --stat` dos originais → vazio durante F1–F7.3 |
| **Checkpoint por ajuste** | `_backups/wip/<ts>/` contém `X_new.py` + `estado.txt` (linhas, métodos, docstrings, acentos, hash) |
| **`_new` fora do git e sem import** | `git status` não lista `*_new.py`; `Select-String -r -Path api,db -Pattern "import .*_new"` → vazio |
| Switch validado | testes rápidos verdes após o `Move-Item` (F7.4) |
| **Etapas separadas e retomáveis** | `progresso.json` existe; `python -u dev_tools/refactor_status.py` aponta a próxima etapa; cada etapa tem `_backups/wip/<ts>/` |
| **Nenhuma etapa > 2 h** | conferir a tabela §7.1 (única exceção: F5.6, isolada, com `--resume` e timeout) |
| **Retomada exercitada** | interromper uma etapa de propósito, rodar `refactor_status.py` e continuar da próxima pendente |
| Sem referências ao legado | `Select-String -r -Path api,db,core,test -Pattern "_ingest_locked\|_cache_table_exists\|_indexed_parquet_records\|question_scope\|clear_cache"` → vazio |

### Comandos de validação (Windows/PowerShell)

```powershell
# UNICA execucao de testes (F7.4) - subconjunto rapido
python -m pytest -q -m "not requires_llm and not requires_data and not slow"

# (opcional, pos-entrega) suite completa
python -m pytest -q

Select-String -Path db\*.py -Pattern "ATTACH "
(Get-Content db/cache.py).Count; (Get-Content db/group_cache.py).Count
python -u _inventario_metodos.py     # já existe no repo (docstrings/mojibake)
```

> `_inventario_metodos.py` (existente) pode ser reaproveitado para recontar
> métodos e docstrings ausentes ao final de cada fase.

---

## 10. Riscos e limitações

| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| `ATTACH` único falhar em grupo bloqueado | média | boot em 503 | já é o comportamento desejado; logar `lsof`/`fuser` e permitir `--skip-locked` no `dev_tools` |
| Registry divergir do disco (novo `.duckdb` copiado à mão) | média | grupo invisível | `sync_groups()` roda no boot e reconcilia **as duas direções** (disco → registry e registry → disco) |
| `group_periods` desatualizado após ingestão interrompida | média | recorte vazio | recalcular no `finalize()` e no `ingest()`; validar com `_verify_period_integrity` |
| `bi` (38 GB) reingerir no rebuild | alta | horas | rebuild em `dev_tools/rebuild_group_cache.py --only bi`, fora do boot; marcar `status='legacy'` até concluir |
| Testes que mockam métodos privados (`_summary_tables`, `_indexed_parquet_records`) | alta | falhas em CI | ajustar mocks na mesma fase (F1/F4) |
| Perda de comportamento do modo legado | baixa | nenhum cliente real (sem grupos nunca ocorre na instalação medida) | código fica no histórico do git (tag/commit antes da F5) |
| Grupo legado com `datasus_cache` como VIEW | baixa | rebuild do grupo | `ensure()` detecta (`exists(kind="view")`) e migra para TABELA uma vez; `status='legacy'` até concluir |
| Padronização UTF-8 sem BOM gerar diff grande (mojibake) | média | revisão mais lenta | fazer por arquivo, validar com `py_compile`/`import` (**sem pytest**) e testar só no F7.4; `git diff -w` |
| Docstring sem acentos reduzir legibilidade | baixa | — | diretriz do projeto; texto curto e objetivo |
| WIP do `_new` corromper/perder | baixa | retrabalho | snapshot em `_backups/wip/<ts>/` a cada ajuste + original intacto (rollback de 1 comando) |
| Duas versões vivas (`_new` e original) divergirem | média | confusão/erro | `_new` não é importado por produção, entra no `.gitignore` e o switch é **único, no fim (F7.4)** |
| Fase longa bloquear a entrega | média | atraso | etapas ≤ 45 min (§7.1) + checkpoint; F5.6 isolada, com `--resume` e timeout |
| Não saber onde parou após interrupção | média | retrabalho | `progresso.json` + `dev_tools/refactor_status.py` (§7.2) |
| Erro de integração só aparecer no fim | média | retrabalho | mudanças mecânicas + `py_compile`/`import` em toda etapa; se o F7.4 falhar, rollback de 1 comando e refazer a etapa culpada |
| Sessão/terminal cair no meio | média | retrabalho | nada crítico em memória: `_new` + snapshot em `_backups/wip/` |

### Fora de escopo

* Trocar DuckDB por outro engine.
* Reprocessar/consolidar os Parquet originais (fora do escopo: só metadados mudam).
* Alterar o prompt/schema enviado ao LLM (`api/llm_service_parts/`), exceto os
  dois fallbacks de `ask.py` citados na F5.


---

## Anexo A — esqueleto da API alvo

### A.1 `_GroupCache` (25 métodos)

```python
class _GroupCache:
    """Cache DuckDB de UM grupo DATASUS (schema <grupo> no banco principal)."""

    def __init__(self, name, source_dir, cache_dir, main_connection=None): ...

    # --- ciclo de vida (único caminho de criação) -------------------------
    def ensure(self) -> "_GroupCache":
        """Anexa (se preciso), garante tabelas/migrações e abre a conexão."""

    def attach(self) -> None:
        """ATTACH IF NOT EXISTS RW → RO. Único ATTACH do projeto."""

    def close(self) -> None: ...
    def exists(self, relation="datasus_cache", kind="any") -> bool: ...

    # --- metadados --------------------------------------------------------
    def columns(self) -> list[dict[str, str]]:
        """DESCRIBE <grupo>.datasus_cache (sem metadados de arquivo)."""

    def column_signature(self) -> float: ...
    def periods(self) -> list[tuple[int, str]]:
        """DISTINCT atend_ano/atend_mes (usado no finalize, não por pergunta)."""

    def status(self) -> dict[str, Any]:
        """files, rows_est, cache_relation, schema_version (sem COUNT(*))."""

    # --- ingestão ---------------------------------------------------------
    def discover_files(self) -> list[Path]: ...
    def pending_batch(self, limit=50) -> tuple[list[Path], int, int]: ...
    def import_batch(self, batch: list[Path]) -> dict[str, Any]: ...
    def ingest(self) -> dict[str, Any]: ...
    def finalize(self) -> None:
        """View datasus + comentários + checkpoint + registry/group_periods."""

    def _verify_period_integrity(self) -> None:
        """Confere atend_ano/atend_mes nulos e corrige por backfill."""

    # --- utilitários privados --------------------------------------------
    def _configure_memory(self) -> None: ...
    def _ensure_schema(self) -> None: ...
    def _migrate_period_columns(self) -> None: ...
    def period_projection(self, col: str) -> str | None: ...
    def period_index(self, files: list[Path] | None = None) -> None: ...
    def checkpoint(self, value: int | None = None) -> int: ...
    def scalar(self, sql, params=None, default=None): ...
    def _qualify_sql(self, sql: str) -> str: ...
    def cached_files(self) -> list[Path]: ...
    def _apply_column_comments(self) -> int: ...
```

### A.2 `DatasusCache` (28 métodos)

```python
class DatasusCache:
    # --- ciclo de vida ----------------------------------------------------
    def __init__(self, settings): ...            # abre principal + sync_groups()
    @property
    def database_path(self) -> Path: ...
    def close(self) -> None: ...

    # --- grupos (NOVO núcleo) --------------------------------------------
    def group(self, name: str) -> _GroupCache: ...
    def register_group(self, name: str, source_dir: Path | None = None) -> _GroupCache: ...
    def sync_groups(self) -> list[str]:
        """Reconcilia group_registry ⟷ disco (inclui cache_dir/<grupo>)."""

    def groups_needing_ingest(self) -> list[str]: ...
    def ensure_ready_sync(self, max_rounds=3) -> dict[str, Any]: ...

    # --- schema/colunas ---------------------------------------------------
    def canonical_schema(self) -> dict[str, Any]: ...
    def invalidate_schema(self) -> None: ...
    def columns(self, grupo: str | None = None) -> dict[str, list[dict[str, str]]]: ...
    def column_dictionary(self, refresh: bool = False): ...

    # --- período/consulta -------------------------------------------------
    def periods_for_question(self, question: str) -> list[tuple[str, str, str]]: ...
    def prepare_question_cache(self, question, request_id, sql=None, extra_columns=None): ...
    def schema_context(self, relation="datasus", include_examples=True) -> dict[str, Any]: ...
    def execute(self, sql, request_id, limite=None) -> list[dict[str, Any]]: ...
    def query_kb(self, sql, params=None) -> list[dict[str, Any]]: ...
    def _fetch_scalar(self, query, params=None, default=None): ...

    # --- ingestão/agregados ----------------------------------------------
    def ingest(self, build_summary=False) -> dict[str, Any]: ...
    def build_summary_tables(self) -> dict[str, Any]: ...      # delega db/summary.py
    def summary_tables(self) -> list[str]: ...                 # delega db/summary.py
    def refresh_cid_prefixes(self) -> int: ...                 # delega db/cid_prefixes.py
    def get_cid_prefixes(self, min_len=2) -> list[str]: ...

    # --- estado/telemetria ------------------------------------------------
    def is_ready(self) -> bool: ...
    def startup_status(self) -> str | None: ...
    def mark_ready(self, error=None) -> None: ...
    def status(self) -> dict[str, Any]: ...                    # lê group_registry
    def _configure_memory(self) -> None: ...
```

> `MappingProxyType(self._groups)` é exposto em `groups` para leitura; a
> escrita passa a ser exclusivamente `register_group`.


---

## Anexo C — script de contagem (usar em F0 e F6)

```python
# python -u dev_tools/count_cache_api.py
import ast
from pathlib import Path

for rel, cls in (("db/cache.py", "DatasusCache"), ("db/group_cache.py", "_GroupCache")):
    src = Path(rel).read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    methods = [i for i in node.body if isinstance(i, (ast.FunctionDef, ast.AsyncFunctionDef))]
    body = sum((i.end_lineno or i.lineno) - i.lineno + 1 for i in methods)
    print(f"{rel}: {len(src.splitlines())} linhas | {len(methods)} métodos | corpo={body}")
```

Saída atual (baseline): `db/cache.py: 2848 linhas | 63 métodos`,
`db/group_cache.py: 1487 linhas | 35 métodos`.

---

## Anexo D — achados que devem ser corrigidos de imediato (independentes do refactor)

1. **`ab`/`abo` invisíveis** (`cache.py:952`): trocar `if not group_names and
   base.is_dir():` por uma união das duas fontes. Sem isso, dois grupos ficam
   fora de qualquer consulta mesmo com Parquet em disco.
2. **`bi` em schema v0** (sem `file_manifest`): precisa de rebuild antes de
   entrar no UNION (hoje entra e força `SELECT DISTINCT` sobre 929 M linhas).
3. **Views legadas no banco principal** apontando para `/mnt/d/...`: além de
   inúteis, fazem `_cache_table_exists()`/`_datasus_view_exists()` responderem
   de forma ambígua no `lifespan`.
4. **`status()` com `COUNT(*)`**: `pa` (1,5 B) + `bi` (929 M) tornam
   `/api/status` inviável sob carga.
5. **`select_groups_periods_for_question` por pergunta**: 14 varreduras de
   bilhões de linhas; usar `group_periods`.
6. **`_unified_view_enabled`**: variável morta que sugere um modo que não
   existe mais (a view é sempre criada em `cache.py:1786`).

---

## Anexo E — o que é `DATASUS_CACHE_AS_VIEW` (modo S1)

**Não é um método**: é uma variável de ambiente lida por
`_GroupCache._cache_as_view_enabled()` (`group_cache.py:706-718`), um
`@staticmethod` que faz `os.getenv("DATASUS_CACHE_AS_VIEW", "false")` a cada
chamada. Valores aceitos: `1/true/yes/on`. Default `false` (também é `false`
no `.env` atual).

> **Status neste plano: REMOVIDA (F5).** Decisão fechada: o cache de grupo é
> sempre uma **TABELA física no DuckDB**; não existe mais modo alternativo nem
> view sobre Parquet. Esta seção fica como registro histórico do que era e do
> porquê da remoção.

### O que ela muda

| | `false` (default) | `true` (S1) |
|---|---|---|
| `datasus_cache` | **TABELA física** no `<grupo>.duckdb` | **VIEW** sobre os Parquet |
| Dados | copiados para dentro do `.duckdb` | lidos de `cache_dir/<grupo>/**/*.parquet` |
| `ingest` | `INSERT` de todas as linhas | só copia Parquet que falta + recria a view |
| Disco | 2 cópias (Parquet **+** tabela) | 1 cópia (só Parquet) |
| `atend_ano`/`atend_mes` | colunas físicas (push-down) | derivadas na view (campo nativo) |
| `parquet_year`/`parquet_month` | colunas físicas | derivadas do nome do arquivo (regex) |
| Comentários de coluna | aplicados na tabela | ignorados (não há tabela) |
| Backfill/`UPDATE` | possível | não aplicável (`_verify_period_integrity` sai cedo) |

### Fluxo quando `true` (histórico — removido na F5)

1. `ingest()` (`group_cache.py:886`) desvia para `_ingest_as_view()` (801);
2. `_ingest_as_view()` copia para `cache_dir/<grupo>/<ano>/<mes>/` apenas os
   Parquet novos/alterados (`_retry_copyfile` + `file_manifest`), sem inserir
   linhas no DuckDB;
3. `_materialize_as_view()` (759) faz `DROP VIEW/TABLE` e
   `CREATE VIEW <grupo>.datasus_cache AS SELECT *, filename AS parquet_path,
   <parquet_year/month do nome> <atend_ano/mes do campo nativo>
   FROM read_parquet([...], union_by_name=true, filename=true)`;
4. cria a view de compatibilidade `<grupo>.datasus` e reindexa o período;
5. o resto do pipeline **não muda**: `prepare_question_cache` seleciona
   `<grupo>.datasus_cache` (tabela ou view) e monta o mesmo `UNION ALL`.

### Para que serve (motivação da FASE 2 / S1)

* **Elimina a duplicação** Parquet + tabela (`PLANO_PERFORMANCE_CACHE.md`: ~40 GB
  por grupo; aqui são ~180 GiB de `.duckdb` que deixariam de existir);
* **ingestão deixa de ser O(linhas)**: não há `INSERT` de bilhões de linhas, nem
  growth de WAL/checkpoint, nem risco de OOM;
* **uma única fonte de verdade** (os Parquet), o que também elimina a classe de
  bug do cache duplicado (`PLANO_REPARO_CACHE_DUPLICADO.md`).

### Por que a remoção foi decidida

1. **Boot quebra com 503**: `groups_needing_ingest()` (`cache.py:1016`) testa
   `group._cache_table_exists()`, que é `False` em S1 → todos os grupos entram
   como pendentes, `ensure_ready_sync` reingere, continua pendente e levanta
   `RuntimeError` após `max_rounds`. Já registrado em
   `PLANO_REPARO_CACHE_DUPLICADO.md:289-290`.
2. **Custo por consulta**: cada pergunta decodifica Parquet (sem estatísticas de
   tabela, sem `atend_ano` físico, sem índice); para `pa` (79 GB / 1,5 B linhas)
   isso foi um dos motivos para nao adotar o modo view.
3. **Sem manutenção de dados**: não dá para aplicar comentários de coluna nem
   corrigir períodos por `UPDATE`.
4. **Não coexiste por grupo**: ligar a flag exige recriar `datasus_cache` do
   grupo (a `_materialize_as_view` derruba tabela *ou* view).

### Decisão (F5) — remoção obrigatória

**O que sai do código** (4 métodos + 1 atributo + 1 teste + 1 variável de env):

| Item | Arquivo |
|---|---|
| `_cache_as_view_enabled` | `db/group_cache.py` |
| `_ingest_as_view` | `db/group_cache.py` |
| `_materialize_as_view` | `db/group_cache.py` |
| `_view_period_native_expr` | `db/group_cache.py` |
| `_VIEW_PERIOD_EXPR` + `_cache_view_exists` | `db/group_cache.py` |
| ramo `if self._cache_as_view_enabled():` em `ingest()` | `db/group_cache.py` |
| `test/test_cache_as_view_s1.py` (6 testes) | `test/` |
| `DATASUS_CACHE_AS_VIEW=false` | `.env` |

**O que fica no lugar**:

* `ingest()` **sempre** materializa a tabela (`CREATE TABLE` + `INSERT`);
* `exists()` consulta apenas TABELAS para `datasus_cache`
  (`duckdb_tables()`), sem o ramo de VIEW;
* views permitidas: `<grupo>.datasus` (sobre a tabela, compatibilidade) e
  `datasus_query_cache` (TEMP VIEW com `UNION ALL` das tabelas);
* `read_parquet` só na ingestão (validar/carregar arquivo), nunca como fonte de
  consulta (ver §4.0 e F5.2);
* se um `<grupo>.duckdb` legado tiver `datasus_cache` como VIEW, o `ensure()`
  migra para tabela uma única vez (rebuild a partir dos Parquet) e segue.

> Não confundir com `DATASUS_UNIFIED_VIEW`, que é outra flag (da view
> unificada `datasus_query_cache`) e está **morta**: a view é sempre criada
> (`cache.py:1786`), independentemente do valor da variável. A view
> `datasus_query_cache` **permanece** — ela é um `UNION ALL` de TABELAS
> DuckDB, não uma view sobre Parquet.

---

## Anexo F — `dev_tools/check_new_state.py` (checkpoint do `_new`)

Script usado em §7.0.2. É um arquivo genuinamente novo (não substitui nada), por
isso usa o nome definitivo. Comentários e docstrings em ASCII; a única linha com
acentos é o conjunto `ACENTOS`, que precisa deles por definição.

```python
"""Relatorio de estado dos arquivos *_new.py (checkpoint do refactor).

Uso:
    python -u dev_tools/check_new_state.py
    python -u dev_tools/check_new_state.py db/cache_new.py

Saida por arquivo: linhas, metodos, docstrings faltando, acentos em
comentarios/docstrings, BOM, SHA256, mtime e status.
Exit code 1 quando houver erro de sintaxe/encoding.
"""
from __future__ import annotations

import ast
import hashlib
import io
import sys
import tokenize
from datetime import datetime
from pathlib import Path

ACENTOS = set("áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ")
IGNORAR = {"_backups", ".venv", "__pycache__", ".git", ".pytest_cache", ".spill"}


def alvos() -> list[Path]:
    """Retorna os caminhos por argumento ou todos os *_new.py do repo."""
    if len(sys.argv) > 1:
        return [Path(arg) for arg in sys.argv[1:]]
    return sorted(
        p for p in Path(".").rglob("*_new.py")
        if not IGNORAR.intersection(p.parts)
    )


def estado(path: Path) -> dict:
    """Coleta as metricas de um arquivo sem alterar nada."""
    dados = path.read_bytes()
    texto = dados.decode("utf-8-sig")  # BOM e tolerado e reportado em bom=
    arvore = ast.parse(texto)
    funcoes = [
        n for n in ast.walk(arvore)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    sem_doc = sum(1 for f in funcoes if ast.get_docstring(f) is None)
    acentos = 0
    for tok in tokenize.generate_tokens(io.StringIO(texto).readline):
        if tok.type == tokenize.COMMENT:
            acentos += sum(1 for c in tok.string if c in ACENTOS)
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef,
                           ast.AsyncFunctionDef)):
            doc = ast.get_docstring(no, clean=False) or ""
            acentos += sum(1 for c in doc if c in ACENTOS)
    return {
        "linhas": len(texto.splitlines()),
        "metodos": len(funcoes),
        "sem_doc": sem_doc,
        "acentos": acentos,
        "bom": dados[:3] == b"\xef\xbb\xbf",
        "sha256": hashlib.sha256(dados).hexdigest()[:16],
        "mtime": datetime.fromtimestamp(
            path.stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    """Imprime a tabela de estado e devolve 1 se algum arquivo falhar."""
    falhou = False
    for path in alvos():
        if not path.is_file():
            print(f"{path}: AUSENTE")
            falhou = True
            continue
        try:
            info = estado(path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"{path}: FALHA ({type(exc).__name__}: {exc})")
            falhou = True
            continue
        print(
            f"{path} | linhas={info['linhas']} | metodos={info['metodos']} | "
            f"sem_doc={info['sem_doc']} | acentos={info['acentos']} | "
            f"utf8=OK | bom={info['bom']} | sha={info['sha256']} | "
            f"mtime={info['mtime']} | status=OK"
        )
    return 1 if falhou else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Saída esperada (exemplo):

```
db/cache_new.py | linhas=2812 | metodos=63 | sem_doc=0 | acentos=0 | utf8=OK | bom=False | sha=3f9c1a2b4d5e6f70 | mtime=2026-10-18 14:32:07 | status=OK
```

---

## Anexo G — `dev_tools/refactor_status.py` (retomada)

Lê o `progresso.json` (§7.2) e diz exatamente de onde continuar. Somente leitura.

```python
"""Mostra a proxima etapa do refactor e o estado dos arquivos *_new.py.

Uso:
    python -u dev_tools/refactor_status.py
    python -u dev_tools/refactor_status.py _backups/simplificacao_cache_<ts>/progresso.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PADRAO = "_backups/simplificacao_cache_*/progresso.json"
ORDEM = [
    "F0.1", "F0.2", "F0.3", "F0.4",
    "F1.1", "F1.2", "F1.3", "F1.4",
    "F2.1", "F2.2", "F2.3", "F2.4", "F2.5",
    "F3.1", "F3.2", "F3.3", "F3.4", "F3.5", "F3.6",
    "F4.1", "F4.2", "F4.3", "F4.4", "F4.5", "F4.6",
    "F5.1", "F5.2", "F5.3", "F5.4", "F5.5", "F5.6",
    "F6.1", "F7.1", "F7.2", "F7.3", "F7.4",
]


def achar() -> Path | None:
    """Retorna o progresso.json indicado por argumento ou o mais recente."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    candidatos = sorted(Path(".").glob(PADRAO))
    return candidatos[-1] if candidatos else None


def iniciar(path: Path) -> int:
    """Cria o progresso.json com todas as etapas pendentes (idempotente)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"progresso ja existe: {path}")
        return 0
    dados = {
        "versao": 1,
        "iniciado_em": datetime.now().isoformat(timespec="seconds"),
        "ts_backup": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "etapas": {etapa: "pending" for etapa in ORDEM},
        "wip_atual": None,
        "ultimo_erro": None,
    }
    path.write_text(json.dumps(dados, indent=2), encoding="utf-8")
    print(f"criado: {path}")
    return 0


def main() -> int:
    """Imprime o estado e a proxima etapa (nunca altera nada, exceto --init)."""
    if sys.argv[1:2] == ["--init"]:
        return iniciar(Path(sys.argv[2]) if len(sys.argv) > 2
                       else Path("_backups/progresso.json"))
    path = achar()
    if path is None or not path.is_file():
        print("progresso.json: AUSENTE -> comecar pela F0.1 (backup)")
        return 0
    dados = json.loads(path.read_text(encoding="utf-8"))
    etapas = dados.get("etapas", {})
    print(f"progresso: {path}")
    print(f"iniciado_em: {dados.get('iniciado_em')} | wip: {dados.get('wip_atual')}")
    for etapa in ORDEM:
        if etapas.get(etapa) != "done":
            print(f"  {etapa}: {etapas.get(etapa, 'pending')}")
    pendentes = [e for e in ORDEM if etapas.get(e) != "done"]
    if pendentes:
        print(f"\nproxima etapa: {pendentes[0]}")
        print("passo: PLANO_SIMPLIFICACAO_CACHE.md §7.1 + checkpoint §7.0.2")
    else:
        print("\ntodas as etapas concluidas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

---

## Conclusão

O `cache.py` acumulou três gerações de arquitetura no mesmo arquivo:

1. **G1 — tabela única** (`main.datasus_cache`, `_ingest_locked`) — hoje morta,
   mas com 456 linhas e 20 métodos ainda presentes;
2. **G2 — schema por grupo** (`_GroupCache`, `UNION ALL`) — arquitetura atual,
   correta, mas com a criação/acesso espalhados em 5 etapas e 4 portas;
3. **G3 — experimentos** (`DATASUS_UNIFIED_VIEW`, `DATASUS_CACHE_AS_VIEW`) —
   flags desligadas por padrão, uma delas já morta.

O refactor proposto **não muda a arquitetura de dados** (schema por grupo é a
certa para 3,3 B de linhas e ~180 GiB): ele apaga G1, promove G2 para uma API de
duas chamadas (`sync_groups()` + `group(name)`) e **remove G3** (`DATASUS_CACHE_AS_VIEW`
e a flag morta `DATASUS_UNIFIED_VIEW`).
O resultado é ~46% menos métodos, ~715 linhas removidas, 1 catálogo em vez de
duas varreduras de disco, **cache sempre em tabela DuckDB** (nenhuma view sobre
Parquet) e nenhuma consulta de bilhões de linhas em `/api/status` ou na
resolução de período.


---

## Registro de execução pós-F7.4 (2026-10-23)

Validação final no cache real (`D:\datasus\cache`, 16 grupos) e correções
aplicadas depois do switch. Nenhuma etapa nova em `progresso.json`: são ajustes
de robustez que fecham as pendências abertas na entrega.

### 1. `ab` e `abo` (grupos sem `.duckdb`)

Os dois grupos estavam no catálogo como `status='unknown'` (só Parquet, sem
banco). Foram ingeridos fora do boot com `ensure_ready_sync`:

| Grupo | Arquivos | Linhas | Períodos | Status |
|---|---|---|---|---|
| `ab` | 91 | 19.781 | 12 | `ok` |
| `abo` | 905 | 211.685 | 82 | `ok` |

O `abo` foi retomado pelo `group_checkpoint` após uma interrupção (550/905) e
concluiu os 905 arquivos na execução seguinte.

### 2. Falha fatal de GIL na ingestão (duckdb-python)

Na primeira execução do `abo`, o processo morreu com:

```
Fatal Python error: PyEval_SaveThread: the function must be called with the
GIL held, but the GIL is released (the current Python thread state is NULL)
```

logo após o 11º lote (`pending_batch → discover_files → pathlib.rglob`). É o
mesmo sintoma do bug `duckdb-python#456` (destrutor de resultado liberando o GIL),
cuja correção (`#462`) entrou no **1.5.3** — a máquina já usa **1.5.5**, então é
uma manifestação residual. Evidência: `_backups/simplificacao_cache_20261018_170419/ingest_pending_20261023.err`.

Mitigações aplicadas (não alteram o resultado da ingestão):

* `_GroupCache._exec()` — todo DDL/DML do caminho de ingestão faz `fetchall()`
  imediato, encerrando o resultado no ponto em que o GIL está garantido;
* manifesto gravado em **uma** instrução `INSERT ... SELECT FROM (VALUES ...)`
  por lote (antes: 1 `INSERT` parametrizado por arquivo);
* `_save_checkpoint()` e `_refresh_period_index()` em **um** comando cada
  (antes: 3 e `executemany`);
* validação do Parquet por literal escapado (sem binding de parâmetros).

Risco residual: se voltar a ocorrer, rodar a ingestão com `DATASUS_THREADS=1`
e/ou reconstruir o grupo com `dev_tools/rebuild_group_cache.py <grupo>`
(a retomada usa o `group_checkpoint`).

### 3. Migração `atend_ano/atend_mes` em tabela gigante

`reopen()` chama `_migrate_period_columns()`. Em um `finalize` **sem novidades**
(rodada de `ensure_ready_sync`), o `bi` (929.218.917 linhas) ganhou as colunas
`atend_ano`/`atend_mes` e iniciou o backfill O(n) — que reescreveria a tabela
inteira dentro do boot. Pior: com as colunas presentes e nulas,
`db/unified_view.py` as usa como colunas físicas e **todo o `bi` sairia de
qualquer consulta com recorte de período** (`WHERE atend_ano IN (...)`).

Correções:

| Item | Correção |
|---|---|
| `db/group_cache.py` | `MIGRATE_MAX_ROWS` (`DATASUS_MIGRATE_MAX_ROWS`, padrão 5.000.000): acima disso a migração **in place** é desativada e o log pede rebuild. Conversão `VARCHAR → INTEGER` continua sendo aplicada quando viável. |
| `dev_tools/rebuild_group_cache.py` | passa a criar `atend_ano`/`atend_mes` (derivados do campo nativo) na própria reconstrução — uma passada, sem migração posterior. |
| `db/group_cache.py` | `finalize(verify_period=True)`: `DatasusCache.ingest()` passa `False` para grupos sem escrita na rodada, evitando `COUNT(*)` de integridade em bilhões de linhas. |
| `db/group_cache.py` | `_publicar_no_catalogo()` publica `files` como o **nº de Parquet na origem** (mesma métrica de `groups_needing_ingest`), em vez de `COUNT(*)` do manifesto — o manifesto do `ad` tinha 4.212 entradas para 2.106 arquivos (entradas antigas de outro `source_dir`), o que faria o grupo parecer completo e ignorar arquivos novos. |
| Dados | o `bi` teve as colunas nulas removidas (`ALTER TABLE ... DROP COLUMN`); `parquet_year/month` intactos (0 NULL) e a view unificada volta a usar o campo nativo de período. O catálogo do `ad` foi republicado com `files=2106`. |

### 4. Validação

* `python -m py_compile db/group_cache.py db/cache.py dev_tools/rebuild_group_cache.py` → OK.
* `python -m pytest -q -m "not requires_llm and not requires_data and not slow"`
  → suíte rápida verde (193 testes, incluindo 2 novos em
  `test/test_atend_columns_f3.py`: bloqueio da migração acima do limite e
  `finalize(verify_period=False)` publicando view + `period_index`).
* Catálogo: 16 grupos, todos `status='ok'`; `group_periods` com os 16 grupos.

