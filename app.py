# ===================== GEVENT PATCH =====================
from gevent import monkey
monkey.patch_all()

# ===================== IMPORT =====================
import os
import time
import json
import re
import requests
from datetime import date
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO
from bs4 import BeautifulSoup

import gspread
from google.oauth2.service_account import Credentials

# ===================== CONFIG =====================
BASE_URL = "https://www.salto-youth.net"
SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

DEFAULT_MAX_PAGES = 50
PAGE_SIZE = 10

# ordine colonne FISSO
HEADERS = [
    "title", "type", "dates", "location", "application_deadline",
    "participants_no", "participants_from", "recommended_for",
    "accessibility", "working_language", "organiser",
    "participation_fee", "accommodation_food", "travel_reimbursement",
    "infopack_downloads", "application_procedure_url",
    "application_form_link", "detail_url",
    "training_summary", "training_description"
]

# ===================== APP =====================
app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"
socketio = SocketIO(app, cors_allowed_origins="*")

# ===================== GOOGLE SHEET =====================
def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    # header garantito
    if sheet.row_count == 0 or sheet.row_values(1) != HEADERS:
        sheet.clear()
        sheet.append_row(HEADERS)

    return sheet

# ===================== SCRAPER =====================
def build_search_url(offset):
    today = date.today()
    return (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        f"&b_begin_date_after_day={today.day}"
        f"&b_begin_date_after_month={today.month}"
        f"&b_begin_date_after_year={today.year}"
    )

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for a in soup.select("h3 a"):
        title = a.get_text(strip=True)
        url = a.get("href", "")
        if not url.startswith("http"):
            url = BASE_URL + url

        block = a.parent.parent.get_text("\n", strip=True).split("\n")

        events.append({
            "title": title,
            "type": block[0] if len(block) > 0 else "",
            "dates": block[2] if len(block) > 2 else "",
            "location": block[3] if len(block) > 3 else "",
            "application_deadline": "",
            "detail_url": url
        })

    return events

def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")

    def text_after(label):
        h = soup.find(lambda t: t.name in ["h3", "h4"] and label in t.get_text())
        if not h:
            return ""
        out = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            out.append(sib.get_text(" ", strip=True))
        return " ".join(out)

    return {
        "participants_no": "",
        "participants_from": "",
        "recommended_for": "",
        "accessibility": text_after("Accessibility"),
        "working_language": text_after("Working language"),
        "organiser": text_after("Organiser"),
        "participation_fee": text_after("Participation fee"),
        "accommodation_food": text_after("Accommodation"),
        "travel_reimbursement": text_after("Travel reimbursement"),
        "infopack_downloads": "",
        "application_procedure_url": "",
        "training_summary": soup.get_text(" ", strip=True)[:500],
        "training_description": soup.get_text(" ", strip=True)[:1500]
    }

def scrape_events():
    socketio.emit("log", {"message": "🚀 Scraping avviato"})

    sheet = get_gsheet()
    existing_urls = set(sheet.col_values(HEADERS.index("detail_url") + 1)[1:])

    session = requests.Session()
    new_rows = []

    for page in range(DEFAULT_MAX_PAGES):
        offset = page * PAGE_SIZE
        socketio.emit("log", {"message": f"📄 Pagina {page + 1}"})

        resp = session.get(build_search_url(offset), timeout=15)
        events = parse_list_page(resp.text)

        if not events:
            break

        for event in events:
            is_new = event["detail_url"] not in existing_urls
            status = "🆕 NEW" if is_new else "⏭ SKIP"

            socketio.emit("log", {
                "message": f"{status} | {event['title']} | {event['location']}"
            })

            if not is_new:
                continue

            detail_resp = session.get(event["detail_url"], timeout=15)
            event.update(parse_detail_page(detail_resp.text))
            event["application_form_link"] = ""

            row = [event.get(h, "") for h in HEADERS]
            new_rows.append(row)
            existing_urls.add(event["detail_url"])

            time.sleep(0.5)

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")

    socketio.emit("log", {"message": f"✅ Nuovi eventi aggiunti: {len(new_rows)}"})
    return len(new_rows)

# ===================== ROUTES =====================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/scrape", methods=["GET"])
def api_scrape():
    count = scrape_events()
    return jsonify({"status": "ok", "new_events": count})

@socketio.on("start_scraping")
def handle_start_scraping():
    scrape_events()

# ===================== RUN =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
