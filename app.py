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
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)


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
    return {
        r["detail_url"]
        for r in sheet.get_all_records()
        if r.get("detail_url")
    }

# ================= SCRAPING =================

def browse_url(offset):
    return f"{BROWSE_URL}?b_offset={offset}&b_limit=10&b_order=applicationDeadline"


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    events = {}

    for a in soup.find_all("a", href=True):
        if "/tools/european-training-calendar/training/" not in a["href"]:
            continue

        href = a["href"]
        if not href.startswith("http"):
            href = BASE_URL + href

        title = a.get_text(strip=True)
        if title:
            events[href] = {"title": title, "detail_url": href}

    return list(events.values())


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")

    def find_text(label):
        el = soup.find(string=lambda s: s and label.lower() in s.lower())
        if not el:
            return ""
        nxt = el.parent.find_next_sibling()
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
        "type": find_text("Type"),
        "participants_no": find_text("Participants"),
        "participants_from": find_text("from"),
        "recommended_for": find_text("recommended"),
        "accessibility": find_text("Accessibility"),
        "working_language": find_text("Working language"),
        "organiser": find_text("Organiser"),
        "infopack_downloads": infopack,
        "application_procedure_url": app_proc,
    }


def scrape_generator():
    yield "🚀 Scraping avviato"

    sheet = get_sheet()
    ensure_header(sheet)
    known = existing_urls(sheet)

    added = 0
    offset = 0

    while True:
        yield f"📄 Pagina offset {offset}"

        r = requests.get(browse_url(offset), timeout=TIMEOUT)
        r.raise_for_status()

        events = parse_list(r.text)
        if not events:
            break

        for e in events:
            if e["detail_url"] in known:
                yield f"⏭ {e['title']}"
                continue

            yield f"➡️ {e['title']}"

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
            yield "🆕 aggiunto"
            time.sleep(1)

        offset += OFFSET_STEP
        time.sleep(1)

    yield f"✅ Nuovi eventi aggiunti: {added}"

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
        for msg in scrape_generator():
            yield f"data: {msg}\n\n"

    return Response(stream(), mimetype="text/event-stream")

# ================= MAIN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
