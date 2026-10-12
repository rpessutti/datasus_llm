# PLANO P4 — Suite de testes determinística e reprodutível

## Objetivo

Fazer com que `python -m pytest test/` **complete sem erros de coleta**
(`INTERNALERROR`) e **sem travar** — coletando apenas testes de verdade e
ignorando os scripts manuais que executam código pesado no import.

## Problema original

A pasta `test/` misturava dois tipos de arquivo:

1. **Testes reais** — `test_*.py` que definem `def test_*` ou classe `Test*`
   e não executam lógica no nível de módulo.
2. **Scripts manuais** — arquivos com prefixo `test_` que, na verdade, são
   programas: rodam ingestão no DuckDB, geram arquivos em `/tmp`, chamam o
   LLM/Ollama e/ou terminam com `sys.exit()` **no import**.

Ao rodar `pytest test/`, o pytest importava os scripts, que executavam tudo
isso no momento da coleta → `INTERNALERROR` ou travamento (timeout).

Arquivos identificados como scripts (sem testes coletáveis):

| Arquivo | Natureza |
|---|---|
| `test_attach_isolado.py` | Ingestão DuckDB + ATTACH no import |
| `test_integracao_completa.py` | Fluxo completo no import |
| `test_llm_grafic.py` | Instancia LLM, gera gráfico no import |
| `test_validacao_fluxo.py` | Execução end-to-end no import |
| `test_python_d.py` | Script solto |
| `test_limite.py` | Script com laço no import |
| `manual_duckdb_check.py` / `manual_lifespan_check.py` | Manuais (fora do padrão `test_*`, já ignorados) |

## Solução aplicada

### 1. `pytest.ini` (raiz do projeto)

- `testpaths = test`
- `python_files = test_*.py`, `python_classes = Test*`, `python_functions = test_*`
- `addopts = --strict-markers -p no:cacheprovider`
- Marcadores registrados (evitam *warnings* de marcador desconhecido e permitem
  separar a suíte rápida da lenta):
  - `slow`, `integration`, `requires_data`, `requires_llm`
- `norecursedirs` para `.git`, `.venv`, `build`, etc.

### 2. `test/conftest.py`

- `collect_ignore = [...]` com a lista dos **scripts manuais** (os que executam
  no import). Eles continuam executáveis manualmente:
  `python test/<arquivo>.py`, mas o pytest não os importa mais.
- Critério documentado: entra na lista o módulo que (a) executa lógica no nível
  de módulo e (b) **não** define nenhum `test_*`/`Test*`.

## Correções de testes frágeis (ambiente × determinismo)

### `test_llm_columns_cache.py::test_cache_invalidated_when_source_is_newer`

- **Antes**: o teste lia o `source_mtime` do cache real em disco e comparava
  com o mtime **vivo** da fonte. Isso dependia do estado ambiente (mtime dos
  `.duckdb`/Parquet) → falhava quando a fonte havia avançado desde a gravação.
- **Depois**: valida a **regra** de invalidação com valores controlados
  (`cached >= source` ou `source == 0`) **e** verifica o comportamento real de
  reconstrução com a leitura da fonte **mockada** (determinística), restaurando
  o cache original ao final.

## Resultado

| Antes | Depois |
|---|---|
| `pytest test/` → `INTERNALERROR` / travamento | `200 tests collected` em ~0,35s |
| Suíte verde quebrada por teste frágil | **0 failed** (teste frágil reescrito) |
| Tempo: travava (>2 min) | sem travamento (soma das durações; ver observações) |

> **Nota (revisão do plano de VSS/HNSW):** `test/test_hnsw_experimental_persistence.py`
> (11 testes) foi **removido** junto com `core/duckdb_config.py`, porque o índice HNSW
> experimental foi **descartado** em `PLANO_MELHORIA_PROMPT_CID.md`. Por isso o total caiu de
> **204 → 193** (a coleta continua sem `INTERNALERROR`).

### Distribuição dos 200 testes (por arquivo)

- `test_agrupamento_mes.py` — 7
- `test_atend_columns_f3.py` — 7
- `test_cache_as_view_s1.py` — 9
- `test_canonical_schema_s2.py` — 6
- `test_cid_discovery_full.py` — 30
- `test_cid_explicito.py` — 7
- `test_cid_false_positive.py` — 14
- `test_escopo_llm.py` — 6
- `test_lexical_rank.py` — 19
- `test_limite_execute_f7.py` — 5
- `test_llm_columns_cache.py` — 31
- `test_p2_prompt_cid.py` — 5
- `test_percentual.py` — 5
- `test_qualify_sql_f5.py` — 14
- `test_repair_group_cache.py` — 7 (reparo do cache duplicado, dados fake)
- `test_safe_io.py` — 6
- `test_schema_comments_prompt.py` — 10
- `test_tipos_graficos.py` — 6
- `test_unified_view_s3.py` — 6

## Observações operacionais

- **Locks do DuckDB**: matar um `pytest` no meio pode deixar um processo Python
  segurando `*.duckdb` (`IOException: ... já está sendo usado por outro
  processo`). Verifique com `Get-Process python` e encerre o processo zumbi
  antes de nova execução.
- **Dados reais tornam a suíte LENTA (a coleta não):** com `D:\datasus\cache` presente
  (14 grupos), vários testes executam consultas reais — ex.:
  `test_agrupamento_mes.py::test_4_numero_por_extenso_nos_ultimos_anos` (a seleção de período
  varre as VIEWs Parquet de cada grupo), `test_5_...`, `test_7_...`, `test_llm_columns_cache.py`
  e a telemetria de `test_schema_comments_prompt.py`. Esses casos levam **minutos**; a
  **coleta** — que é o que a P4 torna determinística — segue em **~0,35 s / 193 testes**.
- **Marcadores**: para rodar só a suíte rápida,
  `pytest test/ -m "not slow and not requires_llm"`.
- **`LLM_ENABLED=false`** evita chamadas de rede durante os testes.

## Uso

```powershell
# Suíte completa (determinística)
python -m pytest test/ -q

# Só os testes rápidos (sem dados/LLM)
$env:LLM_ENABLED="false"; python -m pytest test/ -q -m "not slow and not requires_llm"

# Coleta apenas (verifica que não há INTERNALERROR)
python -m pytest test/ --collect-only -q
```
