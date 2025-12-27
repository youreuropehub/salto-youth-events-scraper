import os
import json
import csv
import time
import requests
from flask import Flask, Response, stream_with_context
from bs4 import BeautifulSoup
from urllib.parse import urljoin

import gspread
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ----------------

BASE_URL = "https://www.salto-youth.net"
BROWSE_URL = "https://www.salto-youth.net/tools/european-training-calendar/browse/"

SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
    "posted_to_instagram",
]

# ---------------- APP ----------------

app = Flask(__name__)

# ---------------- GOOGLE SHEET ----------------

def get_gsheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

def prepare_sheet(sheet):
    sheet.clear()
    sheet.insert_row(HEADERS, 1)

def get_existing_urls(sheet):
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return set()
    idx = HEADERS.index("detail_url")
    return set(r[idx] for r in rows[1:] if len(r) > idx)

# ---------------- HELPERS ----------------

def normalize_url(href):
    return urljoin(BASE_URL, href)

def text_or_empty(el):
    return el.get_text(strip=True) if el else ""

# ---------------- SCRAPING ----------------

def parse_detail(url):
    data = dict.fromkeys(HEADERS, "")
    data["detail_url"] = url
    data["posted_to_instagram"] = "FALSE"

    res = requests.get(url, timeout=30)
    soup = BeautifulSoup(res.text, "html.parser")

    data["title"] = text_or_empty(soup.select_one("h1"))
    data["type"] = text_or_empty(soup.select_one(".tool-item-category"))
    data["working_language"] = text_or_empty(soup.find("span", string="Working language"))
    data["organiser"] = text_or_empty(soup.find("span", string="Organiser"))

    # downloads
    downloads = soup.select("ul.downloads-list a")
    if downloads:
        data["infopack_downloads"] = normalize_url(downloads[0]["href"])

    # application procedure
    app_link = soup.select_one("a[href*='application-procedure']")
    if app_link:
        data["application_procedure_url"] = normalize_url(app_link["href"])

    # overview
    overview = soup.find("h3", string="Training overview")
    if overview:
        p = overview.find_next_siblings("p")
        for el in p:
            txt = el.get_text(strip=True)
            if "participants" in txt and "for" in txt:
                data["participants_no"] = txt
            if txt.startswith("from"):
                data["participants_from"] = txt.replace("from", "").strip()
            if "recommended" in txt:
                data["recommended_for"] = txt

    # accessibility
    acc = soup.find(string=lambda x: x and "accessible" in x.lower())
    if acc:
        data["accessibility"] = acc.strip()

    return data

def scrape_events(stream_log):
    page = 1
    sheet = get_gsheet()
    prepare_sheet(sheet)
    existing = set()

    yield "🧹 Foglio pulito e header creato\n\n"

    while True:
        yield f"📄 Pagina {page}\n"
        url = f"{BROWSE_URL}?b_offset={(page-1)*10}&b_limit=10"
        res = requests.get(url, timeout=30)
        soup = BeautifulSoup(res.text, "html.parser")

        items = soup.select(".tool-item")
        if not items:
            break

        for item in items:
            a = item.select_one("h2 a")
            if not a:
                continue

            detail_url = normalize_url(a["href"])
            title = a.get_text(strip=True)

            yield f"🔍 {title}\n"

            if detail_url in existing:
                yield "↩️ già presente\n"
                continue

            data = parse_detail(detail_url)
            row = [data[h] for h in HEADERS]
            sheet.append_row(row, value_input_option="RAW")

            existing.add(detail_url)
            yield "✅ aggiunto\n"

        page += 1
        time.sleep(1)

    yield "\n🏁 Scraping completato\n"

# ---------------- ROUTES ----------------

@app.route("/")
def index():
    return "<h2>SALTO scraper attivo</h2><p>/api/scrape</p>"

@app.route("/api/scrape")
def api_scrape():
    def stream():
        yield "🚀 Scraping avviato\n\n"
        yield from scrape_events(stream)
    return Response(stream_with_context(stream()), mimetype="text/plain")

# ---------------- RUN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
