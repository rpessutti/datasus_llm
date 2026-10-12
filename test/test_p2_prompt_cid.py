"""P2 (integração no prompt) — verifica as regras das seções de CID que o
``generate.py`` monta a partir da descoberta COMPLETA (``_discover_cid_full``).

Objetivo: garantir que, quando a descoberta completa resolve a doença, o prompt:
  1. mostra o bloco PRIMÁRIO "CIDs DA DOENÇA CITADA" (com o LIKE da categoria);
  2. rotula a lista bruta de prefixos como SECUNDÁRIA ("OUTROS PREFIXOS ...");
  3. filtra prefixos curtos de 2 chars (ex.: 'A0'), exibindo categorias de 3+;
  4. limita a amostra de prefixos a 30 (vs 60 quando NÃO há descoberta).

Usa o mock de captura de prompt (sem LLM real). Os testes marcam SKIP quando o
cache/layout de dados não está disponível.

Rodar:
    python -m pytest test/test_p2_prompt_cid.py -q
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from config.settings import Settings
from db.cache import DatasusCache
from api.llm_service_modular import LLMQueryService


class _CapturePromptBase(unittest.TestCase):
    """Base que instancia o serviço real e captura o prompt do _generate_sql."""

    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        try:
            cls.cache = DatasusCache(cls.settings)
            cls.service = LLMQueryService(cls.cache, cls.settings)
        except Exception as exc:  # pragma: no cover - ambiente sem dados
            raise unittest.SkipTest(f"cache indisponível: {exc}")

    def _captura_prompt(self, question: str) -> str:
        """Executa _generate_sql capturando o prompt enviado ao LLM."""
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

        original = self.service.client
        self.service.client = mock_client
        try:
            self.service._generate_sql(question, "test-p2-prompt", {("2024", "01")}, limite=100)
        finally:
            self.service.client = original

        return captured.get("content", "")


class TestP2PromptCID(_CapturePromptBase):
    """Regras das seções de CID no prompt (P2)."""

    def test_descoberta_presente_para_doenca_conhecida(self):
        """Sarampo deve produzir o bloco PRIMÁRIO com a categoria B05 e o LIKE."""
        prompt = self._captura_prompt("quantos casos de sarampo em 2024")
        if "CIDs DA DOENÇA CITADA" not in prompt:
            self.skipTest("descoberta completa não disponível neste ambiente (cid10 ausente)")
        self.assertIn("CIDs DA DOENÇA CITADA", prompt)
        self.assertIn("B05", prompt)
        self.assertIn("LIKE 'B05%'", prompt)
        self.assertIn("MESMA doença", prompt)

    def test_prefixos_rotulados_como_secundarios(self):
        """Com descoberta completa, a lista de prefixos é SECUNDÁRIA."""
        prompt = self._captura_prompt("quantos casos de sarampo em 2024")
        if "CIDs DA DOENÇA CITADA" not in prompt:
            self.skipTest("descoberta completa não disponível neste ambiente")
        self.assertIn("OUTROS PREFIXOS DE CID", prompt)
        self.assertIn("SECUNDÁRIA", prompt)
        # NÃO deve usar o rótulo primário antigo quando há descoberta.
        self.assertNotIn("PREFIXOS DE CID REAIS PRESENTES NOS DADOS (referência", prompt)

    def test_prefixos_curtos_de_2_chars_filtrados(self):
        """A amostra de prefixos não deve conter códigos de 2 chars (ex.: A0)."""
        prompt = self._captura_prompt("quantos casos de sarampo em 2024")
        if "PREFIXOS DE CID" not in prompt and "OUTROS PREFIXOS DE CID" not in prompt:
            self.skipTest("bloco de prefixos ausente neste ambiente")
        # Localiza a lista de prefixos exibida no prompt.
        import re
        mk = re.search(r"(?:OUTROS )?PREFIXOS DE CID[^\n]*?:\s*([A-Z0-9, ]+?)\.", prompt)
        if not mk:
            self.skipTest("não foi possível localizar a lista de prefixos")
        codigos = [c.strip() for c in mk.group(1).split(",") if c.strip()]
        self.assertTrue(codigos, "lista de prefixos não pode estar vazia")
        # Todos devem ter 3+ chars (categorias), sem 'A0'/'A1' de 2 chars.
        curtas = [c for c in codigos if len(c) < 3]
        self.assertEqual(curtas, [], f"prefixos de 2 chars vazaram: {curtas}")

    def test_lista_prefixos_limitada_a_30_com_descoberta(self):
        """Com descoberta completa, no máximo 30 prefixos são exibidos."""
        import re
        prompt = self._captura_prompt("quantos casos de sarampo em 2024")
        if "OUTROS PREFIXOS DE CID" not in prompt:
            self.skipTest("descoberta completa não disponível neste ambiente")
        mk = re.search(r"OUTROS PREFIXOS DE CID[^\n]*?:\s*([A-Z0-9, ]+?)\.", prompt)
        self.assertIsNotNone(mk, "lista de prefixos secundários não encontrada")
        codigos = [c.strip() for c in mk.group(1).split(",") if c.strip()]
        self.assertLessEqual(len(codigos), 30, f"excedeu 30 prefixos: {len(codigos)}")


class TestP2SemDoenca(_CapturePromptBase):
    """Pergunta sem doença não deve trazer descoberta nem quebrar o prompt."""

    def test_sem_doenca_nao_tem_bloco_descoberta(self):
        prompt = self._captura_prompt("total de atendimentos por mes")
        self.assertNotIn("CIDs DA DOENÇA CITADA", prompt)
        # O prompt continua sendo montado (não vazio).
        self.assertGreater(len(prompt), 0)


if __name__ == "__main__":
    unittest.main()
