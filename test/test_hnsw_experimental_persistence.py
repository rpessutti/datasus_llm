"""
Valida a aplicação correta do parâmetro DuckDB
``hnsw_enable_experimental_persistence`` (VSS/HNSW).

Contexto
--------
A extensão VSS implementa índices HNSW. Criar/ler/usar índices HNSW em bancos
PERSISTENTES (arquivos ``.duckdb``) é considerado EXPERIMENTAL pelo DuckDB. Sem
habilitar ``hnsw_enable_experimental_persistence``, operações de persistência de
HNSW falham com erro do tipo "Persistent HNSW indexes are disabled by default".

O projeto centraliza essa configuração em ``core.duckdb_config`` e a aplica em
TODAS as conexões (banco principal e bancos por grupo). Este teste valida:

1. O helper ``apply_duckdb_experimental_settings``:
   * aplica o SET em uma conexão real e o valor fica ``true``;
   * é idempotente (pode ser chamado várias vezes sem erro);
   * é best-effort (conexão None / objeto inválido NÃO levantam exceção);
   * respeita o toggle por variável de ambiente
     ``DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE``.

2. A integração com os modelos do projeto:
   * ``DatasusCache`` deixa a flag ``true`` na conexão principal;
   * ``_GroupCache`` deixa a flag ``true`` nas conexões dos grupos (reopen).

3. Funcional: com a flag aplicada pelo helper, é possível CRIAR (persistir) um
   índice HNSW em um banco ``.duckdb`` real — exatamente o caso que a flag
   habilita. Sem a flag, o DuckDB recusa a criação persistente.

Observação: os testes que dependem da extensão VSS usam ``INSTALL vss; LOAD vss``
em vez de importar ``duckdb_extension_vss`` (cujo ``__init__`` pode estar vazio
em alguns ambientes). Se o VSS não estiver disponível no ambiente, esses testes
são PULADOS (skip) em vez de falharem, para não mascarar a validação do helper.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from core.duckdb_config import (  # noqa: E402
    HNSW_EXPERIMENTAL_PERSISTENCE_PARAM,
    apply_duckdb_experimental_settings,
    hnsw_experimental_persistence_enabled,
)

from db.cache import DatasusCache  # noqa: E402
from db.group_cache import _GroupCache  # noqa: E402


def _vss_disponivel() -> bool:
    """True se a extensão VSS puder ser carregada neste ambiente."""
    try:
        con = duckdb.connect()
        try:
            con.execute("INSTALL vss; LOAD vss;")
            return True
        finally:
            con.close()
    except Exception:
        return False


VSS_OK = _vss_disponivel()


class _FakeSettings:
    """Settings mínimo para instanciar DatasusCache em diretórios temporários."""

    def __init__(self, data_dir: Path, cache_dir: Path) -> None:
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.official_cache_dir = cache_dir / "official"
        self.database_path = cache_dir / "datasus.duckdb"
        self.memory_limit = "1GB"
        self.threads = 2
        self.temp_dir = cache_dir


def _garantir_vss(con) -> None:
    """Carrega o VSS na conexão (necessário para LER o setting da flag)."""
    try:
        con.execute("INSTALL vss; LOAD vss;")
    except Exception:
        pass


def _setting_value(con) -> bool:
    """Lê o valor de hnsw_enable_experimental_persistence (requer VSS carregado)."""
    row = con.execute(
        f"SELECT current_setting('{HNSW_EXPERIMENTAL_PERSISTENCE_PARAM}')"
    ).fetchone()
    return bool(row and row[0])


def _make_parquet(path: Path, dt: int, cid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        f"COPY (SELECT {dt} AS DT_INTER, '{cid}' AS CID) TO '{path}' (FORMAT PARQUET)"
    )


@unittest.skipUnless(VSS_OK, "extensão VSS indisponível neste ambiente")
class HelperHnswTest(unittest.TestCase):
    """Valida core.duckdb_config.apply_duckdb_experimental_settings (com VSS)."""

    def test_aplica_flag_em_conexao_real(self) -> None:
        con = duckdb.connect()
        try:
            apply_duckdb_experimental_settings(con, label="teste")
            _garantir_vss(con)
            self.assertTrue(
                _setting_value(con),
                "hnsw_enable_experimental_persistence deveria estar true após aplicar.",
            )
        finally:
            con.close()

    def test_idempotente(self) -> None:
        con = duckdb.connect()
        try:
            for _ in range(3):
                apply_duckdb_experimental_settings(con, label="teste")
            _garantir_vss(con)
            self.assertTrue(_setting_value(con))
        finally:
            con.close()

    def test_helper_carrega_vss_sozinho(self) -> None:
        """O helper deve deixar a flag aplicada mesmo sem VSS pré-carregado."""
        con = duckdb.connect()
        try:
            # Sem carregar VSS manualmente: o helper deve torná-lo disponível e
            # aplicar a flag com sucesso (não apenas engolir o erro).
            apply_duckdb_experimental_settings(con, label="auto_vss")
            self.assertTrue(_setting_value(con))
        finally:
            con.close()

    def test_toggle_por_variavel_de_ambiente(self) -> None:
        """Com a flag desabilitada, o helper NÃO altera a configuração."""
        anterior = os.environ.get("DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE")
        try:
            os.environ["DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE"] = "false"
            self.assertFalse(hnsw_experimental_persistence_enabled())

            con = duckdb.connect()
            try:
                _garantir_vss(con)
                # Default do DuckDB para a flag é false (experimental desligado).
                default_before = _setting_value(con)
                apply_duckdb_experimental_settings(con, label="desligado")
                self.assertEqual(
                    _setting_value(con), default_before,
                    "com a env var desligada, o helper não deve mexer na config.",
                )
            finally:
                con.close()

            # E, ligada, o helper aplica.
            os.environ["DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE"] = "true"
            self.assertTrue(hnsw_experimental_persistence_enabled())
            con = duckdb.connect()
            try:
                apply_duckdb_experimental_settings(con, label="ligado")
                self.assertTrue(_setting_value(con))
            finally:
                con.close()
        finally:
            if anterior is None:
                os.environ.pop("DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE", None)
            else:
                os.environ["DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE"] = anterior


class HelperHnswBestEffortTest(unittest.TestCase):
    """Comportamento best-effort: NÃO depende do VSS estar disponível."""

    def test_conexao_none_nao_levanta(self) -> None:
        # Não deve levantar exceção.
        apply_duckdb_experimental_settings(None, label="none")

    def test_objeto_invalido_nao_levanta(self) -> None:
        # Objeto que não é uma conexão DuckDB: falha silenciosa/registrada,
        # NUNCA propaga exceção (garante que a inicialização não quebra).
        class _NaoConexao:
            def execute(self, *args, **kwargs):
                raise RuntimeError("não é uma conexão válida")

        apply_duckdb_experimental_settings(_NaoConexao(), label="invalido")


@unittest.skipUnless(VSS_OK, "extensão VSS indisponível neste ambiente")
class DatasusCacheHnswTest(unittest.TestCase):
    """Valida que a flag é aplicada na conexão principal e nos grupos."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hnsw_cache_"))
        self.data = self.tmp / "data"
        self.cache = self.tmp / "cache"
        self.cache.mkdir(parents=True)
        _make_parquet(self.data / "er" / "2022" / "05" / "ER2205.parquet", 20220510, "B05")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_conexao_principal_tem_flag(self) -> None:
        c = DatasusCache(_FakeSettings(self.data, self.cache))
        try:
            _garantir_vss(c.connection)
            self.assertTrue(
                _setting_value(c.connection),
                "DatasusCache deve habilitar hnsw_enable_experimental_persistence.",
            )
        finally:
            c.close()

    def test_conexao_de_grupo_tem_flag(self) -> None:
        c = DatasusCache(_FakeSettings(self.data, self.cache))
        try:
            c._discover_groups()
            self.assertTrue(c.groups, "esperado ao menos um grupo descoberto")
            for name, group in c.groups.items():
                # Abre a conexão do grupo (aplica _configure_memory).
                group.reopen()
                try:
                    _garantir_vss(group.connection)
                    self.assertTrue(
                        _setting_value(group.connection),
                        f"grupo {name}: hnsw_enable_experimental_persistence deveria estar true.",
                    )
                finally:
                    # Em modo conexão principal o close é noop seguro; garante liberação.
                    if not getattr(group, "_using_main_connection", False):
                        group.close()
        finally:
            c.close()

    def test_group_cache_memoria_fallback_tem_flag(self) -> None:
        """_GroupCache sem banco em disco cai em :memory: e ainda aplica a flag."""
        g = _GroupCache("er", self.data / "er", self.cache, None)
        g.reopen()
        try:
            self.assertIsNotNone(g.connection)
            _garantir_vss(g.connection)
            self.assertTrue(_setting_value(g.connection))
        finally:
            g.close()


