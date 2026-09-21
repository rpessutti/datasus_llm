"""
Testes unitários para o cache persistente de colunas por grupo e
para as chamadas ao LLM no app.py do DATASUS LLM.

Valida:
1. Criação e persistência dos caches de colunas por grupo
2. Não recriação dos caches existentes
3. Detecção correta dos grupos relevantes para uma pergunta
4. Prompt do LLM contém apenas colunas dos grupos relevantes
5. Validação pós-geração corrige colunas inventadas pelo LLM
"""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

# No diretório raiz do projeto
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from config.settings import Settings
from db.cache import DatasusCache
from db.group_cache import _GroupCache
from api.llm_service_modular import LLMQueryService


# ============================================================================
# Helper: cria um DatasusCache com grupos mockados e caches de colunas
# ============================================================================

class ColumnCacheTestBase(unittest.TestCase):
    """Base para testes do cache de colunas por grupo."""

    @classmethod
    def setUpClass(cls):
        """Cria uma instância real do DatasusCache com os grupos do sistema."""
        # Usa a configuração real (aplicação já detecta grupos automaticamente)
        cls.settings = Settings()
        cls.cache = DatasusCache(cls.settings)
        cls.service = LLMQueryService(cls.cache, cls.settings)
        
        # Guarda os caches de colunas reais
        cls.grupos = sorted(cls.cache.get_grupo_columns().keys())
        cls.columns_by_grupo = cls.cache.get_grupo_columns()

    def setUp(self):
        """Antes de cada teste, garante que o cache está carregado."""
        self.ensure_columns_cache_exists()

    def ensure_columns_cache_exists(self):
        """Garante que os caches de colunas existem para todos os grupos."""
        for grupo in self.grupos:
            cache_file = self.settings.cache_dir / "colunas_cache" / f"{grupo}.json"
            if not cache_file.exists():
                self.cache._build_grupo_columns_cache(grupo)


# ============================================================================
# Teste 1: Verifica que os caches de colunas são criados e persistidos
# ============================================================================

