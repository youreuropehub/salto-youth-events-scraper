# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
import json
from io import StringIO
from flask import Flask, jsonify, request, render_template
from flask_socketio import SocketIO
from bs4 import BeautifulSoup
import requests

import gspread
from google.oauth2.service_account import Credentials


# ===================== CONFIG =====================

BASE_URL = "https://www.salto-youth.net"
SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

DEFAULT_MAX_PAGES = 50
PAGE_SIZE = 10

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")


# ===================== GOOGLE SHEETS =====================

def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDS_JSON non impostato")

    creds_dict = json.loads(creds_json)

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)

    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    return sheet


# ===================== SCRAPING =====================

def build_search_url(offset: int) -> str:
    return (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit={PAGE_SIZE}"
        "&b_order=applicationDeadline"
    )


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title:
            continue

        url = link.get("href", "").strip()
        if not url:
            continue

        if not url.startswith("http"):
            url = BASE_URL + url

        if url in seen:
            continue

        seen.add(url)

        container = link.find_parent("div")
        text = container.get_text("\n", strip=True) if container else ""
        lines = [l for l in text.split("\n") if l.strip()]

        idx = lines.index(title) if title in lines else 0

        events.append({
            "title": title,
            "type": lines[idx - 1] if idx > 0 else "",
            "dates": lines[idx + 1] if idx + 1 < len(lines) else "",
            "location": lines[idx + 2] if idx + 2 < len(lines) else "",
            "application_deadline": "",
            "detail_url": url
        })

    return events


def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")

    def get_section(label):
        h = soup.find(lambda t: t.name in ["h3", "h4"] and label.lower() in t.get_text(strip=True).lower())
        if not h:
            return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        return " ".join(parts)

    summary = soup.find("div", class_=re.compile("training-summary"))
    description = soup.find("div", class_=re.compile("training-description"))

    return {
        "participants_no": "",
        "participants_from": "",
        "recommended_for": "",
        "accessibility": get_section("Accessibility"),
        "working_language": "",
        "organiser": "",
        "participation_fee": get_section("Participation fee"),
        "accommodation_food": get_section("Accommodation"),
        "travel_reimbursement": get_section("Travel reimbursement"),
        "infopack_downloads": "",
        "application_procedure_url": "",
        "training_summary": summary.get_text("\n", strip=True) if summary else "",
        "training_description": description.get_text("\n", strip=True) if description else "",
    }


def scrape_events():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events = {}
    page = 0

    while page < DEFAULT_MAX_PAGES:
        offset = page * PAGE_SIZE
        socketio.emit("log", {"message": f"Pagina {page + 1}"})

        r = session.get(build_search_url(offset), timeout=20)
        r.raise_for_status()

        page_events = parse_list_page(r.text)
        if not page_events:
            break

        for e in page_events:
            events.setdefault(e["detail_url"], e)

        page += 1
        time.sleep(1)

    socketio.emit("log", {"message": f"Eventi trovati: {len(events)}"})

    for e in events.values():
        try:
            r = session.get(e["detail_url"], timeout=20)
            r.raise_for_status()
            e.update(parse_detail_page(r.text))
            time.sleep(1)
        except Exception:
            continue

    return list(events.values())


# ===================== EXPORT GOOGLE SHEETS =====================

def export_to_google_sheet(events):
    sheet = get_gsheet()

    rows = sheet.get_all_records()
    existing_urls = {r.get("detail_url") for r in rows if r.get("detail_url")}

    new_rows = [e for e in events if e["detail_url"] not in existing_urls]

    if not rows:
        sheet.append_row(list(events[0].keys()))

    if new_rows:
        sheet.append_rows([list(e.values()) for e in new_rows])
        socketio.emit("log", {"message": f"Aggiunti {len(new_rows)} nuovi eventi"})
    else:
        socketio.emit("log", {"message": "Nessun evento nuovo"})


# ===================== ROUTES =====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["GET", "POST"])
def api_scrape():
    events = scrape_events()
    export_to_google_sheet(events)
    return jsonify({"status": "ok", "count": len(events)})


# ===================== MAIN =====================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
