from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import matplotlib

from config.constants import CHART_TYPE_KEYWORDS

matplotlib.use("Agg")

LOGGER = logging.getLogger("datasus_llm")


class LLMGrafic:
    """Gera gráficos dinâmicos via LLM, executando código matplotlib retornado pela API."""

    # Mapeamento de palavras-chave para tipos de gráfico
    # IMPORTANTE: donut deve vir ANTES de pizza para que "donut"
    # seja detectado como donut em vez de pizza
    # Centralizado em config/constants.py (CHART_TYPE_KEYWORDS)
    TIPOS_GRAFICO_KEYWORDS: dict[str, list[str]] = CHART_TYPE_KEYWORDS

    def __init__(self, settings) -> None:
        # Armazena referência às Settings para leitura das configurações
        # centralizadas de temperature/max_tokens para geração de gráficos (sugestão 7)
        self.settings = settings
        self.llm_enabled = settings.llm_enabled
        # Modelo Llama servido via Ollama (API compatível com OpenAI).
        self.model = settings.llm_model
        self.client = None
        if self.llm_enabled:
            from openai import OpenAI

            self.client = OpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                max_retries=settings.llm_max_retries,
            )
        LOGGER.info(
            "llm_grafic configurado enabled=%s client_ready=%s model=%s base_url=%s",
            self.llm_enabled,
            self.client is not None,
            self.model,
            settings.llm_base_url,
        )

    @staticmethod
    def detectar_tipo_grafico(pergunta: str) -> str | None:
        """
        Detecta o tipo de gráfico solicitado na pergunta do usuário.
        Retorna um identificador de tipo ('pizza', 'horizontal', 'linha', etc.)
        ou None se não houver preferência explícita.
        """
        if not pergunta:
            return None

        pergunta_lower = pergunta.lower().strip()

        # Verifica se a pergunta menciona explicitamente um tipo de gráfico
        # Se não menciona "gráfico"/"grafico", evita falsos positivos
        menciona_grafico = any(
            palavra in pergunta_lower
            for palavra in ["gráfico", "grafico", "chart", "plot", "visualizar", "exibir", "mostre", "mostrar", "crie", "criar", "faça", "faca", "gere", "gerar", "desenhe", "desenhar"]
        )

        # Ordem de prioridade - se múltiplas palavras-chave, a primeira encontrada
        for tipo, keywords in LLMGrafic.TIPOS_GRAFICO_KEYWORDS.items():
            for keyword in keywords:
                if keyword in pergunta_lower:
                    # Para "linha", exige que a pergunta fale sobre gráfico de linha
                    # ou contenha palavras relacionadas a visualização/série temporal
                    if tipo == "linha":
                        # Se a pergunta não menciona explicitamente gráfico/visualização,
                        # procura por termos específicos que indicam solicitação de gráfico de linha
                        se_refere_linha = any(
                            termo in pergunta_lower
                            for termo in ["gráfico de linha", "grafico de linha", "line chart", "line plot"]
                        ) or any(
                            palavra in pergunta_lower
                            for palavra in ["tendência", "tendencia", "evolução", "evolucao", "série temporal", "serie temporal"]
                        )
                        if not se_refere_linha and not menciona_grafico:
                            continue
                        # Exclui falsos positivos comuns
                        if any(
                            expressao in pergunta_lower
                            for expressao in ["linha do tempo", "linha de cuidado", "linha de pesquisa", "linha de cuidado", "linha do tratamento", "linha de base"]
                        ):
                            continue
                    return tipo

        return None

    def gerar_grafico_llm(
        self,
        dados: list[dict[str, Any]],
        pergunta: str,
        tipo_grafico: str | None = None,
    ) -> str | None:
        """
        Consulta o LLM para gerar código matplotlib que cria um gráfico
        baseado nos dados fornecidos, executa o código e retorna o gráfico
        como data URL base64 em formato JPG.

        Args:
            dados: lista de dicionários com os dados
            pergunta: pergunta do usuário
            tipo_grafico: tipo de gráfico desejado (opcional).
                Valores aceitos: 'pizza', 'donut', 'horizontal', 'vertical',
                'linha', 'area', 'scatter', 'radar', etc.
                Se None, será detectado automaticamente da pergunta
                ou deixado à escolha do LLM.
        """
        if not self.client:
            LOGGER.warning("llm_grafic cliente não disponível; retornando None")
            return None

        if not dados:
            return None

        # Detectar tipo de gráfico a partir da pergunta se não fornecido
        tipo_detectado = tipo_grafico or self.detectar_tipo_grafico(pergunta)

        # Prompt para o LLM gerar código matplotlib
        prompt = self._build_chart_prompt(dados, pergunta, tipo_detectado)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=getattr(self.settings, "llm_chart_temperature", 0.2),
                max_tokens=getattr(self.settings, "llm_chart_max_tokens", 2048),
                messages=[{"role": "user", "content": prompt}],
            )
            codigo_python = response.choices[0].message.content.strip()

            # Extrair apenas o código Python válido
            codigo_limpo = self._extract_python_code(codigo_python)
            if not codigo_limpo:
                LOGGER.warning("llm_grafic não retornou código Python válido")
                return None

            # Executar o código para gerar a imagem em JPG
            imagem_base64 = self._executar_codigo_grafico(codigo_limpo, dados, tipo_detectado)
            if imagem_base64 is None:
                LOGGER.warning("llm_grafic falhou ao executar código para gerar gráfico")
                return None

            LOGGER.info(
                "llm_grafic gráfico gerado com sucesso (tipo=%s base64_len=%d)",
                tipo_detectado or "auto",
                len(imagem_base64),
            )
            return f"data:image/jpeg;base64,{imagem_base64}"

        except Exception as e:
            LOGGER.error("llm_grafic erro ao gerar gráfico: %s", e)
            return None

    def _build_chart_prompt(
        self,
        dados: list[dict[str, Any]],
        pergunta: str,
        tipo_grafico: str | None = None,
    ) -> str:
        """
        Constrói o prompt para o LLM gerar código matplotlib.
        Se tipo_grafico for fornecido, instrui o LLM a usar esse tipo.
        """
        dados_json = json.dumps(dados, default=str, ensure_ascii=False)

        # Instruções específicas para o tipo de gráfico solicitado
        tipo_instruction = ""
        if tipo_grafico:
            tipo_map: dict[str, str] = {
                "pizza": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de PIZZA (pie chart)!
Use SOMENTE ax.pie() para criar o gráfico.
- Se houver mais de 8 categorias, agrupe as menores em "Outros"
- Use autopct='%1.1f%%' para mostrar percentuais
- Adicione legendas fora do gráfico: ax.legend(title='Categorias', bbox_to_anchor=(1.05, 1))
- Garanta que a figura seja circular: use ax.set_aspect('equal') ou axis('equal')
- Cores: use uma paleta agradável como plt.cm.Set3 ou plt.cm.tab20
- IMPORTANTE: Ao usar pie, a figura pode precisar ser quadrada (figsize=(8, 8))
""",
                "donut": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de PIZZA EM FORMATO DE ROSCA (donut)!
Use ax.pie() com a opção `wedgeprops=dict(width=0.3)` para criar o efeito donut.
- Se houver mais de 8 categorias, agrupe as menores em "Outros"
- Use autopct='%1.1f%%' para mostrar percentuais
- Adicione um texto central se útil
- Adicione legendas fora do gráfico: ax.legend(title='Categorias', bbox_to_anchor=(1.05, 1))
- Cores: use uma paleta agradável como plt.cm.Set3
""",
                "horizontal": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de BARRAS HORIZONTAIS!
Use SOMENTE ax.barh() (a função com 'h' no final para horizontal).
- Perfeito para muitas categorias (mais de 8)
- Inverta a ordem para que a maior fique no topo se útil: ax.invert_yaxis()
- Adicione valores ao final das barras com ax.bar_label(container, padding=5)
- Aumente a altura/figura verticalmente para acomodar todas as barras: figsize=(10, 8) ou maior
""",
                "vertical": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de BARRAS VERTICAIS!
Use SOMENTE ax.bar() (a função sem 'h' para vertical).
- Adicione valores acima das barras com ax.bar_label()
- Configure o título e rótulos dos eixos
- Rotacione rótulos do eixo X em 45° se necessário
""",
                "linha": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de LINHA!
Use SOMENTE ax.plot() para criar o gráfico.
- Ideal para dados temporais (séries temporais)
- Adicione marcadores: ax.plot(x, y, marker='o')
- Adicione grade: ax.grid(True, alpha=0.3)
- Se houver múltiplas séries, crie legendas
- Formate o eixo X para textos de anos/meses
""",
                "area": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de ÁREA!
Use ax.fill_between() para área simples ou ax.stackplot() para múltiplas séries empilhadas.
- Se houver múltiplas colunas numéricas, use ax.stackplot()
- Adicione transparência (alpha) para sobreposição se necessário
- Crie legendas quando houver múltiplas séries
""",
                "dispersao": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de DISPERSÃO (scatter)!
Use SOMENTE ax.scatter() para criar o gráfico.
- Este tipo é bom para correlações entre duas variáveis numéricas
- Se os dados não tiverem duas colunas numéricas, use barra do tipo mais adequado
- Adicione cores e tamanhos de pontos para diferenciar grupos, se útil
""",
                "radar": """
**INSTRUÇÃO DE TIPO FORÇADO**: O usuário pediu/quer um gráfico de RADAR (spider/radar chart)!
- Crie os eixos polares com: fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
- Use ax.plot() e ax.fill() para cada variável
- Categorias devem ser dispostas ao redor do círculo
""",
            }
            if tipo_grafico in tipo_map:
                tipo_instruction = tipo_map[tipo_grafico]

        return f"""
Você é um especialista em visualização de dados com matplotlib.

Dados de uma consulta DATASUS:

Pergunta: {pergunta}

Dados em formato JSON:
{dados_json}

Gere um código Python COMPLETO que:
1. Use matplotlib para criar um gráfico apropriado baseado nos dados.
2. Analise as colunas e seus tipos. Identifique:
   - Colunas categóricas (string) → use como eixo X
   - Colunas numéricas → use como valores

{tipo_instruction}

3. Se NENHUM tipo for forçado, escolha o melhor tipo de gráfico:
   - Barra vertical para comparação entre categorias
   - Barra horizontal se houver muitas categorias (mais de 8)
   - Linha se os dados forem temporais/sequenciais
   - Pizza apenas para distribuição com até 6 categorias
   - Dispersão para correlação entre duas variáveis numéricas
4. Formate o gráfico com:
   - Título descritivo em português baseado na pergunta
   - Rótulos dos eixos em português
   - Cores consistentes e agradáveis
   - Legenda quando necessário
   - Valores nos pontos/barras quando útil
   - Rotação de 45° nos rótulos do eixo X para evitar sobreposição
5. Configure o tema com `plt.style.use('ggplot')` ou similar.
6. Formate valores numéricos no formato brasileiro (ex.: 1.234.567)

IMPORTANTE - Regras obrigatórias:
- Crie a figura com: `fig, ax = plt.subplots(figsize=(10, 6))`
- Salve a figura COMPULSORIAMENTE como JPG: `plt.savefig('/tmp/llm_graph.jpg', format='jpeg', dpi=150, bbox_inches='tight')`
- Feche a figura: `plt.close(fig)`
- Importe apenas: `import matplotlib`, `import matplotlib.pyplot as plt`, `import json`, `import base64`
- NÃO use f-strings com caracteres especiais que possam causar erro
- Use os dados diretamente da variável `dados` que será fornecida como lista de dicionários
- NÃO abra arquivos externos ou leia dados de outros lugares
- NÃO chame `plt.show()`
- Retorne apenas o código Python sem markdown, sem explicação ou comentários extensos
- Salve a imagem em '/tmp/llm_graph.jpg' (formato JPG)

Formato de saída: apenas o código Python puro, sem ```markdown``` nem comentários antes ou depois.
"""

    @staticmethod
    def _extract_python_code(content: str) -> str | None:
        """Extrai código Python do conteúdo retornado pelo LLM."""
        if not content or not content.strip():
            return None

        text = content.strip()

        # Tentar extrair de blocos fenced
        fenced_match = re.search(
            r"```(?:python|py)?\s*(.*?)```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced_match:
            return fenced_match.group(1).strip()

        # Se não houver markdown, mas houver código python (começando com import ou fig,)
        python_match = re.search(
            r"(?:^(?:import|from)\s+\w+|^(?:fig|ax|plt)\.\w+)",
            text,
            flags=re.MULTILINE,
        )
        if python_match:
            return text[python_match.start():].strip()

        return None

    def _executar_codigo_grafico(
        self,
        codigo: str,
        dados: list[dict[str, Any]],
        tipo_grafico: str | None = None,
    ) -> str | None:
        """
        Executa o código Python retornado pelo LLM para gerar o gráfico.
        """
        import matplotlib.pyplot as plt

        try:
            # Executar o código com os dados disponíveis no escopo
            exec_globals = {
                "dados": dados,
                "json": json,
                "base64": base64,
                "matplotlib": matplotlib,
                "plt": plt,
                "tipo_grafico": tipo_grafico,
            }

            # Executar o código
            exec(codigo, exec_globals)

            # Verificar se a imagem foi criada
            caminho_imagem = Path("/tmp/llm_graph.jpg")
            if not caminho_imagem.exists():
                LOGGER.warning(
                    "llm_grafic código executado mas imagem não foi criada em /tmp/llm_graph.jpg"
                )
                return None

            # Ler e converter para base64
            with open(caminho_imagem, "rb") as f:
                imagem_bytes = f.read()

            # Limpar arquivo temporário
            caminho_imagem.unlink(missing_ok=True)

            return base64.b64encode(imagem_bytes).decode("ascii")

        except Exception as e:
            LOGGER.error("llm_grafic erro ao executar código: %s", e)
            # Limpar arquivo temporário se existir
            try:
                caminho_imagem = Path("/tmp/llm_graph.jpg")
                caminho_imagem.unlink(missing_ok=True)
            except Exception:
                pass
            return None