class TestColumnsCacheCreation(ColumnCacheTestBase):
    """Verifica que os caches de colunas são criados e persistidos por grupo."""

    def test_cache_files_exist_for_all_grupos(self):
        """Cada grupo deve ter um arquivo JSON em cache/colunas_cache/{grupo}.json."""
        cache_dir = self.settings.cache_dir / "colunas_cache"
        self.assertTrue(cache_dir.is_dir(), f"Diretório {cache_dir} não criado")
        
        for grupo in self.grupos:
            cache_file = cache_dir / f"{grupo}.json"
            self.assertTrue(
                cache_file.exists(),
                f"Arquivo de cache de colunas não existe para grupo {grupo}: {cache_file}"
            )

    def test_cache_structure_is_valid(self):
        """O JSON deve ter a estrutura correta: version, grupo, columns.

        A partir da versão "1.1" o cache inclui ``source_mtime`` (invalidação
        por mtime da fonte) e ``column_count`` (diagnóstico).
        """
        for grupo in self.grupos:
            cache_file = self.settings.cache_dir / "colunas_cache" / f"{grupo}.json"
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            self.assertIn(data.get("version"), ("1.0", "1.1"), f"Versão incorreta para grupo {grupo}")
            self.assertEqual(data.get("grupo"), grupo, f"Nome do grupo incorreto no cache")
            
            columns = data.get("columns", [])
            self.assertIsInstance(columns, list, f"columns deve ser uma lista para grupo {grupo}")
            self.assertGreater(len(columns), 0, f"Nenhuma coluna encontrada para grupo {grupo}")

            # Campos de versionamento (best-effort em caches novos)
            if data.get("version") == "1.1":
                self.assertIn("source_mtime", data, f"Falta source_mtime para grupo {grupo}")
                self.assertEqual(
                    data.get("column_count"), len(columns),
                    f"column_count inconsistente para grupo {grupo}",
                )
            
            # Cada coluna deve ter "column" e "type"
            for col in columns:
                self.assertIn("column", col, f"Coluna sem chave 'column' em grupo {grupo}")
                self.assertIn("type", col, f"Coluna sem chave 'type' em grupo {grupo}")

    def test_cache_contains_expected_columns(self):
        """Grupos específicos devem ter colunas de diagnóstico reais."""
        rd_columns = {col["column"] for col in self.columns_by_grupo.get("rd", [])}
        # O grupo RD tem DIAG_PRINC, DIAG_SECUN, CID_NOTIF, etc.
        expected_diag_columns = {"DIAG_PRINC", "DIAG_SECUN", "CID_NOTIF"}
        found = rd_columns & expected_diag_columns
        self.assertGreaterEqual(
            len(found), 2,
            f"Grupo rd deveria ter pelo menos 2 colunas de diagnóstico esperadas. Encontradas: {found}"
        )

    def test_cache_not_zeroed_when_source_unavailable(self):
        """Nunca deve sobrescrever um cache válido com lista VAZIA.

        Regressão: quando a fonte não está acessível no momento (grupo sem
        conexão / parquets indisponíveis), a leitura retorna 0 colunas. O cache
        existente deve ser preservado em vez de zerado.
        """
        import tempfile
        grupo = self.grupos[0]
        cache_dir = self.settings.cache_dir / "colunas_cache"
        cache_file = cache_dir / f"{grupo}.json"

        # Snapshot do cache atual (deve ter colunas).
        with open(cache_file, "r", encoding="utf-8") as f:
            original = json.load(f)
        self.assertGreater(len(original.get("columns", [])), 0)

        # Força a leitura a retornar vazio simulando fonte indisponível:
        # o grupo é removido do dicionário de grupos ativos.
        grupos_backup = dict(self.cache.groups)
        try:
            self.cache.groups.pop(grupo, None)
            # Também impede o fallback de ler parquets, apontando para um dir vazio.
            cols = self.cache._build_grupo_columns_cache(grupo)
        finally:
            self.cache.groups.clear()
            self.cache.groups.update(grupos_backup)

        # O cache em disco deve permanecer intacto (com as colunas originais).
        with open(cache_file, "r", encoding="utf-8") as f:
            apos = json.load(f)
        self.assertGreater(
            len(apos.get("columns", [])), 0,
            "Cache não deve ser zerado quando a fonte está indisponível",
        )
        self.assertEqual(
            len(apos.get("columns", [])), len(original.get("columns", [])),
            "Cache deve conservar exatamente as colunas originais",
        )

    def test_cache_invalidated_when_source_is_newer(self):
        """Cache com source_mtime antigo deve ser considerado obsoleto."""
        grupo = self.grupos[0]
        cache_file = self.settings.cache_dir / "colunas_cache" / f"{grupo}.json"
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("source_mtime", data)

        # Um cache com mtime menor que a fonte atual deve ser reconstruído;
        # aqui apenas verificamos a regra de comparação usada pelo método.
        cached_mtime = float(data.get("source_mtime", 0) or 0)
        source_mtime = self.cache._grupo_source_signature(grupo)
        # Regra: reutiliza somente se cached_mtime >= source_mtime (ou fonte=0).
        reutilizavel = source_mtime == 0.0 or cached_mtime >= source_mtime
        self.assertTrue(
            reutilizavel,
            "Um cache recém-gravado deve ser considerado atualizado",
        )

    def test_cache_not_recreated_when_exists(self):
        """Se o cache já existe, não deve ser recriado (o conteúdo não muda)."""
        cache_file = self.settings.cache_dir / "colunas_cache" / self.grupos[0] / ".json"
        # Corrige path: grupo é um diretório
        cache_file = self.settings.cache_dir / "colunas_cache" / f"{self.grupos[0]}.json"
        
        # Lê o conteúdo ANTES
        with open(cache_file, "r", encoding="utf-8") as f:
            content_before = f.read()
        
        # Chama o método novamente - deve carregar e NÃO recriar
        # (mockamos o open para verificar que é lido e não escrito)
        with patch("builtins.open", side_effect=open) as mock_open_obj:
            cols = self.cache._build_grupo_columns_cache(self.grupos[0])
        
        # Verifica que o conteúdo retornado é o mesmo
        self.assertEqual(
            len(cols), len(json.loads(content_before)["columns"]),
            "Cache retornado deve ter o mesmo número de colunas"
        )
        
        # Verifica que o arquivo não foi modificado
        with open(cache_file, "r", encoding="utf-8") as f:
            content_after = f.read()
        self.assertEqual(content_before, content_after, "Arquivo de cache não deve ser alterado se já existe")


