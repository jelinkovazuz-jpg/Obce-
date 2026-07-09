print("START")
import duckdb
from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "obce.duckdb"

DATA_DIR.mkdir(exist_ok=True)

conn = duckdb.connect(DB_PATH)

conn.execute("""
CREATE TABLE IF NOT EXISTS obce (
    kod_obce INTEGER,
    nazev TEXT,
    okres TEXT,
    kraj TEXT,
    latitude DOUBLE,
    longitude DOUBLE
)
""")

print("Databáze byla vytvořena.")