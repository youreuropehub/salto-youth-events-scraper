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
BROWSE_URL = BASE_URL + "/tools/european-training-calendar/browse/"

SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

OFFSET_STEP = 10
TIMEOUT = 30

# ================= GOOGLE SHEETS =================

def get_sheet():
    creds_json = os.environ["GOOGLE_CREDS_JSON"]
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
    if sheet.row_values(1):
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


def existing_urls(sheet):
    urls = set()
    for row in sheet.get_all_records():
        if row.get("detail_url"):
            urls.add(row["detail_url"])
    return urls

# ================= SCRAPING =================

def browse_url(offset):
    return f"{BROWSE_URL}?b_offset={offset}&b_limit=10&b_order=applicationDeadline"


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for a in soup.find_all("a", href=True):
        if "/tools/european-training-calendar/training/" not in a["href"]:
            continue

        href = a["href"]
        if not href.startswith("http"):
            href = BASE_URL + href

        title = a.get_text(strip=True)
        if not title:
            continue

        events.append({
            "title": title,
            "detail_url": href
        })

    return list({e["detail_url"]: e for e in events}.values())


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")

    def text_after(label):
        el = soup.find(string=lambda s: s and label in s)
        if not el:
            return ""
        parent = el.parent
        nxt = parent.find_next_sibling()
        return nxt.get_text(" ", strip=True) if nxt else ""

    infopack = ""
    for a in soup.find_all("a", href=True):
        if "download" in a["href"].lower():
            infopack = a["href"]
            if not infopack.startswith("http"):
                infopack = BASE_URL + infopack
            break

    app_proc = ""
    for a in soup.find_all("a", href=True):
        if "/application-procedure/" in a["href"]:
            app_proc = BASE_URL + a["href"]
            break

    return {
        "type": text_after("Type"),
        "participants_no": text_after("Participants"),
        "participants_from": text_after("from"),
        "recommended_for": text_after("recommended for"),
        "working_language": text_after("Working language"),
        "organiser": text_after("Organiser"),
        "accessibility": text_after("Accessibility"),
        "infopack_downloads": infopack,
        "application_procedure_url": app_proc,
    }


def scrape(log):
    sheet = get_sheet()
    ensure_header(sheet)

    known = existing_urls(sheet)
    added = 0
    offset = 0

    while True:
        log(f"📄 Pagina offset {offset}")
        r = requests.get(browse_url(offset), timeout=TIMEOUT)
        r.raise_for_status()

        events = parse_list(r.text)
        if not events:
            break

        for e in events:
            if e["detail_url"] in known:
                continue

            log(f"➡️ {e['title']}")
            d = requests.get(e["detail_url"], timeout=TIMEOUT)
            d.raise_for_status()

            detail = parse_detail(d.text)

            sheet.append_row([
                e["title"],
                e["detail_url"],
                detail["type"],
                detail["infopack_downloads"],
                detail["application_procedure_url"],
                detail["participants_no"],
                detail["participants_from"],
                detail["recommended_for"],
                detail["accessibility"],
                detail["working_language"],
                detail["organiser"],
                "FALSE",
            ])

            known.add(e["detail_url"])
            added += 1
            log(f"🆕 aggiunto")
            time.sleep(1)

        offset += OFFSET_STEP
        time.sleep(1)

    log(f"✅ Nuovi eventi aggiunti: {added}")

# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def index():
    return """
    <h1>SALTO Scraper</h1>
    <button onclick="start()">Start</button>
    <pre id="log"></pre>
    <script>
    function start(){
        const log = document.getElementById("log");
        log.textContent = "";
        const es = new EventSource("/api/scrape");
        es.onmessage = e => log.textContent += e.data + "\\n";
        es.onerror = () => es.close();
    }
    </script>
    """

@app.route("/api/scrape")
def api_scrape():
    def stream():
        try:
            yield "🚀 Scraping avviato\n"
            scrape(lambda m: (yield m))
        except Exception as e:
            yield f"❌ {str(e)}\n"

    return Response(stream(), mimetype="text/event-stream")

# ================= MAIN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
