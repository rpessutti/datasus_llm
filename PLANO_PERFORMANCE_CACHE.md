# PLANO — Performance da criação dos caches DuckDB (configuração)

**Status:** PROPOSTO (2026-10-12)
**Escopo:** **configuração do DuckDB** (Etapas 1-4) + **backup/restauração** (Etapa 0) +
**docstrings dos métodos** (Etapa 0.1) + **testes de fechamento** (Etapa 5). Não altera o
algoritmo de ingestão.

---

## Etapa 0 — Backup e recuperação (fazer ANTES de qualquer edição)

> Utilitário de guarda do plano: **`dev_tools/backup_perf_cache_files.py`** (usa `core/safe_io`:
> cópia + `fsync` + SHA256; grava em `_backups/perf_cache_<AAAAMMDD_HHMMSS>/`, que é gitignored).

**Arquivos-alvo (os 4 que este plano altera — nenhum outro é tocado):**

| # | Arquivo | Papel no plano |
|---|---|---|
| 1 | `config/settings.py` | defaults de `threads` / `memory_limit` / `temp_dir` (Etapas 1-2) |
| 2 | `db/group_cache.py` | `_GroupCache._configure_memory` (Etapas 1-4) |
| 3 | `db/cache.py` | `DatasusCache._configure_memory` (Etapas 1-2) |
| 4 | `dev_tools/rebuild_group_cache.py` | config do rebuild + `CHECKPOINT` final (Etapas 1-4) |

**0.1) Criar o backup (comando explícito):**
```powershell
cd d:\workspace\datasus_llm
.venv\Scripts\python.exe dev_tools\backup_perf_cache_files.py
```
Saída (já verificado nesta máquina):
```
[OK] config/settings.py              -> _backups\perf_cache_<stamp>\config__settings.py
[OK] db/group_cache.py               -> _backups\perf_cache_<stamp>\db__group_cache.py
[OK] db/cache.py                     -> _backups\perf_cache_<stamp>\db__cache.py
[OK] dev_tools/rebuild_group_cache.py -> _backups\perf_cache_<stamp>\dev_tools__rebuild_group_cache.py
Backup concluido em: ...\_backups\perf_cache_<stamp>
```
Artefatos: os 4 arquivos + **`SHA256SUMS.txt`** (hash + mapeamento) dentro de `_backups/perf_cache_<stamp>/`.

**0.2) Conferir integridade (quando quiser):**
```powershell
.venv\Scripts\python.exe dev_tools\backup_perf_cache_files.py --verify
```
Esperado: `[OK]` nos 4 arquivos (compara o SHA256 atual com o do backup mais recente).

**0.3) Recuperar em caso de corrupção (restore rápido):**
```powershell
.venv\Scripts\python.exe dev_tools\backup_perf_cache_files.py --restore                 # todos
.venv\Scripts\python.exe dev_tools\backup_perf_cache_files.py --restore db/group_cache.py  # um só
```
O restore usa `core.safe_io.restore_file(..., verify=True)`: escrita **atômica** (`tmp` + `os.replace`
+ `fsync`) e **confere o hash** depois de gravar.

**0.4) Listar backups disponíveis:**
```powershell
.venv\Scripts\python.exe dev_tools\backup_perf_cache_files.py --list
```

**Regra de ouro:** rodar **0.1 antes de cada bloco de edição** — cada iteração passa a ter um
ponto de retorno de 1 comando.

---

## Etapa 0.1 — Documentar (comentar) os métodos dos arquivos alterados

**Comando explícito de auditoria** (AST, somente leitura) — lista classe/método e se possui docstring:
```powershell
.venv\Scripts\python.exe dev_tools\backup_perf_cache_files.py --metodos
```
**Estado atual (medido):** há **1 único item sem docstring** — **`class Settings`** (`config/settings.py:40`).
Todo o resto dos 4 arquivos está `[ok]`.

**O que comentar por símbolo tocado:**

