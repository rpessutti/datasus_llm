# DATASUS LLM

Aplicativo local para indexar arquivos Parquet do DATASUS, consultar o cache DuckDB e fazer perguntas em português por uma página HTML.

## Preparar

```bash
source dataus_llm/bin/activate
pip install -r requirements.txt
mkdir -p data
```

Coloque os arquivos `.parquet` em `data/` (subdiretórios também são lidos). A aplicação valida cada arquivo, copia arquivos novos ou alterados para `cache/parquet/` e materializa os dados válidos na tabela DuckDB persistente `datasus_cache`, exposta pela view `datasus`.

Quando já existe um cache persistido, a aplicação ativa a API imediatamente. A primeira carga de um cache vazio ocorre em segundo plano; durante esse período, o status informa que o cache está carregando e as perguntas aguardam a conclusão.

Arquivos Parquet corrompidos são registrados no log, removidos da pasta de origem e do cache, e não interrompem os demais arquivos. Depois da primeira carga, se o manifesto (nome, tamanho e data de modificação) não mudou, a aplicação reutiliza `datasus_cache` e não relê os Parquet na inicialização.

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
