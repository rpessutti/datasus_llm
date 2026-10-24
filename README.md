# DATASUS LLM

Aplicativo local para indexar arquivos Parquet do DATASUS, consultar o cache DuckDB e fazer perguntas em português por uma página HTML.

## Preparar

```bash
source dataus_llm/bin/activate
pip install -r requirements.txt
mkdir -p data
```

Coloque os arquivos `.parquet` em `data/` (subdiretórios também são lidos). A aplicação valida cada arquivo e o ingere no banco DuckDB do **grupo** correspondente (`<grupo>.datasus_cache`, exposto pela view `<grupo>.datasus`).

Quando já existe um cache persistido, a aplicação ativa a API imediatamente; grupos ausentes/incompletos são ingeridos de forma síncrona na inicialização (`ensure_ready_sync`) e o `/api/status` informa o progresso.

Arquivos Parquet corrompidos são registrados no log e ignorados, sem interromper os demais. O progresso por grupo fica em `group_checkpoint`/`file_manifest` (dentro do banco do grupo) e é retomável.

## Modo grupos e view unificada (UNION)

A aplicação usa **modo grupos**: cada grupo do DATASUS (ex.: `er`, `ps`, `sp`) vive em seu próprio banco anexado (`<grupo>.datasus_cache`). Para consultar todos ao mesmo tempo, a aplicação materializa a **view unificada** `datasus_query_cache` como um `UNION ALL` dos grupos selecionados para a pergunta.

Decisões de projeto relevantes ao modo grupos:

- **#3 — Colunas de período materializadas na ingestão** (`atend_ano`/`atend_mes`): cada grupo ganha as colunas normalizadas de período como **tabela física** na ingestão, derivadas do campo nativo de período do grupo. Consultas de tempo não dependem de parsing em tempo de execução.
- **#4 — View unificada alinhada ao campo nativo de período**: o `UNION ALL` da view usa o campo de período **nativo** de cada grupo (não um derivado por nome de arquivo), garantindo que o recorte temporal (`atend_ano`/`atend_mes`) corresponda ao dado real de atendimento.
- **#5 — Grupos sem campo de período são excluídos do UNION**: grupos que não expõem nenhum campo de período são automaticamente deixados fora da view unificada (evita colunas de período nulas/ambíguas no `UNION ALL`).

Outras otimizações aplicadas:

- **#6 — Schema canônico**: o `UNION` reutiliza o schema canônico já consolidado (S2) em vez de reconsultar o catálogo a cada preparação.
- **#1 — Qualificação de SQL por parser**: a qualificação de colunas/relações no SQL gerado é feita com uma abordagem estrutural (sem regex arriscada), evitando reescritas incorretas.
- **#7 — Prefixos de CID pré-computados**: os prefixos de CID reais presentes nos dados são pré-computados na ingestão (tabela `cid_prefixes`) e apenas lidos nas consultas, em vez de escanear colunas de diagnóstico a cada pergunta.
- **#8/#9/#10 — LIMIT/ORDER BY/projeção mínima**: `execute` respeita o limite da pergunta (fallback `DEFAULT_FETCH_ROWS`); o fallback de coluna ausente reconstrói a view com projeção **mínima** (colunas usadas ∪ faltante), não com todas as colunas; e o prompt do LLM exige `ORDER BY` em toda query com `LIMIT` (resultados reproduzíveis com `preserve_insertion_order=false`).

## Catálogo de grupos e módulos do cache

O catálogo vive no banco principal (`datasus.duckdb`) e substitui a descoberta por glob + sondas por grupo:

- `group_registry` — nome, caminho do banco, pasta de origem, campo de período, `files`, `rows_est` e `status`.
- `group_periods` — `(grupo, atend_ano, atend_mes)` publicados no `finalize` de cada grupo.
- `ingest_log` — histórico de ingestões.

`DatasusCache.sync_groups()` reconcilia o catálogo com o disco; `groups_needing_ingest()` compara `files` com os Parquet da origem; `periods_for_question()` resolve o recorte com **uma** consulta ao catálogo (sem `SELECT DISTINCT` por grupo).

Módulos extraídos (responsabilidade única):

| Módulo | Responsabilidade |
|---|---|
| `db/group_registry.py` | DDL, sincronização e leitura do catálogo |
| `db/period_parser.py` | parsing de períodos na pergunta (`parse_periods`, `wants_monthly_grouping`) |
| `db/sql_repair.py` | reparo/validação do SQL gerado |
| `db/summary.py` | tabelas `resumo_*` (leem as tabelas dos grupos, não Parquet) |
| `db/cid_prefixes.py` | prefixos de CID e carga do `cid10` |
| `db/unified_view.py` | SQL do `UNION ALL` da view unificada |

Aliases temporários mantidos para compatibilidade: `DatasusCache.columns` (`get_grupo_columns`), `DatasusCache._summary_tables` (`summary_tables`), `_GroupCache.scalar` (`_scalar`), `_GroupCache.ensure` (`reopen`) e `_GroupCache.period_index` (`_refresh_period_index`).

Operação:

```bash
# limpa o legado do banco principal e publica o catálogo (F5.5)
python -u dev_tools/migrate_group_catalog.py --limpar-legado --backfill

# reconstrói a tabela de um grupo a partir dos Parquet (longo; já cria
# atend_ano/atend_mes derivados do campo nativo de período)
python -u dev_tools/rebuild_group_cache.py <grupo>
```