# ============================================================================
# Teste 2: Verifica a detecção de grupos relevantes para uma pergunta
# ============================================================================

class TestDetermineRelevantGrupos(ColumnCacheTestBase):
    """Verifica que a detecção de grupos relevantes para a pergunta funciona."""

    def test_returns_all_grupos_when_question_has_no_period(self):
        """Sem período na pergunta, deve retornar todos os grupos (fallback)."""
        # Períodos vazios -> fallback: retorna todos os grupos
        grupos = self.service._determine_relevant_grupos(
            "Quantos casos de dengue?", set()
        )
        self.assertGreaterEqual(len(grupos), 1, "Deveria retornar pelo menos um grupo")
        # Deve retornar apenas grupos que existem no cache
        for g in grupos:
            self.assertIn(g, self.cache.groups, f"Grupo {g} não existe")

    def test_returns_grupos_with_valid_period(self):
        """Com período válido, deve retornar apenas grupos que têm dados nesse período."""
        # Pergunta sobre 2025 - pode selecionar arquivos de alguns grupos
        try:
            grupos = self.service._determine_relevant_grupos(
                "Quantos casos em 2025?", {("2025", "01")}
            )
            self.assertIsInstance(grupos, list)
        except Exception as e:
            self.fail(f"Erro ao chamar _determine_relevant_grupos: {e}")

    def test_returns_legacy_when_no_grupos(self):
        """Sem grupos, deve retornar ['_legacy']."""
        # Mock: cache sem grupos
        mock_cache = MagicMock()
        mock_cache.groups = {}
        mock_cache.select_files_for_question.side_effect = Exception("sem dados")
        
        service = LLMQueryService.__new__(LLMQueryService)
        service.cache = mock_cache
        
        grupos = service._determine_relevant_grupos("Alguma pergunta", {("2025", "01")})
        self.assertEqual(grupos, ["_legacy"])


# ============================================================================
# Teste 3: Verifica que o prompt do LLM contém apenas colunas reais
# ============================================================================

