# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file, request
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
scraped_data = []
OUTPUT_DIR = "output"

DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_EVENTS = 1000


def build_search_url(offset: int) -> str:
    today = date.today()
    return (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        "&b_keyword="
        f"&b_begin_date_after_day={today.day}"
        f"&b_begin_date_after_month={today.month}"
        f"&b_begin_date_after_year={today.year}"
        f"&b_application_deadline_after_day={today.day}"
        f"&b_application_deadline_after_month={today.month}"
        f"&b_application_deadline_after_year={today.year}"
    )


# ===================== PARSE LIST PAGE =====================
def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        url = a.get("href", "").strip()
        if url and not url.startswith("http"):
            url = BASE_URL + url
        if url in seen_urls:
            continue
        seen_urls.add(url)

        block = h3.parent
        text = block.get_text("\n", strip=True)
        lines = [l for l in text.split("\n") if l.strip()]

        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        type_ = lines[idx - 1] if idx > 0 else ""
        dates = lines[idx + 1] if idx + 1 < len(lines) else ""
        location = lines[idx + 2] if idx + 2 < len(lines) else ""

        app_deadline = ""
        for l in lines:
            if "Application deadline" in l:
                app_deadline = l.split(":", 1)[-1].strip()
                break

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": app_deadline,
            "detail_url": url,
        })

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title:
            continue
        detail_url = link.get("href", "").strip()
        if detail_url and not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        container = link.find_parent()
        for _ in range(4):
            if container and container.name not in ["body", "html"]:
                container = container.parent
        text_block = container.get_text("\n", strip=True) if container else ""
        lines = [l.strip() for l in text_block.split("\n") if l.strip()]

        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        type_ = lines[idx - 1] if idx > 0 else ""
        dates = lines[idx + 1] if idx + 1 < len(lines) else ""
        location = lines[idx + 2] if idx + 2 < len(lines) else ""

        app_deadline = ""
        for i, l in enumerate(lines):
            if "Application deadline" in l:
                if i + 1 < len(lines):
                    app_deadline = lines[i + 1]
                break

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": app_deadline,
            "detail_url": detail_url,
        })

    return events


# ===================== PARSE DETAIL PAGE =====================
def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    summary_div = soup.find("div", class_=re.compile(r"training-summary"))
    training_summary = summary_div.get_text("\n", strip=True) if summary_div else ""

    description_div = soup.find("div", class_=re.compile(r"training-description"))
    training_description = description_div.get_text("\n", strip=True) if description_div else ""

    return {
        "training_summary": training_summary,
        "training_description": training_description,
    }


def save_csv_to_file():
    if not scraped_data:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=scraped_data[0].keys())
        writer.writeheader()
        writer.writerows(scraped_data)
    socketio.emit("log", {"message": f"CSV salvato in {path}"})


# ===================== SCRAPER =====================
def scrape_events(max_pages=DEFAULT_MAX_PAGES, max_events=DEFAULT_MAX_EVENTS):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    events_dict = {}

    page = 0
    page_size = 10

    while page < max_pages:
        offset = page * page_size
        socketio.emit("log", {"message": f"Pagina {page + 1} (offset {offset})"})

        resp = session.get(build_search_url(offset), timeout=15)
        events = parse_list_page(resp.text)
        if not events:
            break

        for event in events:
            url = event["detail_url"]
            if url not in events_dict:
                events_dict[url] = event
                if len(events_dict) >= max_events:
                    break

        if len(events_dict) >= max_events:
            break

        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    for i, event in enumerate(scraped_data, start=1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        resp = session.get(event["detail_url"], timeout=15)
        detail = parse_detail_page(resp.text, event["detail_url"])
        event.update(detail)
        time.sleep(1)

    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ===================== ROUTES =====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["GET", "POST"])
def api_scrape():
    scrape_events(
        int(request.values.get("max_pages", DEFAULT_MAX_PAGES)),
        int(request.values.get("max_events", DEFAULT_MAX_EVENTS))
    )
    return jsonify({"status": "ok", "count": len(scraped_data)})


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato", 400

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    mem = BytesIO(buffer.getvalue().encode("utf-8"))
    mem.seek(0)

    return send_file(
        mem,
        as_attachment=True,
        download_name="salto_events_complete.csv",
        mimetype="text/csv"
    )


# ===================== SCRAPE + DOWNLOAD (UNA SOLA CHIAMATA) =====================
@app.route("/scrape_and_download", methods=["GET", "POST"])
def scrape_and_download():
    scrape_events(
        int(request.values.get("max_pages", DEFAULT_MAX_PAGES)),
        int(request.values.get("max_events", DEFAULT_MAX_EVENTS))
    )

    if not scraped_data:
        return "Nessun dato", 400

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    mem = BytesIO(buffer.getvalue().encode("utf-8"))
    mem.seek(0)

    return send_file(
        mem,
        as_attachment=True,
        download_name="salto_events_complete.csv",
        mimetype="text/csv"
    )


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
