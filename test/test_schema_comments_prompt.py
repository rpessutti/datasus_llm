"""Testes da FASE P0-A: comentários de coluna no dicionário/prompt.

Garante que:
- com um ``cache_dir`` contendo ``layout_csv/<grupo>.csv``, o ``ColumnDictionary``
  carrega o comentário por coluna (descrição/exemplos);
- ``labeled_columns_sql_hint(with_comments=True)`` anexa o comentário truncado;
- SEM ``cache_dir`` (ou sem layouts), o comportamento é IDÊNTICO ao anterior
  (nenhum comentário) — compatibilidade total;
- ``generate.py`` propaga ``with_comments``/``max_comment_chars`` no prompt.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.column_dictionary import ColumnDictionary, ColumnDictionaryStore  # noqa: E402


class _RecordingDict:
    """Dicionário FAKE que registra como ``labeled_columns_sql_hint`` foi chamado.

    Usado para verificar que ``generate.py`` propaga ``with_comments`` e
    ``max_comment_chars`` (P0-A) sem precisar de DuckDB/layouts reais.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.by_group = {"pa": [{"column": "AP_CIDPRI", "type": "VARCHAR"}]}

    @property
    def type_by_upper(self):
        return {}

    def columns_for_group(self, grupo):
        return [c["column"] for c in self.by_group.get(grupo, [])]

    def groups_for_column(self, column):
        return ["pa"]

    @property
    def unique_columns(self):
        return ["AP_CIDPRI"]

    @property
    def concept_map(self):
        return {}

    @property
    def diagnosis_columns(self):
        return ["AP_CIDPRI"]

    def labeled_columns_sql_hint(self, grupos=None, limit_per_group=None, **kwargs):
        self.calls.append(
            {"grupos": grupos, "limit_per_group": limit_per_group, **kwargs}
        )
        return '"AP_CIDPRI" [pa] — CID principal da internação'