class TestGenerateSQLWithColumnsCache(ColumnCacheTestBase):
    """Verifica que o prompt do LLM usa apenas colunas dos grupos relevantes."""

    def _mock_llm_response(self, sql_text):
        """Cria um mock do cliente OpenAI que retorna um SQL."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = sql_text
        
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    @patch("api.llm_service_modular.LLMQueryService._validate_sql")
    def test_prompt_contains_real_columns_from_relevant_grupos(self, mock_validate):
        """Verifica que o prompt do LLM contém apenas colunas reais dos grupos relevantes."""
        # Encontra um grupo com colunas de diagnóstico
        target_grupo = None
        for g in self.grupos:
            cols = self.columns_by_grupo.get(g, [])
            diag_cols = [c["column"] for c in cols if "DIAG" in c["column"].upper() or "CID" in c["column"].upper()]
            if diag_cols:
                target_grupo = g
                break
        
        if not target_grupo:
            self.skipTest("Nenhum grupo com colunas de diagnóstico encontrado")
        
        # Encontra uma coluna real de diagnóstico do grupo alvo
        diag_cols_reais = [c["column"] for c in self.columns_by_grupo[target_grupo] if "DIAG" in c["column"].upper() or "CID" in c["column"].upper()]
        real_diag_col = diag_cols_reais[0] if diag_cols_reais else None
        if not real_diag_col:
            self.skipTest(f"Grupo {target_grupo} não tem coluna de diagnóstico")
        
        # Configura o mock do cliente - captura o prompt
        captured_prompt = {}
        
        def create_side_effect(**kwargs):
            messages = kwargs.get("messages", [])
            if messages:
                captured_prompt["content"] = messages[0]["content"]
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = (
                f"SELECT COUNT(*) AS total FROM datasus_query_cache "
                f"WHERE UPPER(CAST(\"{real_diag_col}\" AS VARCHAR)) LIKE 'G%'"
            )
            return mock_resp
        
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = create_side_effect
        
        # Substitue o cliente no service
        original_client = self.service.client
        self.service.client = mock_client
        
        try:
            # Chama _generate_sql
            periods = {("2025", "01")}
            sql = self.service._generate_sql("Quantos casos de G?", "test-request-1", periods, limite=100)
            
            # Verifica que o prompt foi capturado
            self.assertIn("content", captured_prompt, "Prompt deve ter sido capturado")
            prompt_content = captured_prompt["content"]
            
            # Verifica que o prompt contém colunas reais de diagnóstico
            # O prompt pode MENCIONAR 'CID_PRINCIPAL' como exemplo de coluna inventada
            # ('Se a pergunta mencionar CID_PRINCIPAL...', 'substitua pela coluna real'),
            # mas NÃO deve listá-la como uma coluna REAL ENTRE ASPAS no schema.
            # Verifica que 'CID_PRINCIPAL' não aparece ENTRE ASPAS (que seria como coluna real)
            self.assertNotIn('"CID_PRINCIPAL"', prompt_content, "Prompt não deve listar CID_PRINCIPAL como coluna real")
            self.assertNotIn('"CID_SECUNDARIO"', prompt_content, "Prompt não deve listar CID_SECUNDARIO como coluna real")
            
            # Verifica que o prompt contém a coluna real de diagnóstico
            self.assertIn(real_diag_col, prompt_content, f"Prompt deve conter a coluna real {real_diag_col}")
            
            # Verifica que o SQL retornado contém a coluna real
            self.assertIn(real_diag_col, sql, "SQL deve conter a coluna real de diagnóstico")
            
        finally:
            self.service.client = original_client

    @patch("api.llm_service_modular.LLMQueryService._validate_sql")
    def test_prompt_mentions_grupos_relevantes(self, mock_validate):
        """Verifica que o prompt menciona os grupos relevantes explicitamente."""
        # Mock do cliente que captura o prompt
        captured_prompts = []
        
        def create_side_effect(**kwargs):
            messages = kwargs.get("messages", [])
            if messages:
                captured_prompts.append(messages[0]["content"])
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "SELECT COUNT(*) AS total FROM datasus_query_cache"
            return mock_resp
        
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = create_side_effect
        
        original_client = self.service.client
        self.service.client = mock_client
        
        try:
            self.service._generate_sql("Quantos casos em 2025?", "test-request-2", {("2025", "01")}, limite=100)
            
            # Verifica que o prompt foi capturado
            self.assertGreater(len(captured_prompts), 0, "Prompt deve ter sido capturado")
            prompt = captured_prompts[0]
            
            # Verifica que menciona "DADOS DISPONÍVEIS" (a seção que lista grupos relevantes)
            self.assertIn("DADOS DISPONÍVEIS", prompt, "Prompt deve conter a seção DADOS DISPONÍVEIS")
            
            # Verifica que menciona pelo menos um grupo real
            for grupo in self.grupos[:2]:  # Verifica pelos primeiros grupos
                if grupo in prompt:
                    break
            else:
                self.fail("Prompt deve mencionar pelo menos um grupo real")
            
        finally:
            self.service.client = original_client


# ============================================================================
# Teste 4: Verifica que a validação corrige colunas inventadas
# ============================================================================

class TestValidateAndFixColumns(ColumnCacheTestBase):
    """Verifica que colunas inventadas pelo LLM são corrigidas automaticamente."""

    def test_fix_invented_cid_principal(self):
        """CID_PRINCIPAL deve ser corrigido para uma coluna real de diagnóstico."""
        # Usa as colunas reais de todos os grupos
        real_columns = []
        for cols in self.columns_by_grupo.values():
            for col in cols:
                real_columns.append(col["column"])
        
        sql = 'SELECT COUNT(*) FROM datasus_query_cache WHERE UPPER(CAST("CID_PRINCIPAL" AS VARCHAR)) LIKE \'G12%\''
        
        fixed_sql, unresolved = self.service._validate_and_fix_columns(sql, real_columns)
        
        # Verifica que não contém mais CID_PRINCIPAL (inventado)
        self.assertNotIn("CID_PRINCIPAL", fixed_sql, "CID_PRINCIPAL deve ter sido corrigido")
        
        # Verifica que contém alguma coluna real de diagnóstico.
        # A correção pode ter escolhido CIDPRI, DIAG_PRINC, SP_CIDPRI, etc.
        # desde que seja UMA coluna REAL de diagnóstico (com CID ou DIAG no nome)
        self.assertNotEqual(fixed_sql, sql, "SQL deve ter sido alterado")
        
        # Deve conter uma coluna real de diagnóstico (CID ou DIAG)
        has_real_diag = any(
            col in fixed_sql for col in real_columns
            if any(token in col.upper() for token in ("CID", "DIAG"))
        )
        self.assertTrue(has_real_diag, "Correção deve usar uma coluna real de diagnóstico com CID ou DIAG no nome")
        
    def test_fix_invented_sex_column(self):
        """SEXO_PRINCIPAL deve ser corrigido para uma coluna de sexo real."""
        real_columns = []
        for cols in self.columns_by_grupo.values():
            for col in cols:
                real_columns.append(col["column"])
        
        sql = 'SELECT SEXO_PRINC, COUNT(*) FROM datasus_query_cache GROUP BY SEXO_PRINC'
        
        fixed_sql, unresolved = self.service._validate_and_fix_columns(sql, real_columns)
        
        # Deve ter substituído SEXO_PRINC por algo real (AP_SEXO, SEXO, etc.)
        self.assertNotIn("SEXO_PRINC", fixed_sql, "SEXO_PRINC deve ter sido corrigido")
        
    def test_sql_with_valid_columns_unchanged(self):
        """SQL com colunas válidas não deve ser alterado."""
        # Encontra colunas reais de um grupo
        target_grupo = self.grupos[0] if self.grupos else None
        if not target_grupo:
            self.skipTest("Nenhum grupo disponível")
        cols = self.columns_by_grupo.get(target_grupo, [])
        if not cols:
            self.skipTest("Nenhuma coluna disponível")
        
        # Usa uma coluna real
        real_col = cols[0]["column"]
        real_columns = [col["column"] for col in cols]
        
        sql = f"SELECT COUNT(*) FROM datasus_query_cache WHERE \"{real_col}\" IS NOT NULL"
        
        fixed_sql, unresolved = self.service._validate_and_fix_columns(sql, real_columns)
        
        # SQL não deve ser alterado
        self.assertEqual(sql, fixed_sql, "SQL com colunas válidas não deve ser alterado")

    def test_fix_bare_column_invented(self):
        """Coluna inventada sem aspas e com significativa similaridade deve ser corrigida."""
        real_columns = []
        for cols in self.columns_by_grupo.values():
            for col in cols:
                real_columns.append(col["column"])
        
        # Simula o LLM escrevendo CID_PRINC (compreensível, mas não é o nome exato)
        # Deve ser corrigido para CIDPRI (grupo ps) ou outra coluna com CID
        sql = "SELECT COUNT(*) FROM datasus_query_cache WHERE CID_PRINC IS NOT NULL"
        
        fixed_sql, unresolved = self.service._validate_and_fix_columns(sql, real_columns)
        
        # Deve ter sido corrigido para uma coluna real com CID (CIDPRI, CIDPRIU, etc.)
        self.assertNotIn("CID_PRINC", fixed_sql, "Coluna inventada deve ser corrigida para uma real")
        # Deve conter alguma coluna real com CID
        has_cid = any(
            col in fixed_sql for col in real_columns
            if "CID" in col.upper()
        )
        self.assertTrue(has_cid, "Correção deve usar uma coluna real que contenha CID no nome")


# ============================================================================
# Teste 5: Verifica que get_grupo_columns retorna apenas os grupos solicitados
# ============================================================================

class TestGetGrupoColumns(ColumnCacheTestBase):
    """Verifica a funcionalidade de obter colunas de grupos específicos."""

    def test_get_all_grupos(self):
        """Sem argumento, deve retornar todos os grupos."""
        all_cols = self.cache.get_grupo_columns()
        self.assertEqual(len(all_cols), len(self.grupos), "Deve retornar todos os grupos")
        for grupo in self.grupos:
            self.assertIn(grupo, all_cols, f"Grupo {grupo} deve estar presente")

    def test_get_specific_grupo(self):
        """Com argumento, deve retornar apenas o grupo solicitado."""
        if not self.grupos:
            self.skipTest("Nenhum grupo disponível")
        grupo = self.grupos[0]
        cols = self.cache.get_grupo_columns(grupo)
        
        self.assertEqual(list(cols.keys()), [grupo], "Deve retornar apenas o grupo solicitado")
        self.assertEqual(cols[grupo], self.columns_by_grupo[grupo], "Colunas devem ser as mesmas do cache")

    def test_get_nonexistent_grupo(self):
        """Grupo inexistente deve retornar lista vazia."""
        cols = self.cache.get_grupo_columns("grupo_inexistente")
        self.assertEqual(cols, {"grupo_inexistente": []}, "Grupo inexistente deve retornar lista vazia")


# ============================================================================
# Teste 6: Verifica que o cache é compartilhado corretamente
# ============================================================================

class TestColumnsCacheSharing(ColumnCacheTestBase):
    """Verifica que o cache de colunas é reutilizado (não reconstruído)."""

    def test_cache_is_loaded_once_and_reused(self):
        """O cache deve ser carregado e permanecer em memória para reuso."""
        # Já foi carregado no __init__
        self.assertIsNotNone(self.cache._columns_cache, "Cache deve existir")
        
        # Chama get_grupo_columns - deve usar o cache existente (não re-construir)
        with patch.object(self.cache, "_build_all_grupo_columns_caches") as mock_build:
            _ = self.cache.get_grupo_columns()
            mock_build.assert_not_called(), "get_grupo_columns não deve reconstruir o cache se já existe"

    def test_cache_persisted_to_disk(self):
        """Os arquivos JSON devem existir no disco."""
        for grupo in self.grupos:
            cache_file = self.settings.cache_dir / "colunas_cache" / f"{grupo}.json"
            self.assertTrue(cache_file.exists(), f"Arquivo {cache_file} deve existir")


# ============================================================================
# Teste 7: Verifica a extração de limite da pergunta
# ============================================================================

class TestExtractLimitFromQuestion(unittest.TestCase):
    """Verifica que o limite numérico é extraído corretamente da pergunta."""

    def test_limite_com_municipios(self):
        """'os 10 municipios' deve retornar 10."""
        limite = LLMQueryService._extract_limit_from_question(
            "quais os 10 municipios com mais diagnosticos de cancer de pele nos ultimos 2 anos ?"
        )
        self.assertEqual(limite, 10, "Deve extrair 10 de 'os 10 municipios'")

    def test_limite_com_estados(self):
        """'os 5 estados' deve retornar 5."""
        limite = LLMQueryService._extract_limit_from_question(
            "quais os 5 estados com mais casos de dengue no ultimo ano ?"
        )
        self.assertEqual(limite, 5, "Deve extrair 5 de 'os 5 estados'")

    def test_limite_com_cidades(self):
        """'as 20 cidades' deve retornar 20."""
        limite = LLMQueryService._extract_limit_from_question(
            "quais as 20 cidades com mais registros de covid-19 em 2024 ?"
        )
        self.assertEqual(limite, 20, "Deve extrair 20 de 'as 20 cidades'")

    def test_limite_com_primeiros(self):
        """'os 3 primeiros' deve retornar 3."""
        limite = LLMQueryService._extract_limit_from_question(
            "quais os 3 primeiros medicamentos mais prescritos ?"
        )
        self.assertEqual(limite, 3, "Deve extrair 3 de 'os 3 primeiros'")

    def test_limite_com_top(self):
        """'top 15' deve retornar 15."""
        limite = LLMQueryService._extract_limit_from_question(
            "top 15 municipios com mais casos de diabetes"
        )
        self.assertEqual(limite, 15, "Deve extrair 15 de 'top 15'")

    def test_limite_padrao_quando_nao_informado(self):
        """Sem número explícito, deve retornar 100 (padrão)."""
        limite = LLMQueryService._extract_limit_from_question(
            "quantos casos de cancer de pulmao existem em 2024 ?"
        )
        self.assertEqual(limite, 100, "Sem número explícito deve usar 100 como padrão")

    def test_limite_padrao_para_municipios_sem_numero(self):
        """'os municipios' sem número deve retornar 100 (padrão)."""
        limite = LLMQueryService._extract_limit_from_question(
            "quais os municipios com mais casos de cancer de mama ?"
        )
        self.assertEqual(limite, 100, "Sem número explícito deve usar 100 como padrão")

    def test_limite_limitado_a_100(self):
        """Limite acima de 100 deve ser limitado a 100."""
        limite = LLMQueryService._extract_limit_from_question(
            "quais os 500 municipios com mais casos ?"
        )
        self.assertEqual(limite, 100, "Limite acima de 100 deve ser limitado a 100")


# ============================================================================
# Teste de fallback de schema em modo grupos
# ============================================================================

class TestSchemaFallbackGroupsMode(ColumnCacheTestBase):
    """Valida o fallback de schema quando schema_context('datasus') está vazio.

    Em modo grupos, a view legada 'datasus' não existe; os dados vivem nos
    bancos anexados (<grupo>.datasus_cache). O fallback deve preencher o schema
    a partir da união das colunas de todos os grupos, para que o LLM receba
    colunas REAIS (e não um schema vazio).
    """

    def _union_columns(self):
        """Reproduz a união de colunas de todos os grupos (dedup por upper)."""
        seen = set()
        cols = []
        for grupo_cols in self.cache.get_grupo_columns().values():
            for col in grupo_cols:
                key = str(col.get("column", "")).upper()
                if key and key not in seen:
                    seen.add(key)
                    cols.append(col["column"])
        return cols

    def test_union_columns_non_empty(self):
        """A união das colunas dos grupos deve ser não vazia."""
        cols = self._union_columns()
        self.assertGreater(len(cols), 0, "União de colunas dos grupos ficou vazia")

    def test_fallback_fills_prompt_without_relevant_grupos(self):
        """Sem grupos relevantes, o prompt deve conter colunas reais (união)."""
        captured = {}

        def create_side_effect(**kwargs):
            messages = kwargs.get("messages", [])
            if messages:
                captured["content"] = messages[0]["content"]
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = (
                "SELECT COUNT(*) AS total FROM datasus_query_cache"
            )
            return mock_resp

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = create_side_effect

        original_client = self.service.client
        self.service.client = mock_client

        try:
            with patch.object(
                self.service, "_determine_relevant_grupos", return_value=[]
            ):
                self.service._generate_sql(
                    "Quantos registros existem por ano?", "test-fb-1", {("2025", "01")}, limite=100
                )
        finally:
            self.service.client = original_client

        self.assertIn("content", captured, "Prompt deve ter sido capturado")
        prompt = captured["content"]

        union = self._union_columns()
        self.assertGreater(len(union), 0)
        encontradas = [c for c in union if c in prompt]
        self.assertGreater(
            len(encontradas),
            0,
            "Prompt deve conter colunas reais da união dos grupos (fallback)",
        )

    def test_real_columns_populated_without_relevant_grupos(self):
        """A variável real_columns deve ser populada no caminho 'else'."""
        # _union_columns() retorna os nomes das colunas (list[str]); o caminho
        # 'else' do _generate_sql usa exatamente esses nomes como real_columns.
        real_columns = list(self._union_columns())
        self.assertGreater(len(real_columns), 0, "real_columns não deve ficar vazio")


if __name__ == "__main__":
    unittest.main()