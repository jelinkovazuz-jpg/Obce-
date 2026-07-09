import requests

obec = "Pardubice"

url = "https://cs.wikipedia.org/w/api.php"

params = {
    "action": "query",
    "format": "json",
    "prop": "extlinks",
    "titles": obec,
    "ellimit": "max"
}

response = requests.get(url, params=params, timeout=20)
response.raise_for_status()

data = response.json()

pages = data["query"]["pages"]

for page in pages.values():

    if "extlinks" not in page:
        print("Nebyl nalezen žádný externí odkaz.")
        continue

    for link in page["extlinks"]:

        href = link["*"]

        if "pardubice.eu" in href:
            print("Oficiální web:", href)