| Arquivo | Símbolo (linha) | Conteúdo mínimo do comentário |
|---|---|---|
| `config/settings.py` | `class Settings` (40) | docstring da classe: "Configurações globais lidas do ambiente; `threads`, `memory_limit` e `temp_dir` governam o DuckDB (Etapas 1-2)." |
| `db/group_cache.py` | `_GroupCache._configure_memory` (429) | atualizar: aplica `temp_directory` (**disco separado**), `threads=16`, `memory_limit=20GB`, **`preserve_insertion_order=false`** e **`checkpoint_threshold='4GB'`** |
| `db/cache.py` | `DatasusCache._configure_memory` (299) | idem para a conexão principal + nota do `max_temp_directory_size` |
| `dev_tools/rebuild_group_cache.py` | `rebuild_group` (54) | atualizar: 16 threads / 20 GB, spill em disco separado, `preserve_insertion_order=false`, `checkpoint_threshold='4GB'` e **`CHECKPOINT` antes do `close()`** |
| `dev_tools/rebuild_group_cache.py` | `_parquet_files` (42), `main` (189) | já documentados — conferir se o texto continua correto após as mudanças |

**Critério de aceite:** `--metodos` retornar **`[metodos] sem docstring: 0`** (hoje: **1**).

---

## Motivação (medido, não estimado)

O rebuild do grupo `bi` — que é o **caminho mais leve** (lê os Parquet direto, sem cópia de
arquivos e sem `DELETE`/`UPDATE` por lote) — levou **3 h 59 min** para:

| item | valor |
|---|---|
| Arquivos de entrada | 1.211 Parquet = **29,31 GB** |
| Linhas | **929.218.917** |
| Saída (`bi.duckdb`) | 37.993 MiB (~39,8 GiB) |
| Duração (`rb_bi.out`) | 19:27:12 → **23:27:05** |
| Taxa média | **~64.500 linhas/s** · **~3,0 MB/s** |
| Benchmark inicial (30 arquivos) | 353.251 linhas/s · **15,26 MB/s** |

Hardware: **32 núcleos**, **31,7 GB RAM**, NVMe (escrita bruta medida: **646 MB/s**).

**Sintoma:** CPU e SSD quase ociosos e rendimento **5,5× menor** que o da amostra inicial.
Causa provável: `threads`/`memory_limit` baixos → **spill constante**, e o `temp_directory`
fica **no mesmo disco** do banco → I/O concorrente consigo mesmo.

---

## Etapa 1 — Threads e memória: `DATASUS_THREADS=16` / `DATASUS_MEMORY_LIMIT=20GB`

**Objetivo:** usar 16 dos 32 núcleos e 20 GB (~63% dos 31,7 GB) para **reduzir o spill**.

**Onde alterar (defaults dos `getenv`):**

| Arquivo | Linha | Antes | Depois |
|---|---|---|---|
| `config/settings.py` | 69 | `os.getenv("DATASUS_MEMORY_LIMIT", "8GB")` | `..., "20GB"` |
| `config/settings.py` | 70 | `int(os.getenv("DATASUS_THREADS", "8"))` | `..., "16"))` |
| `db/group_cache.py` | 439 | `os.getenv("DATASUS_MEMORY_LIMIT", "8GB")` | `..., "20GB"` |
| `db/group_cache.py` | 440 | `int(os.getenv("DATASUS_THREADS", "8"))` | `..., "16"))` |
| `dev_tools/rebuild_group_cache.py` | 90 | `os.getenv("DATASUS_MEMORY_LIMIT", "4GB")` | `..., "20GB"` |
| `dev_tools/rebuild_group_cache.py` | 91 | `os.getenv("DATASUS_THREADS", "4")` | `..., "16")` |

> `db/cache.py:302-303` lê de `settings` (`getattr(self.settings, ...)`) e depois aplica o
> override por env (`:306-307`) — muda junto, sem edição adicional.

**Override sem editar código (para testar já):**
```powershell
$env:DATASUS_THREADS='16'; $env:DATASUS_MEMORY_LIMIT='20GB'
```

> ### ⚠️ Efetivo × default (importante)
> `config/settings.py` chama `load_dotenv(".env")` e o **`.env` do projeto define** estas
> variáveis — logo **os valores do `.env` vencem os defaults do código**. É preciso ajustar
> **os dois** (ou só o `.env`, que é o que vale em runtime):
> ```dotenv
> # .env  (valores EFETIVOS aplicados em runtime)
> DATASUS_MEMORY_LIMIT=20GB
> DATASUS_THREADS=16
> DATASUS_TEMP_DIR=C:\datasus_spill
> ```
> **Validação obrigatória** (depois de editar, não antes):
> ```powershell
> python -c "from config.settings import setting as s; print(s.memory_limit, s.threads, s.temp_dir)"
> # esperado: 20GB 16 C:\datasus_spill
> ```

