import duckdb
import bcrypt
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB = str(BASE_DIR / "data" / "obce.duckdb")

conn = duckdb.connect(DB)

username = input("Uživatelské jméno: ").strip()
password = input("Nové heslo: ").strip()

password_hash = bcrypt.hashpw(
    password.encode("utf-8"),
    bcrypt.gensalt()
).decode("utf-8")

updated = conn.execute("""
UPDATE users
SET password_hash = ?
WHERE username = ?
""", [password_hash, username])

if updated.rowcount == 0:
    print(f"❌ Uživatel '{username}' neexistuje.")
else:
    print(f"✅ Heslo uživatele '{username}' bylo změněno.")

conn.close()