def _write_layout(cache_dir: Path, grupo: str, rows: list[tuple[str, str, str]]) -> None:
    layout_dir = cache_dir / "layout_csv"
    layout_dir.mkdir(parents=True, exist_ok=True)
    path = layout_dir / f"{grupo}.csv"
    lines = ["campo,descricao,exemplos"]
    for campo, desc, ex in rows:
        lines.append(f'{campo},"{desc}","{ex}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSchemaComments(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name)
        _write_layout(
            self.cache_dir,
            "pa",
            [
                ("AP_CIDPRI", "CID principal da internação", "A90"),
                ("AP_SEXO", "Sexo do paciente", "M;F"),
            ],
        )
        self.grupo_columns = {
            "pa": [
                {"column": "AP_CIDPRI", "type": "VARCHAR"},
                {"column": "AP_SEXO", "type": "VARCHAR"},
                {"column": "AP_SEM_LAYOUT", "type": "VARCHAR"},
            ]
        }

    def test_comment_loaded_from_layout(self) -> None:
        cd = ColumnDictionary.from_grupo_columns(self.grupo_columns, cache_dir=str(self.cache_dir))
        self.assertEqual(cd.comment_for_column("AP_CIDPRI"), "CID principal da internação Exemplos: A90")
        self.assertIn("Sexo do paciente", cd.comment_for_column("AP_SEXO"))
        # Coluna sem entrada no layout -> sem comentário.
        self.assertEqual(cd.comment_for_column("AP_SEM_LAYOUT"), "")

    def test_labeled_hint_includes_comment(self) -> None:
        cd = ColumnDictionary.from_grupo_columns(self.grupo_columns, cache_dir=str(self.cache_dir))
        hint = cd.labeled_columns_sql_hint(with_comments=True)
        self.assertIn('"AP_CIDPRI" [pa] — ', hint)
        self.assertIn("CID principal", hint)

    def test_labeled_hint_truncates_comment(self) -> None:
        cd = ColumnDictionary.from_grupo_columns(self.grupo_columns, cache_dir=str(self.cache_dir))
        hint = cd.labeled_columns_sql_hint(with_comments=True, max_comment_chars=15)
        # Deve conter reticências (truncado).
        self.assertIn("…", hint)

    def test_without_cache_dir_is_backward_compatible(self) -> None:
        cd = ColumnDictionary.from_grupo_columns(self.grupo_columns)  # sem cache_dir
        self.assertEqual(cd.comment_for_column("AP_CIDPRI"), "")
        # Sem comentários -> rótulo igual ao formato antigo.
        self.assertEqual(
            cd.labeled_columns_sql_hint(with_comments=True),
            '"AP_CIDPRI" [pa], "AP_SEXO" [pa], "AP_SEM_LAYOUT" [pa]',
        )

    def test_store_accepts_cache_dir(self) -> None:
        store = ColumnDictionaryStore()
        cd = store.get(self.grupo_columns, cache_dir=str(self.cache_dir))
        self.assertIn("CID principal", cd.comment_for_column("AP_CIDPRI"))

    def test_store_cache_key_distinguishes_cache_dir(self) -> None:
        """P0-A: um dicionário COM comentários não é servido para pedido SEM.

        A chave do store inclui o cache_dir; caso contrário, chamar primeiro sem
        cache_dir envenenaria o cache e a chamada COM cache_dir perderia os
        comentários (regressão silenciosa na injeção no prompt).
        """
        store = ColumnDictionaryStore()
        sem = store.get(self.grupo_columns)  # sem cache_dir -> sem comentários
        self.assertEqual(sem.comment_for_column("AP_CIDPRI"), "")
        com = store.get(self.grupo_columns, cache_dir=str(self.cache_dir))
        self.assertIn("CID principal", com.comment_for_column("AP_CIDPRI"))


class TestGenerateSqlPassesComments(unittest.TestCase):
    """P0-A (generate.py): ``_generate_sql`` propaga ``with_comments``.

    Não depende de DuckDB: usa um serviço mínimo (``__new__``) com cache/
    dicionário FAKE e um cliente LLM mockado, capturando o prompt final.
    """

    def _build_service(self, schema_comments_enabled: bool, max_chars: int = 90):
        from unittest.mock import MagicMock
        from api.llm_service_modular import LLMQueryService

        svc = LLMQueryService.__new__(LLMQueryService)
        recording = _RecordingDict()

        fake_cache = MagicMock()
        fake_cache.get_column_dictionary.return_value = recording
        fake_cache._summary_tables.return_value = []
        fake_cache.question_wants_monthly_grouping.return_value = False

        settings = MagicMock()
        settings.schema_comments_enabled = schema_comments_enabled
        settings.schema_comment_max_chars = max_chars
        settings.llm_sql_temperature = 0.0
        settings.llm_sql_max_tokens = 16
        settings.llm_timeout = 5
        settings.llm_sql_timeout = 5

        captured = {}

        def _create(**kwargs):
            messages = kwargs.get("messages", [])
            if messages:
                captured["prompt"] = messages[0]["content"]
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = (
                "SELECT COUNT(*) AS total FROM datasus_query_cache"
            )
            return resp

        client = MagicMock()
        client.chat.completions.create.side_effect = _create

        svc.cache = fake_cache
        svc.settings = settings
        svc.client = client
        svc.model = "test-model"
        svc._first_attempt_ok = True
        return svc, recording, captured

    def test_generate_sql_enables_comments_when_setting_on(self):
        from unittest.mock import patch

        svc, recording, captured = self._build_service(schema_comments_enabled=True, max_chars=42)
        with patch.object(svc, "_determine_relevant_grupos", return_value=["pa"]):
            svc._generate_sql("Quantos casos em 2025?", "req-1", {("2025", "01")}, limite=100)
        self.assertTrue(recording.calls, "labeled_columns_sql_hint deve ter sido chamado")
        call = recording.calls[0]
        self.assertTrue(call.get("with_comments"), "with_comments deve ser True quando habilitado")
        self.assertEqual(call.get("max_comment_chars"), 42)
        # O comentário deve aparecer no prompt final.
        self.assertIn("CID principal", captured.get("prompt", ""))

    def test_generate_sql_disables_comments_when_setting_off(self):
        from unittest.mock import patch

        svc, recording, captured = self._build_service(schema_comments_enabled=False)
        with patch.object(svc, "_determine_relevant_grupos", return_value=["pa"]):
            svc._generate_sql("Quantos casos em 2025?", "req-2", {("2025", "01")}, limite=100)
        self.assertTrue(recording.calls, "labeled_columns_sql_hint deve ter sido chamado")
        self.assertFalse(recording.calls[0].get("with_comments"), "with_comments deve ser False quando desabilitado")


class TestCachePassesCacheDirToStore(unittest.TestCase):
    """P0-A (db/cache.py): ``get_column_dictionary`` passa ``cache_dir`` ao store.

    Verifica o elo ``DatasusCache -> ColumnDictionaryStore.get(cache_dir=...)``
    sem abrir DuckDB (o cache é instanciado via ``__new__``).
    """

    def test_get_column_dictionary_passes_cache_dir(self):
        from unittest.mock import MagicMock
        from db.cache import DatasusCache

        cache = DatasusCache.__new__(DatasusCache)
        cache._columns_cache = {"pa": [{"column": "AP_CIDPRI", "type": "VARCHAR"}]}
        cache._column_dictionary = None
        settings = MagicMock()
        settings.cache_dir = Path(tempfile.gettempdir()) / "datasus_cache_dir_test"
        cache.settings = settings

        captured = {}

        class _StoreSpy:
            def get(self, grupo_columns, grupos=None, cache_dir=None):
                captured["cache_dir"] = cache_dir
                # Reproduz a API mínima usada por get_column_dictionary.
                from db.column_dictionary import ColumnDictionary
                return ColumnDictionary.from_grupo_columns(
                    grupo_columns, grupos, cache_dir=cache_dir
                )

        cache._column_dict_store = _StoreSpy()
        result = cache.get_column_dictionary()

        self.assertIsNotNone(result)
        self.assertEqual(
            captured.get("cache_dir"), str(settings.cache_dir),
            "get_column_dictionary deve repassar settings.cache_dir ao store (P0-A)",
        )


class TestPromptCharsTelemetry(unittest.TestCase):
    """P0-A (telemetria): mede o impacto (chars) dos comentários no rótulo.

    Usa os dados REAIS quando disponíveis (``settings.cache_dir`` com
    ``layout_csv`` + ``colunas_cache``). Sem eles, o teste é SKIP (não falha):
    é telemetria, não uma invariante de código.

    Garante 3 coisas:
      1. o comentário AUMENTA o tamanho do hint quando há cobertura de layout;
      2. grupos SEM layout ficam IDÊNTICOS (retrocompatibilidade);
      3. o tamanho por grupo respeita o teto prático do prompt (alerta se passar).
    """

    # Teto "de sanidade" por grupo (25 colunas). Não é regra do produto; é um
    # alerta: se um grupo ultrapassar isto, o prompt pode inflar demais.
    MAX_CHARS_PER_GROUP = 4000

    def _real_cache_dir(self) -> Path | None:
        try:
            from config.settings import setting
        except Exception:
            return None
        cache_dir = Path(setting.cache_dir)
        if not (cache_dir / "layout_csv").is_dir():
            return None
        if not (cache_dir / "colunas_cache").is_dir():
            return None
        return cache_dir

    def test_real_data_chars_and_coverage(self):
        import json

        cache_dir = self._real_cache_dir()
        if cache_dir is None:
            self.skipTest("cache_dir real sem layout_csv/colunas_cache; telemetria ignorada")

        reserved = {"datasus", "datasus_fallback", "parquet", "cache", "tmp"}
        grupos: dict[str, list[dict[str, str]]] = {}
        for f in sorted((cache_dir / "colunas_cache").glob("*.json")):
            grupo = f.stem
            if grupo in reserved or "[" in grupo or "]" in grupo or not grupo.isidentifier():
                continue
            try:
                payload = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            cols = payload.get("columns") if isinstance(payload, dict) else payload
            if cols:
                grupos[grupo] = cols

        if not grupos:
            self.skipTest("sem caches de colunas válidos; telemetria ignorada")

        total_sem = total_com = 0
        colunas_com_cobertura = colunas_total = 0
        sem_layout = []
        for grupo, cols in grupos.items():
            cd = ColumnDictionary.from_grupo_columns({grupo: cols}, cache_dir=str(cache_dir))
            sem = cd.labeled_columns_sql_hint([grupo], limit_per_group=25)
            com = cd.labeled_columns_sql_hint(
                [grupo], limit_per_group=25,
                with_comments=True, max_comment_chars=90,
            )
            total_sem += len(sem)
            total_com += len(com)
            limitadas = cd.columns_for_group(grupo)[:25]
            colunas_total += len(limitadas)
            n_com = sum(1 for c in limitadas if cd.comment_for_column(c))
            colunas_com_cobertura += n_com
            if n_com == 0:
                sem_layout.append(grupo)
                # Retrocompatibilidade: grupo sem layout => hint IDÊNTICO.
                self.assertEqual(sem, com, f"grupo {grupo} sem layout deveria ter hint idêntico")
            # Sanidade de tamanho por grupo.
            self.assertLess(
                len(com), self.MAX_CHARS_PER_GROUP,
                f"grupo {grupo}: hint com comentários ficou grande demais "
                f"({len(com)} chars > {self.MAX_CHARS_PER_GROUP})",
            )

        # Log (aparece com pytest -s) — útil como telemetria.
        delta = total_com - total_sem
        pct = (delta / total_sem * 100) if total_sem else 0.0
        print(
            f"\n[P0-A telemetria] cols com comentário="
            f"{colunas_com_cobertura}/{colunas_total} "
            f"| chars sem={total_sem} com={total_com} delta={delta:+d} ({pct:+.1f}%) "
            f"| grupos sem layout={sem_layout}"
        )
        # Se houve cobertura de layout, os comentários DEVEM aumentar o hint.
        if colunas_com_cobertura > 0:
            self.assertGreater(total_com, total_sem)


if __name__ == "__main__":
    unittest.main()