**Validação:**
```sql
PRAGMA threads;         -- deve retornar 16
PRAGMA memory_limit;    -- deve retornar 20.0 GiB
```

---

## Etapa 2 — Spill em **disco separado**: `DATASUS_TEMP_DIR=C:\datasus_spill`

**Objetivo:** tirar o spill de `D:\datasus\cache\duckdb_spill` (mesmo disco do banco) e pôr
num **disco físico diferente** → o DuckDB não compete consigo mesmo na mesma fila do NVMe.

**Onde alterar:**

| Arquivo | Linha | Antes | Depois |
|---|---|---|---|
| `config/settings.py` | 71-74 | default `str(PROJECT_DIR / ".spill")` | `r"C:\datasus_spill"` (Windows) |
| `db/group_cache.py` | 443, 448 | `temp_dir = self.cache_dir / "duckdb_spill"` | `Path(os.getenv("DATASUS_TEMP_DIR", r"C:\datasus_spill"))` |
| `dev_tools/rebuild_group_cache.py` | 92-95 | `spill = CACHE_DIR / "duckdb_spill"` | `Path(_os.getenv("DATASUS_TEMP_DIR", r"C:\datasus_spill"))` |

**Snippet sugerido — `dev_tools/rebuild_group_cache.py:92-95`:**
```python
spill = Path(_os.getenv("DATASUS_TEMP_DIR", r"C:\datasus_spill"))
spill.mkdir(parents=True, exist_ok=True)
conn.execute(f"SET temp_directory='{spill}'")
```

**Pré-requisitos:**
- `C:` com espaço livre suficiente (o spill de uma tabela de ~1,5 bi de linhas pode passar de
  dezenas de GB). Hoje: `C:` tem folga (muito mais que `D:`).
- Adicionar `C:\datasus_spill` às **exclusões do antivírus** (senão o escudo de arquivos
  estrangula as gravações de spill).

**Validação:**
```sql
PRAGMA temp_directory;              -- deve apontar para C:\datasus_spill
```
E confirmar que **não** foi criado `D:\datasus\cache\duckdb_spill` durante o rebuild.

---

## Etapa 3 — `preserve_insertion_order=false` **também** no rebuild e nos grupos

**Objetivo:** o default é `true`; isso obriga o `INSERT ... SELECT` a **bufferizar/ordenar**
todo o resultado do lote (100 arquivos ≈ 77 M linhas) para preservar a ordem de inserção →
estoura o `memory_limit` → **spill massivo**. Desativar reduz drasticamente o estado em memória.

> Já está aplicado na conexão **principal** (`db/cache.py:319`), mas **não** nas conexões de
> grupo nem no rebuild.

**Onde alterar:**

1. `db/group_cache.py` — em `_configure_memory`, logo após a linha 463 (`PRAGMA threads`):
```python
            try:
                self.connection.execute("PRAGMA preserve_insertion_order=false")
            except Exception:
                pass
```

2. `dev_tools/rebuild_group_cache.py` — logo após a linha 97 (`SET threads`):
```python
        conn.execute("SET preserve_insertion_order=false")
```

**Requisito já atendido:** consultas com `LIMIT` precisam de `ORDER BY` explícito (regra #10 do
prompt e do `README.md`), portanto a ordem de inserção **não** é semanticamente necessária.

**Validação:**
```sql
PRAGMA preserve_insertion_order;    -- deve retornar false
```

---

## Etapa 4 — `checkpoint_threshold='4GB'` + `CHECKPOINT` explícito no fim

**Objetivo:** limitar o crescimento do WAL e evitar o **grande checkpoint single-thread** que
hoje acontece no `conn.close()` (fase final pesada e sem paralelismo).

**Onde alterar:**

