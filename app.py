# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
from io import StringIO, BytesIO
from datetime import date

import requests
from bs4 import BeautifulSoup

from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit

# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

scraped_data = []

# ============================================================
# HELPERS
# ============================================================

def build_search_url(offset: int) -> str:
    today = date.today()
    return (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        f"&b_begin_date_after_day={today.day}"
        f"&b_begin_date_after_month={today.month}"
        f"&b_begin_date_after_year={today.year}"
        f"&b_application_deadline_after_day={today.day}"
        f"&b_application_deadline_after_month={today.month}"
        f"&b_application_deadline_after_year={today.year}"
    )


def parse_list_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for row in soup.select(".search-results-list .tool-item"):
        title_el = row.select_one("h2 a")
        if not title_el:
            continue

        detail_url = title_el.get("href", "").strip()
        if not detail_url:
            continue

        if not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        if detail_url in seen:
            continue
        seen.add(detail_url)

        events.append({
            "title": title_el.get_text(strip=True),
            "type": (row.select_one(".tool-item-category") or {}).get_text(strip=True),
            "dates": (row.select_one("p.h5") or {}).get_text(strip=True),
            "location": (row.select_one(".microcopy") or {}).get_text(strip=True),
            "application_deadline": "",
            "training_summary": "",
            "training_description": "",
            "detail_url": detail_url
        })

    return events


def parse_detail_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # ------------------------------------------------------------
    # Application deadline (stesso concetto di Scrapy)
    # ------------------------------------------------------------
    application_deadline = ""
    for el in soup.select(".call-addendum"):
        txt = el.get_text(" ", strip=True)
        if txt.lower().startswith("application deadline"):
            application_deadline = txt.split(":", 1)[-1].strip()
            break

    # ------------------------------------------------------------
    # Training summary
    # ------------------------------------------------------------
    summary_block = soup.select_one(".training-summary")
    training_summary = (
        " ".join(t.strip() for t in summary_block.stripped_strings)
        if summary_block else ""
    )

    # ------------------------------------------------------------
    # Training description
    # ------------------------------------------------------------
    desc_block = soup.select_one(".training-description")
    training_description = (
        " ".join(t.strip() for t in desc_block.stripped_strings)
        if desc_block else ""
    )

    return {
        "application_deadline": application_deadline,
        "training_summary": training_summary,
        "training_description": training_description
    }


def save_csv_to_disk():
    if not scraped_data:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fieldnames = [
        "title",
        "type",
        "dates",
        "location",
        "application_deadline",
        "training_summary",
        "training_description",
        "detail_url"
    ]

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})


# ============================================================
# SCRAPER CORE
# ============================================================

def scrape_events(max_pages: int = 5, max_events: int = 50):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    })

    socketio.emit("log", {"message": "Inizio scraping..."})

    page = 0
    page_size = 10

    while page < max_pages and len(scraped_data) < max_events:
        offset = page * page_size
        socketio.emit("log", {"message": f"Pagina {page + 1} (offset {offset})"})

        resp = session.get(build_search_url(offset), timeout=20)
        resp.raise_for_status()

        events = parse_list_page(resp.text)
        if not events:
            break

        for event in events:
            if len(scraped_data) >= max_events:
                break

            socketio.emit("log", {"message": event["title"]})

            detail_resp = session.get(event["detail_url"], timeout=20)
            detail_resp.raise_for_status()

            detail_data = parse_detail_page(detail_resp.text)
            event.update(detail_data)

            scraped_data.append(event)
            time.sleep(0.5)

        page += 1

    save_csv_to_disk()

    socketio.emit("scraping_done", {"count": len(scraped_data)})
    socketio.emit("log", {"message": f"Scraping completato: {len(scraped_data)} eventi"})


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping(data):
    scrape_events(
        max_pages=int(data.get("max_pages", 5)),
        max_events=int(data.get("max_events", 50))
    )


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    return send_file(
        BytesIO(buffer.getvalue().encode("utf-8")),
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="salto_events_complete.csv"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
