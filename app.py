import os
import json
import time
import requests
from flask import Flask, Response
from bs4 import BeautifulSoup

import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG
# =========================

BASE_URL = "https://www.salto-youth.net/tools/european-training-calendar/browse/"
DETAIL_BASE = "https://www.salto-youth.net"

SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
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
    "posted_to_instagram"
]

# =========================
# GOOGLE SHEET
# =========================

def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    # crea header se vuoto
    if sheet.row_count == 0 or sheet.get("A1") == []:
        sheet.append_row(HEADERS)

    return sheet


def get_existing_urls(sheet):
    values = sheet.get_all_values()
    if len(values) <= 1:
        return set()
    url_index = HEADERS.index("detail_url")
    return set(row[url_index] for row in values[1:] if len(row) > url_index)


# =========================
# SCRAPER
# =========================

def scrape_events(stream_log):
    sheet = get_sheet()
    existing_urls = get_existing_urls(sheet)

    page = 0
    added = 0

    while True:
        offset = page * 10
        url = f"{BASE_URL}?b_offset={offset}&b_limit=10"
        stream_log(f"📄 Pagina {page + 1}")

        res = requests.get(url, timeout=30)
        soup = BeautifulSoup(res.text, "html.parser")

        items = soup.select(".tool-item")
        if not items:
            break

        for item in items:
            title_el = item.select_one("h2 a")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            detail_url = DETAIL_BASE + title_el["href"]

            stream_log(f"🔍 {title}")

            if detail_url in existing_urls:
                continue

            data = parse_detail(detail_url)

            row = [
                title,
                detail_url,
                data.get("type", ""),
                data.get("infopack_downloads", ""),
                data.get("application_procedure_url", ""),
                data.get("participants_no", ""),
                data.get("participants_from", ""),
                data.get("recommended_for", ""),
                data.get("accessibility", ""),
                data.get("working_language", ""),
                data.get("organiser", ""),
                "FALSE",
            ]

            sheet.append_row(row, value_input_option="RAW")
            existing_urls.add(detail_url)
            added += 1

        page += 1
        time.sleep(1)

    stream_log(f"✅ Nuovi eventi aggiunti: {added}")


def parse_detail(url):
    res = requests.get(url, timeout=30)
    soup = BeautifulSoup(res.text, "html.parser")

    def text(sel):
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else ""

    def link(sel):
        el = soup.select_one(sel)
        return DETAIL_BASE + el["href"] if el and el.has_attr("href") else ""

    data = {}

    data["type"] = text("span.tool-item-category")
    data["organiser"] = text("p.organiser")
    data["working_language"] = text("p:contains('Working language')")

    data["application_procedure_url"] = link("a[href*='application-procedure']")
    data["infopack_downloads"] = link("ul.downloads-list a")

    overview = soup.select_one("h3:contains('Training overview')")
    if overview:
        p = overview.find_next_siblings("p")
        for el in p:
            t = el.get_text(strip=True)
            if "for" in t:
                data["participants_no"] = t.replace("participants", "").strip()
            if "from" in t:
                data["participants_from"] = t
            if "recommended for" in t.lower():
                data["recommended_for"] = t

    data["accessibility"] = text("p:contains('accessible')")
    return data


# =========================
# FLASK
# =========================

app = Flask(__name__)

@app.route("/")
def home():
    return "<h2>SALTO scraper attivo</h2><a href='/api/scrape'>Avvia scraping</a>"


@app.route("/api/scrape")
def api_scrape():

    def stream():
        yield "🚀 Scraping avviato\n\n"

        def log(msg):
            yield_msg = msg + "\n"
            nonlocal_buffer.append(yield_msg)

        nonlocal_buffer = []

        def stream_log(msg):
            nonlocal_buffer.append(msg + "\n")

        scrape_events(stream_log)

        for m in nonlocal_buffer:
            yield m

    return Response(stream(), mimetype="text/plain")


# =========================
# RUN
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
