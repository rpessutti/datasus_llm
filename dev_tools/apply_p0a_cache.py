"""P0-A (passo 3): injeta ``cache_dir`` nas chamadas de ``ColumnDictionaryStore.get``
em ``db/cache.py``, para que os comentários de coluna (descrição/exemplos dos
layouts oficiais) sejam carregados no dicionário consolidado usado pelo prompt.

Usa ``dev_tools.safe_edit.replace_in_file``: faz backup, aplica as substituições,
grava com escrita ATÔMICA + fsync e — crucialmente — **compila** o ``.py``; se a
compilação falhar, RESTAURA automaticamente do backup.

Rode:  python dev_tools/apply_p0a_cache.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dev_tools.safe_edit import replace_in_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

BACKUP_DIR = ROOT / "_backups" / "plano_prompt_cid_p0a"
TARGET = ROOT / "db" / "cache.py"


REPLACEMENTS = [
    # 1) __init__ (inicialização do dicionário)
    (
        """        self._column_dict_store = ColumnDictionaryStore()
        self._column_dictionary = None
        try:
            self._column_dictionary = self._column_dict_store.get(self._columns_cache)
            persist_to_duckdb(self.connection, self._column_dictionary)""",
        """        self._column_dict_store = ColumnDictionaryStore()
        self._column_dictionary = None
        try:
            # P0-A: injeta os comentários de coluna (descrição/exemplos dos
            # layouts oficiais) no dicionário, para o prompt do LLM.
            self._column_dictionary = self._column_dict_store.get(
                self._columns_cache, cache_dir=str(self.settings.cache_dir)
            )
            persist_to_duckdb(self.connection, self._column_dictionary)""",
    ),
    # 2) get_column_dictionary (store + fallback)
    (
        """        try:
            dictionary = self._column_dict_store.get(self._columns_cache)
        except Exception as e:
            LOGGER.warning("get_column_dictionary: falha ao obter do store (%s); reconstruindo.", e)
            dictionary = ColumnDictionaryStore().get(self._columns_cache)""",
        """        try:
            # P0-A: comentários de coluna dos layouts (best-effort).
            dictionary = self._column_dict_store.get(
                self._columns_cache, cache_dir=str(self.settings.cache_dir)
            )
        except Exception as e:
            LOGGER.warning("get_column_dictionary: falha ao obter do store (%s); reconstruindo.", e)
            dictionary = ColumnDictionaryStore().get(
                self._columns_cache, cache_dir=str(self.settings.cache_dir)
            )""",
    ),
    # 3) refresh_column_dictionary
    (
        """            self._column_dict_store.invalidate()
            dictionary = self._column_dict_store.get(self._columns_cache)
            self._column_dictionary = dictionary""",
        """            self._column_dict_store.invalidate()
            dictionary = self._column_dict_store.get(
                self._columns_cache, cache_dir=str(self.settings.cache_dir)
            )
            self._column_dictionary = dictionary""",
    ),
]

EXPECT = [
    "cache_dir=str(self.settings.cache_dir)",
    "_column_dict_store.get(\n                self._columns_cache, cache_dir=",
]


def main() -> int:
    res = replace_in_file(TARGET, REPLACEMENTS, expect=EXPECT, backup_dir=BACKUP_DIR)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res["ok"]:
        print("P0-A cache: FALHOU (arquivo restaurado se necessário)", file=sys.stderr)
        return 1
    # Verificação extra: ausência de chamadas antigas SEM cache_dir.
    text = TARGET.read_text(encoding="utf-8")
    problemas = []
    if "self._column_dict_store.get(self._columns_cache)" in text:
        problemas.append("ainda há _column_dict_store.get(self._columns_cache) sem cache_dir")
    if "ColumnDictionaryStore().get(self._columns_cache)" in text:
        problemas.append("ainda há ColumnDictionaryStore().get(self._columns_cache) sem cache_dir")
    if problemas:
        print("PROBLEMAS:", problemas, file=sys.stderr)
        return 1
    print("P0-A cache OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
