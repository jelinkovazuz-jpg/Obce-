import bcrypt

heslo = input("Zadej heslo: ")

hash = bcrypt.hashpw(
    heslo.encode("utf-8"),
    bcrypt.gensalt()
)

print("\nHash hesla:\n")
print(hash.decode())