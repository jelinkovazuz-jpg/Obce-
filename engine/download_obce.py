import pandas as pd

url = "https://raw.githubusercontent.com/zelenda/czech-addresses/master/data/obce.csv"

df = pd.read_csv(url)

print(df.head())
print()
print(f"Načteno {len(df)} obcí")