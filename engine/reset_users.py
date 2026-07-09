import duckdb

db = duckdb.connect("data/obce.duckdb")

print("Mažu starou tabulku users...")

db.execute("DROP TABLE IF EXISTS users")

print("Vytvářím novou tabulku...")

db.execute("""
CREATE TABLE users (
    username VARCHAR PRIMARY KEY,
    display_name VARCHAR,
    password_hash VARCHAR,
    role VARCHAR,
    active BOOLEAN
)
""")

print("✅ Hotovo.")