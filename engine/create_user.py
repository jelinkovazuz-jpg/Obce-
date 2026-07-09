import bcrypt
import duckdb
import getpass

db = duckdb.connect("data/obce.duckdb")

print("=== Přidání nového uživatele ===\n")

username = input("Přihlašovací jméno: ")
display_name = input("Zobrazované jméno: ")
role = input("Role (admin/user): ").lower()

password = getpass.getpass("Heslo: ")

password_hash = bcrypt.hashpw(
    password.encode("utf-8"),
    bcrypt.gensalt()
).decode()

db.execute("""
INSERT INTO users
(username, display_name, password_hash, role, active)
VALUES (?, ?, ?, ?, TRUE)
""", [
    username,
    display_name,
    password_hash,
    role
])

print("\n✅ Uživatel byl vytvořen.")