from db.cache import _is_valid_group_name, _RESERVED_GROUP_NAMES
from config.settings import DATASUS_DATA_DIR
from pathlib import Path

print("datasus[1]:", _is_valid_group_name("datasus[1]"))
print("parquet:", _is_valid_group_name("parquet"))
print("sp:", _is_valid_group_name("sp"))
print("reserved:", _RESERVED_GROUP_NAMES)

cache_dir = Path(r"D:\datasus\cache")
print("\n*.duckdb in cache dir:")
for f in sorted(cache_dir.glob("*.duckdb")):
    print("  ", f.name, "-> valid:", _is_valid_group_name(f.stem), "reserved:", f.stem in _RESERVED_GROUP_NAMES)

print("\nDATASUS_DATA_DIR:", DATASUS_DATA_DIR)
if DATASUS_DATA_DIR.is_dir():
    for e in sorted(DATASUS_DATA_DIR.iterdir()):
        if e.is_dir():
            print("  dir:", e.name, "-> valid:", _is_valid_group_name(e.name))
