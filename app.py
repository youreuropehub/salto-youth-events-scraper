# ===================== MONKEY PATCH =====================
from gevent import monkey
monkey.patch_all()

# ===================== IMPORT =====================
import os
import time
import csv
import re
import json
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file, request
from flask_socketio import SocketIO
from bs4 import BeautifulSoup
import requests

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# ===================== APP =====================
app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_EVENTS = 1000

GOOGLE_SHEET_NAME = "SALTO-EVENTS"
GOOGLE_TAB_NAME = "SALTO-EVENTS"
DEDUP_KEY = "detail_url"

scraped_data = []

# ===================== GOOGLE SHEETS =====================
def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDS_JSON not set")

    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open(GOOGLE_SHEET_NAME)
    try:
        worksheet = sheet.worksheet(GOOGLE_TAB_NAME)
    except gspread.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=GOOGLE_TAB_NAME, rows=1000, cols=50)

    return worksheet

def export_to_google_sheet(rows):
    if not rows:
        return

    ws = get_gsheet()
    existing = ws.get_all_records()
    existing_keys = {r.get(DEDUP_KEY) for r in existing if r.get(DEDUP_KEY)}

    new_rows = [r for r in rows if r.get(DEDUP_KEY) not in existing_keys]

    if not existing:
        ws.append_row(list(rows[0].keys()))

    if new_rows:
        ws.append_rows([list(r.values()) for r in new_rows])
        socketio.emit("log", {"message": f"Google Sheet: aggiunti {len(new_rows)} nuovi eventi"})
    else:
        socketio.emit("log", {"message": "Google Sheet: nessun nuovo evento"})

# ===================== URL BUILDER =====================
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

# ===================== PARSING =====================
def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        url = a.get("href")
        if not url.startswith("http"):
            url = BASE_URL + url
        events.append({
            "title": title,
            "detail_url": url
        })
    return events

def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")
    return {
        "training_summary": soup.get_text(" ", strip=True)[:500]
    }

# ===================== CSV =====================
def save_csv(rows):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return path

# ===================== SCRAPER =====================
def scrape_events(max_pages, max_events):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    seen = set()

    for page in range(max_pages):
        offset = page * 10
        resp = session.get(build_search_url(offset), timeout=15)
        events = parse_list_page(resp.text)
        if not events:
            break

        for ev in events:
            if ev["detail_url"] in seen:
                continue
            seen.add(ev["detail_url"])

            detail = session.get(ev["detail_url"], timeout=15)
            ev.update(parse_detail_page(detail.text))
            scraped_data.append(ev)

            socketio.emit("log", {"message": ev["title"]})
            if len(scraped_data) >= max_events:
                break
            time.sleep(1)

        if len(scraped_data) >= max_events:
            break

    save_csv(scraped_data)
    export_to_google_sheet(scraped_data)

# ===================== ROUTES =====================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/scrape", methods=["POST", "GET"])
def api_scrape():
    scrape_events(DEFAULT_MAX_PAGES, DEFAULT_MAX_EVENTS)
    return jsonify({"status": "ok", "count": len(scraped_data)})

@app.route("/download_csv")
def download_csv():
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    return send_file(path, as_attachment=True)

# ===================== MAIN =====================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
