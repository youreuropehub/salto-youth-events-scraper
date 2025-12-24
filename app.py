# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []


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


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for row in soup.select(".search-results-list .tool-item"):
        a = row.select_one("h2 a")
        if not a:
            continue

        url = a.get("href")
        if not url.startswith("http"):
            url = BASE_URL + url

        if url in seen:
            continue
        seen.add(url)

        events.append({
            "title": a.get_text(strip=True),
            "type": (row.select_one(".tool-item-category") or {}).get_text(strip=True),
            "dates": (row.select_one("p.h5") or {}).get_text(strip=True),
            "location": (row.select_one(".microcopy") or {}).get_text(strip=True),
            "detail_url": url,
            "application_deadline": ""
        })

    return events


def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")

    # --- Application deadline (stile Scrapy) ---
    application_deadline = ""
    for el in soup.select(".call-addendum"):
        txt = el.get_text(" ", strip=True)
        if txt.lower().startswith("application deadline"):
            application_deadline = txt.split(":", 1)[-1].strip()
            break

    # --- Training summary ---
    training_summary = " ".join(
        t.strip() for t in soup.select_one(".training-summary")?.stripped_strings
    ) if soup.select_one(".training-summary") else ""

    # --- Training description ---
    desc_block = soup.select_one(".training-description")
    training_description = " ".join(
        t.strip() for t in desc_block.stripped_strings
    ) if desc_block else ""

    return {
        "application_deadline": application_deadline,
        "training_summary": training_summary,
        "training_description": training_description
    }


def save_csv():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fields = [
        "title", "type", "dates", "location",
        "application_deadline",
        "training_summary", "training_description",
        "detail_url"
    ]

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato: {path}"})


def scrape_events(max_pages=5, max_events=50):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    page = 0
    while page < max_pages and len(scraped_data) < max_events:
        offset = page * 10
        socketio.emit("log", {"message": f"Pagina {page + 1} (offset {offset})"})
        resp = session.get(build_search_url(offset), timeout=15)
        events = parse_list_page(resp.text)

        if not events:
            break

        for e in events:
            if len(scraped_data) >= max_events:
                break

            d = session.get(e["detail_url"], timeout=15)
            detail = parse_detail_page(d.text)
            e.update(detail)
            scraped_data.append(e)
            socketio.emit("log", {"message": e["title"]})
            time.sleep(0.5)

        page += 1

    save_csv()
    socketio.emit("scraping_done", {"count": len(scraped_data)})


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def start_scraping(data):
    scrape_events(
        max_pages=int(data.get("max_pages", 5)),
        max_events=int(data.get("max_events", 50))
    )


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato", 400

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    return send_file(
        BytesIO(buf.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name="salto_events_complete.csv",
        mimetype="text/csv"
    )


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
