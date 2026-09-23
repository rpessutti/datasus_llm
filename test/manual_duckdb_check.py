import duckdb
import logging

# Configurar o logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

def test_open_duckdb(db_path):
    try:
        connection = duckdb.connect(str(db_path))
        connection.execute("SELECT 1")
        connection.close()
        logging.info("Conexão ao banco de dados %s bem-sucedida", db_path)
    except duckdb.DuckDBError as e:
        logging.error("Falha ao abrir o banco de dados %s: %s", db_path, e)

if __name__ == "__main__":
    db_path = r"D:\datasus\cache\ab.duckdb"  # Substitua pelo caminho do seu banco de dados
    test_open_duckdb(db_path)