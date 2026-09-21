from __future__ import annotations

import base64
from io import BytesIO
from typing import Any



def gerar_grafico_barras(
    resultados: list[dict[str, Any]],
    coluna_categoria: str | None = None,
    coluna_valor: str | None = None,
    limite: int = 100,
    periodos: set[tuple[str, str]] | None = None,
) -> str | None:
    """Gera um gráfico de barras dos resultados e retorna uma data URL PNG."""
    if not resultados:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    import seaborn as sns

    tabela = pd.DataFrame(resultados)
    if tabela.empty:
        return None

    tabela = _filtrar_periodos(tabela, periodos)
    if tabela.empty:
        return None

    if coluna_valor is None:
        colunas_numericas = [coluna for coluna in tabela.columns if _eh_numerica(tabela[coluna])]
        coluna_valor = _escolher_coluna_valor(colunas_numericas) if colunas_numericas else "quantidade"
        if coluna_valor == "quantidade":
            tabela[coluna_valor] = 1
    if coluna_valor not in tabela.columns:
        return None
    if coluna_categoria is None:
        colunas_categoria = [coluna for coluna in tabela.columns if coluna != coluna_valor]
        coluna_categoria = colunas_categoria[0] if colunas_categoria else None

    dados = tabela.copy()
    dados[coluna_valor] = pd.to_numeric(dados[coluna_valor], errors="coerce")
    dados = dados.dropna(subset=[coluna_valor]).head(limite)
    if dados.empty:
        return None
    if coluna_categoria is None:
        dados["categoria"] = dados.index.astype(str)
        coluna_categoria = "categoria"
    else:
        dados[coluna_categoria] = dados[coluna_categoria].fillna("N/A").astype(str)

    altura = max(4.0, min(8.0, 2.5 + len(dados) * 0.12))
    largura = max(7.0, min(14.0, 7.0 + len(dados) * 0.08))
    sns.set_theme(style="whitegrid", palette="deep")
    figure, axis = plt.subplots(figsize=(largura, altura))
    sns.barplot(data=dados, x=coluna_categoria, y=coluna_valor, ax=axis, errorbar=None)
    for container in axis.containers:
        axis.bar_label(
            container,
            labels=[_formatar_valor(valor) for valor in dados[coluna_valor]],
            label_type="center",
            rotation=90,
            color="white",
            fontsize=8,
        )
    axis.set_xlabel(coluna_categoria.replace("_", " ").title())
    axis.set_ylabel(coluna_valor.replace("_", " ").title())
    axis.tick_params(axis="x", rotation=45)
    figure.tight_layout()

    output = BytesIO()
    figure.savefig(output, format="png", dpi=140, bbox_inches="tight")
    plt.close(figure)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _formatar_valor(valor: Any) -> str:
    numero = float(valor)
    if numero.is_integer():
        return f"{int(numero):,}".replace(",", ".")
    return f"{numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _filtrar_periodos(tabela: pd.DataFrame, periodos: set[tuple[str, str]] | None) -> pd.DataFrame:
    """Filtra a tabela pelo período selecionado.
    
    Usa parquet_year/parquet_month se estiverem presentes; caso contrário,
    tenta identificar colunas de ano/mês por nome flexível (ano, year, anio, etc.).
    """
    if not periodos:
        return tabela
    import pandas as pd

    # Tenta localizar colunas de ano/mês com nomes mais flexíveis
    coluna_ano = None
    coluna_mes = None
    
    # Procura por nomes exatos primeiro
    for coluna in tabela.columns:
        nome = coluna.casefold().strip()
        if nome in {"ano", "year", "parquet_year", "anio"}:
            coluna_ano = coluna
            break
    if coluna_ano is None:
        # Procura por coluna que contenha valores de ano (4 dígitos)
        for coluna in tabela.columns:
            try:
                valores = tabela[coluna].astype(str).str.extract(r'(20\d{2})', expand=False)
                if valores.notna().any():
                    # Verifica se a maioria dos valores parece ser ano
                    frac = valores.notna().mean()
                    if frac >= 0.5:
                        coluna_ano = coluna
                        break
            except Exception:
                continue
    
    for coluna in tabela.columns:
        nome = coluna.casefold().strip()
        if nome in {"mes", "month", "parquet_month", "mês"}:
            coluna_mes = coluna
            break
    if coluna_mes is None and coluna_ano is not None:
        # Procura por coluna que contenha valores de mês (01-12)
        for coluna in tabela.columns:
            if coluna == coluna_ano:
                continue
            try:
                valores = tabela[coluna].astype(str).str.zfill(2)
                mascarames = valores.isin([f"{i:02d}" for i in range(1, 13)])
                if mascarames.mean() >= 0.5:
                    coluna_mes = coluna
                    break
            except Exception:
                continue

    if coluna_ano is None and coluna_mes is None:
        # Não há como filtrar por período nos dados
        return tabela
    
    mascara = pd.Series(True, index=tabela.index)
    if coluna_ano is not None:
        anos = {str(ano) for ano, _ in periodos}
        mascara &= tabela[coluna_ano].astype(str).str.extract(r'(20\d{2})', expand=False).isin(anos) | \
                   tabela[coluna_ano].astype(str).isin(anos)
    if coluna_mes is not None:
        meses = {str(mes).zfill(2) for _, mes in periodos}
        mascara &= tabela[coluna_mes].astype(str).str.zfill(2).isin(meses)
    return tabela.loc[mascara]


def _eh_numerica(coluna: pd.Series) -> bool:
    import pandas as pd

    if pd.api.types.is_numeric_dtype(coluna):
        return True
    convertida = pd.to_numeric(coluna, errors="coerce")
    return convertida.notna().any() and convertida.notna().mean() >= 0.8


def _escolher_coluna_valor(colunas: list[str]) -> str:
    indicadores = ("caso", "total", "registro", "quant", "count", "soma", "valor", "media", "média")
    for coluna in colunas:
        if any(indicador in coluna.casefold() for indicador in indicadores):
            return coluna
    return colunas[-1]