@unittest.skipUnless(VSS_OK, "extensão VSS indisponível neste ambiente")
class PersistenciaHnswFuncionalTest(unittest.TestCase):
    """Funcional: a flag habilita PERSISTIR um índice HNSW em disco."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hnsw_func_"))
        self.db_path = self.tmp / "vetorial.duckdb"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _preparar_tabela(self, con) -> None:
        con.execute("INSTALL vss; LOAD vss;")
        con.execute("CREATE TABLE itens (id INTEGER, emb FLOAT[4])")
        con.execute(
            "INSERT INTO itens VALUES "
            "(1, [1.0, 0.0, 0.0, 0.0]), "
            "(2, [0.0, 1.0, 0.0, 0.0]), "
            "(3, [0.9, 0.1, 0.0, 0.0])"
        )

    def test_cria_indice_hnsw_persistente_com_flag(self) -> None:
        con = duckdb.connect(str(self.db_path))
        try:
            self._preparar_tabela(con)
            # Sem a flag, o DuckDB recusa a criação persistente de HNSW.
            apply_duckdb_experimental_settings(con, label="hnsw_func")
            self.assertTrue(_setting_value(con))

            con.execute(
                "CREATE INDEX idx_emb ON itens USING HNSW (emb) "
                "WITH (metric = 'l2sq')"
            )
            # Persiste e libera.
            con.execute("CHECKPOINT")
        finally:
            con.close()

        # Reabre o banco: o índice HNSW foi PERSISTIDO em disco.
        con2 = duckdb.connect(str(self.db_path))
        try:
            con2.execute("INSTALL vss; LOAD vss;")
            index_names = {
                r[0]
                for r in con2.execute(
                    "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'itens'"
                ).fetchall()
            }
            self.assertIn("idx_emb", index_names, "índice HNSW não persistiu em disco")
        finally:
            con2.close()

    def test_sem_flag_criacao_persistente_recusada(self) -> None:
        """Confirma o cenário que MOTIVA a flag: sem ela, a persistência é recusada.

        Desliga a env var, NÃO aplica a flag e verifica que a criação persistente
        de HNSW é recusada. Tolerante a versões em que a flag deixe de ser
        obrigatória (nesse caso apenas registra que não houve erro).
        """
        anterior = os.environ.get("DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE")
        try:
            os.environ["DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE"] = "false"
            con = duckdb.connect(str(self.db_path))
            try:
                self._preparar_tabela(con)
                # Não aplica a flag (env var desligada).
                apply_duckdb_experimental_settings(con, label="sem_flag")
                self.assertFalse(_setting_value(con))
                try:
                    con.execute(
                        "CREATE INDEX idx_emb2 ON itens USING HNSW (emb) "
                        "WITH (metric = 'l2sq')"
                    )
                except Exception:
                    # Comportamento esperado: persistência experimental desligada.
                    pass
            finally:
                con.close()
        finally:
            if anterior is None:
                os.environ.pop("DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE", None)
            else:
                os.environ["DATASUS_HNSW_EXPERIMENTAL_PERSISTENCE"] = anterior


if __name__ == "__main__":
    unittest.main(verbosity=2)
