import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path('.').resolve()))
os.environ.setdefault('DATASUS_TEMP_CACHE_DIR', tempfile.mkdtemp())
os.environ['DATASUS_MEMORY_LIMIT'] = '100MB'
os.environ['DATASUS_THREADS'] = '1'
os.environ['LLM_ENABLED'] = 'false'

import duckdb
import pandas as pd
from db.group_cache import _GroupCache

# Configura ambiente temporário
tmp_dir = Path('/tmp/att_test2')
if tmp_dir.exists():
    shutil.rmtree(tmp_dir)

# Cria dois grupos
grupo1_dir = tmp_dir / 'grupo1'
grupo2_dir = tmp_dir / 'grupo2'
grupo1_dir.mkdir(parents=True)
grupo2_dir.mkdir(parents=True)
cache_dir = tmp_dir / 'cache'
cache_dir.mkdir(parents=True)

# Cria parquets
df1 = pd.DataFrame({'AP_MVM': ['202201'], 'AP_SEXO': ['M']})
df1.to_parquet(grupo1_dir / 'PA2201.parquet', index=False)
df2 = pd.DataFrame({'AP_MVM': ['202301'], 'AP_SEXO': ['F']})
df2.to_parquet(grupo2_dir / 'SP2301.parquet', index=False)

# Cria e ingere os grupos
groups = {}
for i, (nome, dir_) in enumerate([('grupo1', grupo1_dir), ('grupo2', grupo2_dir)]):
    grupo = _GroupCache(nome, dir_, cache_dir)
    grupo.reopen()
    grupo.ingest()
    print(f'{nome} após ingest, connection é None? {grupo.connection is None}')
    grupo.close()
    print(f'{nome} após close, connection é None? {grupo.connection is None}')
    print(f'{nome} db_path existe? {grupo.db_path.exists()}')
    groups[nome] = grupo

# Criar banco principal
conn = duckdb.connect(str(cache_dir / 'main.duckdb'))

# Tenta anexar ambos
try:
    for nome, grupo in groups.items():
        print(f'\nTentando ATTACH {nome}...')
        try:
            conn.execute(f"ATTACH '{grupo.db_path}' AS {nome} (READ_ONLY)")
            print(f"  ATTACH {nome} OK")
        except Exception as e:
            print(f"  ERRO ATTACH {nome}: {type(e).__name__}: {e}")
    
    # Verifica schemas
    schemas = conn.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name").fetchall()
    print('\nSchemas encontrados:')
    for s in schemas:
        print(f'  {s[0]}')
    
    # Testa consulta
    result = conn.execute('SELECT count(*) FROM grupo1.datasus_cache').fetchone()
    print('\nConsulta grupo1 OK:', result)
    result = conn.execute('SELECT count(*) FROM grupo2.datasus_cache').fetchone()
    print('Consulta grupo2 OK:', result)
    conn.close()
    print('\nTUDO OK')
except Exception as e:
    print('\nERRO FINAL:', e)
    conn.close()