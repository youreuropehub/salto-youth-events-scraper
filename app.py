import os
import requests
import gspread
from flask import Flask, Response
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials
from urllib.parse import urljoin
from datetime import datetime
import time

# ===================== CONFIG =====================

BASE_URL = "https://www.salto-youth.net"
BROWSE_URL = "https://www.salto-youth.net/tools/european-training-calendar/browse/"

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

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

SHEET_NAME = os.environ.get("GSHEET_NAME")
CREDS_JSON = "credentials.json"

# ===================== GOOGLE SHEET =====================

def get_sheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        CREDS_JSON, SCOPES
    )
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).sheet1


def prepare_sheet(sheet):
    sheet.clear()
    sheet.insert_row(HEADERS, 1)


def append_event(sheet, event):
    row = [event.get(h, "") for h in HEADERS]
    sheet.append_row(row, value_input_option="RAW")


# ===================== SCRAPER =====================

def scrape_events(log):

    sheet = get_sheet()
    prepare_sheet(sheet)

    log("🧹 Foglio pulito e header creato")
    log("🚀 Scraping avviato")

    page = 1
    offset = 0

    while True:
        log(f"\n📄 Pagina {page}")

        params = {
            "b_offset": offset,
            "b_limit": 10,
        }

        r = requests.get(BROWSE_URL, params=params, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")

        items = soup.select(".tool-item")
        if not items:
            log("✅ Nessun altro evento trovato")
            break

        for item in items:
            title_el = item.select_one("h2 a")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            detail_url = urljoin(BASE_URL, title_el["href"])

            log(f"🔍 {title}")

            event = scrape_detail(detail_url)
            append_event(sheet, event)

            log("   ➕ salvato")

            time.sleep(0.5)

        offset += 10
        page += 1

    log("\n🏁 Scraping completato")


def scrape_detail(url):
    r = requests.get(url, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    def text(selector):
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    def join_text(selector):
        return " ".join(
            el.get_text(strip=True)
            for el in soup.select(selector)
        )

    event = {
        "title": text("h1"),
        "detail_url": url,
        "type": text(".tool-item-category"),
        "infopack_downloads": "",
        "application_procedure_url": "",
        "participants_no": "",
        "participants_from": "",
        "recommended_for": "",
        "accessibility": "",
        "working_language": "",
        "organiser": "",
        "posted_to_instagram": "FALSE",
    }

    # infopack
    dl = soup.select_one(".downloads-list a")
    if dl:
        event["infopack_downloads"] = urljoin(BASE_URL, dl["href"])

    # application procedure
    app_link = soup.find("a", href=lambda x: x and "application-procedure" in x)
    if app_link:
        event["application_procedure_url"] = urljoin(BASE_URL, app_link["href"])

    # overview
    overview = soup.find("h3", string="Training overview")
    if overview:
        block = overview.find_next("div")

        ps = block.find_all("p")
        for p in ps:
            label = p.find("span")
            if not label:
                continue

            key = label.get_text(strip=True).lower()

            value = p.get_text(strip=True).replace(label.get_text(strip=True), "").strip()

            if "for" in key:
                event["participants_no"] = value
            elif "from" in key:
                event["participants_from"] = value
            elif "recommended" in key:
                event["recommended_for"] = value
            elif "accessible" in key:
                event["accessibility"] = value
            elif "language" in key:
                event["working_language"] = value

    # organiser
    organiser = soup.select_one(".training-organiser")
    if organiser:
        event["organiser"] = organiser.get_text(strip=True)

    return event


# ===================== FLASK =====================

app = Flask(__name__)

@app.route("/")
def home():
    return "SALTO scraper online"


@app.route("/api/scrape")
def api_scrape():

    def stream():
        def log(msg):
            yield msg + "\n"

        for line in scrape_generator(log):
            yield line

    return Response(stream(), mimetype="text/plain")


def scrape_generator(log):
    buffer = []

    def _log(msg):
        buffer.append(msg + "\n")

    scrape_events(_log)

    for line in buffer:
        yield line


# ===================== MAIN =====================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
