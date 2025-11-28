import requests
from bs4 import BeautifulSoup

# URL dell'evento di test
detail_url = "https://www.salto-youth.net/tools/european-training-calendar/training/you-are-storyteller.14165/"

print(f"Testing URL: {detail_url}\n")
print("=" * 80)

resp = requests.get(detail_url)
soup = BeautifulSoup(resp.text, "html.parser")

# 1. Cerca la sezione "Training overview"
print("\n### TEST 1: TRAINING OVERVIEW ###\n")
h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
if h3_overview:
    print("✓ TRAINING OVERVIEW TROVATO")
    print(f"Tag: {h3_overview.name}")
    parts = []
    for sib in h3_overview.find_next_siblings():
        if sib.name and sib.name.startswith("h"):
            break
        parts.append(sib.get_text("\n", strip=True))
    training_overview = "\n".join(parts).strip()
    print("\n--- TESTO COMPLETO ---")
    print(training_overview)
    print("\n--- RIGHE SEPARATE ---")
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        print(f"  [{i}]: {line}")
else:
    print("✗ TRAINING OVERVIEW NON TROVATO")

print("\n" + "=" * 80)

# 2. Cerca la sezione "Available downloads"
print("\n### TEST 2: AVAILABLE DOWNLOADS ###\n")
downloads_section = soup.find(lambda tag: "Available downloads" in tag.get_text())
if downloads_section:
    print("✓ AVAILABLE DOWNLOADS TROVATO")
    print(f"Tag: {downloads_section.name}")
    print(f"Testo: {downloads_section.get_text(strip=True)}")
    print("\n--- SIBLINGS (elementi successivi) ---")
    for idx, sib in enumerate(downloads_section.find_next_siblings()):
        if sib.name and sib.name.startswith("h"):
            print(f"  [Stop: trovato heading {sib.name}]")
            break
        links = sib.find_all('a', href=True)
        if links:
            print(f"  Sibling {idx} ({sib.name}): {len(links)} link(s)")
            for link in links:
                print(f"    - {link.get_text(strip=True)}: {link['href']}")
        else:
            print(f"  Sibling {idx} ({sib.name}): nessun link")
else:
    print("✗ AVAILABLE DOWNLOADS NON TROVATO")

print("\n" + "=" * 80)

# 3. Cerca il link "Apply now!"
print("\n### TEST 3: APPLICATION FORM LINK ###\n")
apply_link = soup.find("a", string=lambda s: s and "Apply now" in s)
if apply_link:
    print("✓ APPLY NOW TROVATO")
    print(f"Testo: {apply_link.get_text(strip=True)}")
    print(f"Href: {apply_link.get('href')}")

    # Segui il link
    app_url = apply_link['href']
    if not app_url.startswith("http"):
        app_url = "https://www.salto-youth.net" + app_url

    print(f"\nSeguendo: {app_url}")
    try:
        resp2 = requests.get(app_url, timeout=10)
        soup2 = BeautifulSoup(resp2.text, "html.parser")

        external_link = soup2.find("a", string=lambda s: s and "Proceed to the external" in s)
        if external_link:
            print("\n✓ EXTERNAL LINK TROVATO")
            print(f"Testo: {external_link.get_text(strip=True)}")
            print(f"Href: {external_link.get('href')}")
        else:
            print("\n✗ EXTERNAL LINK NON TROVATO")
            print("\n--- PRIMI 15 LINK NELLA PAGINA ---")
            for idx, a in enumerate(soup2.find_all("a", href=True)[:15]):
                print(f"  [{idx}] {a.get_text(strip=True)[:50]}: {a['href']}")
    except Exception as e:
        print(f"\n✗ ERRORE nel seguire il link: {e}")
else:
    print("✗ APPLY NOW NON TROVATO")
    print("\n--- CERCO LINK SIMILI ---")
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if "apply" in text:
            print(f"  - {a.get_text(strip=True)}: {a['href']}")

print("\n" + "=" * 80)
print("\n### TEST COMPLETATO ###\n")