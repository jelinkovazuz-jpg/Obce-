import pandas as pd
import duckdb

# Načtení CSV
df = pd.read_csv("data/UI_OBEC.csv", sep=";")

# Připojení k databázi
conn = duckdb.connect("data/obce.duckdb")

# Vymazání starých dat
conn.execute("DELETE FROM obce")

# Vložení obcí
for _, row in df.iterrows():
    conn.execute(
        """
        INSERT INTO obce
        (kod_obce, nazev, okres, kraj, latitude, longitude)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(row["KOD"]),
            row["NAZEV"],
            None,
            None,
            None,
            None,
        ),
    )

print(f"Naimportováno {len(df)} obcí.")