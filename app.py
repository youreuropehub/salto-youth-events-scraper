import os
import json
import time
import requests
from flask import Flask, Response
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================

BASE_URL = "https://www.salto-youth.net"
BROWSE_URL = "https://www.salto-youth.net/tools/european-training-calendar/browse/"

SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

HEADERS = [
    "title",
    "detail_url",
    "type",
    "infopack_downloads",
    "application_procedure_url",
    "participants_no",
    "participants_from",
    "recommended_for",
    "accessibility",
    "working_language",
    "organiser",
    "posted_to_instagram"
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ================= GOOGLE SHEET =================

def get_gsheet():
    creds = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    credentials = Credentials.from_service_account_info(creds, scopes=SCOPES)
    client = gspread.authorize(credentials)

    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    values = sheet.get_all_values()
    if not values:
        sheet.append_row(HEADERS)

    return sheet


def get_existing_urls(sheet):
    records = sheet.get_all_records()
    return {r["detail_url"] for r in records if r.get("detail_url")}


# ================= SCRAPING =================

def scrape_list(offset):
    r = requests.get(
        BROWSE_URL,
        params={"b_offset": offset, "b_limit": 10},
        timeout=30
    )
    r.raise_for_status()
    return r.text


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for item in soup.select(".tool-item"):
        a = item.select_one("h2 a")
        if not a:
            continue

        url = a["href"]
        if not url.startswith("http"):
            url = BASE_URL + url

        events.append({
            "title": a.get_text(strip=True),
            "detail_url": url
        })

    return events


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")

    data = {h: "" for h in HEADERS}
    data["posted_to_instagram"] = "FALSE"

    cat = soup.select_one("span.h3.tool-item-category")
    if cat:
        data["type"] = cat.get_text(strip=True)

    d = soup.select_one("ul.downloads-list a")
    if d:
        data["infopack_downloads"] = BASE_URL + d["href"] if d["href"].startswith("/") else d["href"]

    for a in soup.select('a[href*="/application-procedure/"]'):
        data["application_procedure_url"] = BASE_URL + a["href"] if a["href"].startswith("/") else a["href"]
        break

    h = soup.find("h3", string="Training overview")
    if h:
        for p in h.find_next_siblings("p"):
            t = p.get_text(" ", strip=True)
            if p.find("span", string="for"):
                data["participants_no"] = t.replace("participants", "").strip()
            elif p.find("span", string="from"):
                data["participants_from"] = t.replace("from", "").strip()
            elif "recommended for" in t.lower():
                n = p.find_next_sibling("p")
                if n:
                    data["recommended_for"] = n.get_text(" ", strip=True)

    h = soup.find("h3", string="Accessibility")
    if h:
        txt = []
        for s in h.find_next_siblings():
            if s.name and s.name.startswith("h"):
                break
            txt.append(s.get_text(" ", strip=True))
        data["accessibility"] = " ".join(txt)

    for p in soup.select(".microcopy"):
        if "language" in p.get_text().lower():
            data["working_language"] = p.get_text(strip=True)

    for p in soup.find_all("p"):
        if p.get_text(strip=True).lower().startswith("organiser"):
            data["organiser"] = p.get_text().split(":", 1)[-1].strip()

    return data


# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def index():
    return '<a href="/api/scrape">▶ Avvia scraping</a>'


@app.route("/api/scrape")
def scrape():

    def stream():
        yield "🚀 Scraping avviato\n\n"

        sheet = get_gsheet()
        existing_urls = get_existing_urls(sheet)

        offset = 0
        page = 1
        added = 0

        while True:
            yield f"📄 Pagina {page}\n"
            events = parse_list(scrape_list(offset))

            if not events:
                break

            for ev in events:
                yield f"🔍 {ev['title']}\n"

                if ev["detail_url"] in existing_urls:
                    yield "🔁 GIÀ ESISTENTE\n\n"
                    continue

                r = requests.get(ev["detail_url"], timeout=30)
                ev.update(parse_detail(r.text))

                row = [ev[h] for h in HEADERS]
                sheet.append_row(row)

                existing_urls.add(ev["detail_url"])
                added += 1
                yield "➕ AGGIUNTO\n\n"

            offset += 10
            page += 1
            time.sleep(1)

        yield f"\n✅ Nuovi eventi aggiunti: {added}\n"

    return Response(stream(), mimetype="text/plain")


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