| Arquivo | Local | Adição |
|---|---|---|
| `db/group_cache.py` | `_configure_memory` (após linha 463) | `PRAGMA checkpoint_threshold='4GB'` |
| `db/cache.py` | `_configure_memory` (após linha 325) | `SET checkpoint_threshold='4GB'` (opcional) |
| `dev_tools/rebuild_group_cache.py` | após linha 97 | `SET checkpoint_threshold='4GB'` |

**`CHECKPOINT` explícito — `dev_tools/rebuild_group_cache.py`, antes do `conn.close()` (linha 185):**
```python
    conn.execute(f'CHECKPOINT "{group}"')   # flush deterministico do banco do grupo
    conn.close()
    print(f"[{group}] OK")
```

**Snippet sugerido — `db/group_cache.py` (laço do `_configure_memory`, linhas 461-463):**
```python
            self.connection.execute(f"PRAGMA temp_directory='{temp_dir}'")
            self.connection.execute(f"PRAGMA memory_limit='{memory_limit}'")
            self.connection.execute(f"PRAGMA threads={threads}")
            self.connection.execute("PRAGMA preserve_insertion_order=false")  # Etapa 3
            self.connection.execute("PRAGMA checkpoint_threshold='4GB'")      # Etapa 4
```

**Validação:**
- O arquivo `*.duckdb.wal` permanece pequeno durante a execução.
- Ao final, o processo **não** fica minutos parado no `close()`.
- `bi.duckdb` (ou `<grupo>.duckdb`) com tamanho estável após o término.

---

## Etapa 5 — Testes (SOMENTE no final, curtos e relevantes)

> ### ⛔ Ordem explícita
> 1. **NENHUM teste durante a implementação.** Enquanto as Etapas 0.1-4 estiverem sendo aplicadas
>    (edições nos 4 arquivos), **não** rodar pytest nem smoke-checks — nem por arquivo, nem "só o
>    rapidinho".
> 2. **Só no FINAL**, quando as 4 edições estiverem gravadas **e** as docstrings atualizadas
>    (Etapa 0.1 concluída).
> 3. **Só testes curtos e relevantes** — sem suíte completa e sem marcadores pesados
>    (`slow` / `requires_data` / `requires_llm`).

> **Runner:** usar o **Python do sistema** (`python -m pytest ...`) — é ele que tem
> `pytest` **e** `duckdb`. O `.venv\Scripts\python.exe` **não** tem `pytest` instalado
> (verificado: `No module named pytest`).

**Comandos (rodar uma única vez, no fim):**
```powershell
cd d:\workspace\datasus_llm

# (a) curtos e diretamente ligados a este plano:
python -m pytest test/test_safe_io.py test/test_repair_group_cache.py -q

# (b) vizinhos do cache de grupos (colunas/período, attach, view):
python -m pytest test/test_atend_columns_f3.py test/test_cache_as_view_s1.py -q

# (c) opcional: a suíte toda, SEM os lentos/pesados:
python -m pytest test/ -q -m "not slow and not requires_data and not requires_llm"
```

| Arquivo | Por que é relevante |
|---|---|
| `test/test_safe_io.py` | valida o `core/safe_io` usado pela **Etapa 0** (backup/restore/verify) |
| `test/test_repair_group_cache.py` | valida o reparo/validação do cache de grupos |
| `test/test_atend_columns_f3.py` | colunas e derivação de período do cache do grupo |
| `test/test_cache_as_view_s1.py` | caminho de **view** do cache (S1) |

**NÃO rodar aqui:** qualquer `-m slow`, `requires_data` ou `requires_llm`; e **não** executar os
scripts manuais (`test/test_attach_isolado.py`, `test/test_integracao_completa.py`, `test/test_llm_grafic.py`,
`test/test_validacao_fluxo.py`, `test/test_python_d.py`, `test/test_limite.py`) — eles fazem
ingestão/LLM **no import** (ver `PLANO_P4_SUITE_TESTES.md` / `test/conftest.py::collect_ignore`).

**Validação dos PRAGMAs (também só no final):** uma conexão DuckDB deve reportar
`threads=16`, `memory_limit=20.0 GiB`, `temp_directory=C:\datasus_spill`,
`preserve_insertion_order=false` e `checkpoint_threshold='4GB'`.

