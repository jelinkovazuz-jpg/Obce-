import duckdb

db = duckdb.connect("data/obce.duckdb")

print(
    db.execute("""
    SELECT username,
           display_name,
           role,
           active
    FROM users
    """).fetchdf()
)