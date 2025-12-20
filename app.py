import eventlet
eventlet.monkey_patch()

import os
import time
import csv
import re
from io import StringIO, BytesIO
from datetime import date

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit

# ================= APP =================

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []

# ================= UTILITY =================

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

def extract_application_deadline(soup):
    text = soup.get_text(" ", strip=True)
    m = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        text
    )
    return m.group(1).strip() if m else ""

# ================= PARSING =================

def parse_list_page(html):
    soup = BeautifulSoup(html, "lxml")
    events = []
    seen = set()

    for link in soup.select("a[href*='/training/']"):
        title = link.get_text(strip=True)
        if not title:
            continue

        url = link.get("href")
        if not url.startswith("http"):
            url = BASE_URL + url

        if url in seen:
            continue
        seen.add(url)

        container = link.find_parent("article") or link.parent
        lines = container.get_text("\n", strip=True).split("\n")

        events.append({
            "title": title,
            "type": lines[0] if len(lines) > 0 else "",
            "dates": lines[2] if len(lines) > 2 else "",
            "location": lines[3] if len(lines) > 3 else "",
            "detail_url": url,
            "application_deadline": ""
        })

    return events

def parse_detail_page(html):
    soup = BeautifulSoup(html, "lxml")

    # ✅ Training summary
    summary_tag = soup.find("div", class_=re.compile("training-summary"))
    training_summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

    # ✅ Training description
    desc_tag = soup.find("div", class_=re.compile("training-description"))
    training_description = desc_tag.get_text("\n", strip=True) if desc_tag else ""

    # Overview (legacy logic)
    overview = soup.find("h3", string=re.compile("Training overview"))
    training_overview = ""
    if overview:
        parts = []
        for sib in overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts)

    return {
        "training_summary": training_summary,
        "training_description": training_description,
        "training_overview": training_overview,
        "application_deadline": extract_application_deadline(soup),
    }

# ================= SCRAPING =================

def scrape_events():
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_dict = {}

    for page in range(50):
        socketio.emit("log", {"message": f"Pagina {page+1}"})
        resp = session.get(build_search_url(page * 10), timeout=20)
        events = parse_list_page(resp.text)
        if not events:
            break

        for e in events:
            events_dict.setdefault(e["detail_url"], e)

        eventlet.sleep(0.5)

    scraped_data = list(events_dict.values())

    for i, event in enumerate(scraped_data, 1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        r = session.get(event["detail_url"], timeout=20)
        event.update(parse_detail_page(r.text))
        eventlet.sleep(0.5)

    save_csv()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ================= CSV =================

CSV_FIELDS = [
    "title","type","dates","location","application_deadline",
    "training_summary","training_description","training_overview",
    "detail_url"
]

def save_csv():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(scraped_data)

# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def start():
    emit("log", {"message": "Avvio scraping"})
    scrape_events()

@app.route("/download_csv")
def download():
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(scraped_data)

    mem = BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="salto_events_complete.csv")

# ================= RUN =================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
