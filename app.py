import os
import json
import time
import requests
from flask import Flask, Response
from bs4 import BeautifulSoup

import gspread
from google.oauth2.service_account import Credentials

# ===================== CONFIG =====================

BASE_URL = "https://www.salto-youth.net"
BROWSE_URL = BASE_URL + "/tools/european-training-calendar/browse/"
SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

OFFSET_STEP = 10
REQUEST_TIMEOUT = 30

# ===================== GOOGLE SHEETS =====================

def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDS_JSON missing")

    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return sheet


def ensure_header(sheet):
    header = sheet.row_values(1)
    if header:
        return

    sheet.append_row([
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
    ])


def load_existing_urls(sheet):
    urls = set()
    rows = sheet.get_all_records()
    for r in rows:
        if r.get("detail_url"):
            urls.add(r["detail_url"])
    return urls


# ===================== SCRAPING =====================

def build_browse_url(offset):
    return (
        f"{BROWSE_URL}"
        f"?b_offset={offset}"
        f"&b_limit=10"
        f"&b_order=applicationDeadline"
    )


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "/tools/european-training-calendar/training/" not in href:
            continue

        if not href.startswith("http"):
            href = BASE_URL + href

        if href in seen:
            continue
        seen.add(href)

        title = a.get_text(strip=True)
        if not title:
            continue

        events.append({
            "title": title,
            "detail_url": href
        })

    return events


def scrape_all_events(log):
    sheet = get_gsheet()
    ensure_header(sheet)
    existing_urls = load_existing_urls(sheet)

    new_count = 0
    offset = 0

    while True:
        log(f"📄 Offset {offset}")

        url = build_browse_url(offset)
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()

        events = parse_list(r.text)
        if not events:
            log("🛑 Nessun evento trovato, fine scraping")
            break

        for e in events:
            if e["detail_url"] in existing_urls:
                log(f"⏭ SKIP | {e['title']}")
                continue

            row = [
                e["title"],
                e["detail_url"],
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "FALSE",
            ]

            sheet.append_row(row)
            existing_urls.add(e["detail_url"])
            new_count += 1
            log(f"🆕 NEW | {e['title']}")

            time.sleep(1)  # evita quota Google

        offset += OFFSET_STEP
        time.sleep(1)

    log(f"✅ Nuovi eventi aggiunti: {new_count}")


# ===================== FLASK APP =====================

app = Flask(__name__)


@app.route("/")
def index():
    return """
    <html>
    <head>
        <title>SALTO Scraper</title>
        <style>
            body { font-family: monospace; padding: 20px; }
            #log { white-space: pre-wrap; border: 1px solid #ccc; padding: 10px; }
        </style>
    </head>
    <body>
        <h1>SALTO Events Scraper</h1>
        <button onclick="start()">Start scraping</button>
        <pre id="log"></pre>

        <script>
            function start() {
                const log = document.getElementById("log");
                log.textContent = "";
                const evt = new EventSource("/api/scrape");
                evt.onmessage = function(e) {
                    log.textContent += e.data + "\\n";
                };
                evt.onerror = function() {
                    evt.close();
                };
            }
        </script>
    </body>
    </html>
    """


@app.route("/api/scrape")
def api_scrape():
    def generate():
        try:
            yield "🚀 Scraping avviato\n"
            scrape_all_events(lambda m: (yield m))
        except Exception as e:
            yield f"❌ ERROR: {str(e)}\n"

    return Response(generate(), mimetype="text/event-stream")


# ===================== MAIN =====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