**Critério de aceite da Etapa 5:** os comandos **(a)** e **(b)** passam **e**
`--metodos` retorna `[metodos] sem docstring: 0`.

---

## Ordem de execução (checklist)

- [ ] **0. Backup:** `python dev_tools\backup_perf_cache_files.py` (Etapa 0.1) — ponto de retorno.
- [ ] **0.1 Métodos:** `python dev_tools\backup_perf_cache_files.py --metodos` → documentar o que
      faltar (hoje: **`class Settings`**) e **atualizar as docstrings** dos métodos tocados
      (tabela da Etapa 0.1). **Aceite: `sem docstring: 0`.**
- [ ] **1. Baseline:** medir o grupo médio (`er`/`acf`/`rj`) cronometrado (comando abaixo).
- [ ] **2. Aplicar as Etapas 1-4** (são complementares). ⛔ **NENHUM teste neste meio** (ver Etapa 5).
- [ ] **3. Depois:** rodar o **mesmo** comando no mesmo grupo e comparar o tempo.
- [ ] **4. Validar** com os `PRAGMA`: `threads`, `memory_limit`, `temp_directory`,
      `preserve_insertion_order`, `checkpoint_threshold`.
- [ ] **5. Conferir** com `--verify`: como os 4 arquivos foram editados, o SHA256 **deve divergir**
      do backup — isso é o **esperado** (divergência aqui = a edição realmente aconteceu).
- [ ] **6. Testes — SÓ AGORA, no final, curtos e relevantes** (Etapa 5, comandos (a) e (b)).
      **Antes deste ponto é proibido rodar testes.**
- [ ] **7. Registrar** o antes/depois (tempo e PRAGMAs) neste arquivo.

**Comando de medição (PowerShell):**
```powershell
cd d:\workspace\datasus_llm
$env:DATASUS_THREADS='16'; $env:DATASUS_MEMORY_LIMIT='20GB'
$env:DATASUS_TEMP_DIR='C:\datasus_spill'
$sw = [Diagnostics.Stopwatch]::StartNew()
.venv\Scripts\python.exe dev_tools\rebuild_group_cache.py er
$sw.Stop(); "duracao_min=$([math]::Round($sw.Elapsed.TotalMinutes,1))"
```

**Baseline atual (medido ANTES deste plano):** `bi` (1.211 Parquet / 929,2 M linhas) =
**3 h 59 min**. Re-executar `bi` depois das mudanças é a comparação mais direta
(requer apagar/criar o banco vazio antes, se quiser medir com a tabela vazia).

---

## Rollback

- **Config só por env:** basta não definir as variáveis (ou remover do ambiente).
- **Mudanças no código:** reverter os 4 pontos acima. Nenhuma delas altera dados; o pior caso
  é voltar ao desempenho anterior.
- `preserve_insertion_order=false` exige que consultas com `LIMIT` usem `ORDER BY` — **regra que
  o projeto já adota** (README #10 / prompt). Se algum SQL legado depender da ordem implícita,
  ele precisa ser corrigido (e já deveria estar).

---

## Fora de escopo (backlog estrutural — não é configuração)

Estas mudanças atacam o **O(n²)** e a **cópia serial** da ingestão da aplicação
(`db/group_cache.py`), e são as que explicam as "horas" no grupo `pa`:

1. **Eliminar os dois `DELETE ... WHERE parquet_path IN (...)` por lote** (`:1292-1302`) —
   varrem a tabela inteira; idempotência via manifest / chave indexada / staging+MERGE.
2. **Eliminar o `UPDATE ... SET parquet_year = regexp_extract(...)` por lote** (`:1311-1318`,
   `:1002-1009`) — varredura + reescrita da tabela inteira.
3. **Derivar `parquet_year/month` 1× por ARQUIVO** (join com uma tabela de-para ≈1,2 mil linhas)
   em vez de `regexp_extract` por linha.
4. **Paralelizar a cópia** dos Parquet (`_retry_copyfile`, `:956`) — ou **não copiar**.
5. **`DATASUS_CACHE_AS_VIEW=true` (S1)** — expor o grupo como **view sobre os Parquet**,
   eliminando a materialização (segundos em vez de horas e ~40 GB/grupo a menos).