Caches legados sem as colunas físicas `atend_ano`/`atend_mes` são migrados **in
place** apenas até `DATASUS_MIGRATE_MAX_ROWS` linhas (padrão 5.000.000). Acima
disso a migração é desativada no boot (o backfill reescreveria a tabela inteira)
e o grupo precisa ser reconstruído com o comando acima; até lá, a view unificada
usa o campo nativo de período do grupo.

## Robustez contra disco removível (USB / HD externo)

Se os dados (`DATASUS_DATA_DIR`/`DATASUS_CACHE_DIR`) ficarem num pen drive ou HD externo, uma desconexão, suspensão ou queda de montagem pode derrubar a aplicação no meio de uma leitura. Para evitar isso, a aplicação agora:

- **Re-tenta com backoff** cada operação de I/O direta sobre o disco (`stat`, `rglob`, listagem de diretórios), tratando falhas transitórias em vez de propagar `OSError` e interromper a ingestão. O número de tentativas e o atraso base são configuráveis por `USB_IO_RETRIES` (padrão 3) e `USB_IO_BACKOFF` (padrão 0.5s, com backoff exponencial: 0.5s, 1s, 2s).
- **Nunca derruba a ingestão por um arquivo** que some ou falhe no meio: arquivos indisponíveis num USB desconectado são pulados e deixados para o próximo ingest quando o disco re-montar, em vez de abortar o processo.
- **Copia em arquivo temporário e valida o tamanho** antes de renomear para o cache, evitando que uma leitura parcial deixe um Parquet "válido" mas truncado.
- **Faz fallback do banco DuckDB**: se o banco configurado (num USB) falhar ao abrir, a aplicação abre um banco local em `./cache/datasus.fallback.duckdb` e o `/api/status` reporta esse caminho, mantendo o servidor de pé (o `database` retornado reflete o caminho efetivamente usado).
- **Faz fallback do diretório de spill**: se o cache configurado não estiver gravável, o spill do DuckDB usa `./cache/duckdb_spill` local, e o log de arquivo cai para `./cache/datasus.log` local.

> Dica: para concentrar todas as gravações no disco local (rápido e estável) e só ler o Parquet do USB, defina `DATASUS_CACHE_DIR` e `LOG_FILE` apontando para uma pasta do disco local e deixe `DATASUS_DATA_DIR` apontando para o USB.

Opcionalmente, configure o LLM:

```bash
cp .env.example .env
# edite .env e mantenha LLM_ENABLED=true para enviar as perguntas ao modelo
# informe LLM_MODEL, LLM_BASE_URL e LLM_API_KEY do seu servidor Ollama
```

O backend de LLM é **local (Ollama)**. Suba o servidor e baixe o modelo:

```bash
ollama serve
ollama pull qwen3-coder:30b
```

Com `LLM_ENABLED=true` e o Ollama acessível em `LLM_BASE_URL` (padrão `http://localhost:11434/v1`), cada pergunta em linguagem natural é enviada ao modelo `LLM_MODEL` (padrão `qwen3-coder:30b`), que gera uma consulta SQL somente leitura para a view `datasus_query_cache`; o DuckDB executa essa consulta e o resultado é enviado ao modelo para gerar a resposta.

Variáveis de configuração do LLM:

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `LLM_ENABLED` | `true` | Liga/desliga o uso do LLM |
| `LLM_MODEL` | `qwen3-coder:30b` | Nome do modelo no Ollama |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | Endpoint OpenAI-compatível do Ollama |
| `LLM_API_KEY` | `ollama` | Chave fictícia (Ollama não exige) |
| `LLM_TIMEOUT` | `180` | Timeout global (s) do cliente LLM. As chamadas de geração/correção de SQL usam um teto de 60s por chamada (`min(LLM_TIMEOUT, 60)`) |
| `LLM_MAX_RETRIES` | `0` | Tentativas automáticas |

O prompt de geração SQL não envia amostras dos Parquets. Quando a SQL usa as tabelas agregadas `resumo_*`, a aplicação não materializa a view temporária sobre milhares de Parquets.

## Executar

```bash
source dataus_llm/bin/activate
python app.py
```

Abra `http://IP_DA_MAQUINA:8000` em outro computador da rede local. Na própria máquina, use http://127.0.0.1:8000. Para alterar o endereço e a porta, defina `DATASUS_HOST` e `DATASUS_PORT` no `.env`; o padrão é `0.0.0.0:8000`. Libere a porta 8000 no firewall do sistema, se necessário. Cada Parquet lido com sucesso ou removido por erro é registrado em `LOG_FILE` (por padrão `/mnt/d/datasus/cache/datasus.log` no ambiente configurado), com rotação automática. Use `LOG_LEVEL=DEBUG` para rastrear argumentos, retornos, duração e exceções dos métodos.

## API

- `GET /api/status`: quantidade de arquivos e registros indexados.
- `POST /api/ingest`: reprocessa arquivos novos ou alterados e atualiza o índice persistente de períodos. Use esse endpoint depois de adicionar ou substituir Parquets; a ingestão não é executada automaticamente a cada abertura.
- `POST /api/build-indexes`: materializa os resumos de diagnóstico por período, CID e sexo. Execute após o ingest; essa operação pode ler todos os Parquets uma vez.
- `POST /api/ask` com `{"question":"..."}`: executa a consulta.

Para criar os resumos uma vez após a carga inicial:

```bash
curl -X POST http://127.0.0.1:8000/api/build-indexes
```

O SQL produzido pelo LLM é restrito a `SELECT`/`WITH`, usa as tabelas agregadas quando disponíveis e a view `datasus_query_cache` para consultas detalhadas. O SQL é auditável na resposta e nos logs.
