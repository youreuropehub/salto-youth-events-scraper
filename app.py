from gevent import monkey
monkey.patch_all()

import os
import json
import time
import requests
from datetime import date
from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
BASE_URL = "https://www.salto-youth.net"
SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

DEFAULT_MAX_PAGES = 50
PAGE_SIZE = 10

HEADERS = [
    "title","type","dates","location","application_deadline",
    "participants_no","participants_from","recommended_for",
    "accessibility","working_language","organiser",
    "participation_fee","accommodation_food","travel_reimbursement",
    "infopack_downloads","application_procedure_url",
    "application_form_link","detail_url",
    "training_summary","training_description",
    "posted_to_instagram"
]

# ================= APP =================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# ================= GOOGLE SHEET =================
def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDS_JSON"]),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    existing_headers = sheet.row_values(1)

    if not existing_headers:
        sheet.append_row(HEADERS)
        return sheet

    # aggiunge colonne mancanti SENZA cancellare
    for h in HEADERS:
        if h not in existing_headers:
            sheet.add_cols(1)
            sheet.update_cell(1, len(existing_headers) + 1, h)
            existing_headers.append(h)

    return sheet

# ================= SCRAPER =================
def build_url(offset):
    today = date.today()
    return (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        f"&b_begin_date_after_day={today.day}"
        f"&b_begin_date_after_month={today.month}"
        f"&b_begin_date_after_year={today.year}"
    )

def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a:
            continue

        url = a.get("href","")
        if not url.startswith("http"):
            url = BASE_URL + url

        events.append({
            "title": a.get_text(strip=True),
            "detail_url": url
        })

    return events

def scrape():
    socketio.emit("log", {"message": "🚀 Scraping avviato"})
    sheet = get_sheet()

    rows = sheet.get_all_records()
    existing_urls = {r["detail_url"] for r in rows if r.get("detail_url")}

    session = requests.Session()
    new_rows = []

    for page in range(DEFAULT_MAX_PAGES):
        socketio.emit("log", {"message": f"📄 Pagina {page+1}"})
        r = session.get(build_url(page * PAGE_SIZE), timeout=20)
        events = parse_list(r.text)

        if not events:
            break

        for ev in events:
            is_new = ev["detail_url"] not in existing_urls
            status = "🆕 NEW" if is_new else "⏭ SKIP"

            socketio.emit("log", {
                "message": f"{status} | {ev['title']}"
            })

            if not is_new:
                continue

            ev["posted_to_instagram"] = "FALSE"
            row = [ev.get(h, "") for h in HEADERS]
            new_rows.append(row)
            existing_urls.add(ev["detail_url"])

            time.sleep(0.5)

    if new_rows:
        sheet.append_rows(new_rows)

    socketio.emit("log", {
        "message": f"✅ Nuovi eventi aggiunti: {len(new_rows)}"
    })

    return len(new_rows)

# ================= ROUTES =================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/scrape")
def api_scrape():
    return jsonify({"new_events": scrape()})

@socketio.on("start_scraping")
def start_scraping():
    scrape()

# ================= RUN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
