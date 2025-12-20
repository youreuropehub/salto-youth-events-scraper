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

# ================= APPLICATION DEADLINE (FIX DEFINITIVO) =================

def extract_application_deadline(soup: BeautifulSoup) -> str:
    """
    Estrae Application deadline ESCLUSIVAMENTE da:
    <div class="mrgn-btm-22">
        <span class="block call-addendum">Application deadline (24h UTC): DATE</span>
    </div>
    """
    container = soup.find("div", class_="mrgn-btm-22")
    if not container:
        return ""

    spans = container.find_all("span", class_="block call-addendum")
    for span in spans:
        text = span.get_text(" ", strip=True)
        text = re.sub(r"\(.*?\)", "", text)  # rimuove (24h UTC)
        match = re.search(r"Application deadline\s*:\s*(.+)", text)
        if match:
            return match.group(1).strip()

    return ""

# ================= PARSING =================

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_urls = set()

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title:
            continue

        detail_url = link.get("href", "")
        if not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        container = link.find_parent()
        for _ in range(4):
            if container and container.name not in ["body", "html"]:
                container = container.parent

        text_block = container.get_text("\n", strip=True) if container else ""
        lines = [l for l in text_block.split("\n") if l]

        idx = lines.index(title) if title in lines else 0

        events.append({
            "title": title,
            "type": lines[idx - 1] if idx > 0 else "",
            "dates": lines[idx + 1] if idx + 1 < len(lines) else "",
            "location": lines[idx + 2] if idx + 2 < len(lines) else "",
            "application_deadline": "",
            "detail_url": detail_url,
        })

    return events

def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    training_overview = ""
    h3 = soup.find(lambda t: t.name in ["h3", "h4"] and "Training overview" in t.get_text())
    if h3:
        parts = []
        for sib in h3.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts)

    application_deadline = extract_application_deadline(soup)

    return {
        "training_overview": training_overview,
        "application_deadline": application_deadline,
    }

# ================= APPLICATION FORM =================

def get_external_application_link(url):
    if not url:
        return ""
    try:
        soup = BeautifulSoup(requests.get(url, timeout=10).text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(x in a["href"] for x in ["forms.gle", "google.com/forms", "typeform.com"]):
                return a["href"]
    except Exception:
        pass
    return ""

# ================= CSV =================

def save_csv_to_file():
    if not scraped_data:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fields = [
        "title","type","dates","location","application_deadline",
        "training_overview","application_form_link","detail_url"
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato: {path}"})

# ================= SCRAPING =================

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    events_dict = {}
    page = 0

    while page < 50:
        url = build_search_url(page * 10)
        resp = session.get(url, timeout=15)
        events = parse_list_page(resp.text)
        if not events:
            break

        for e in events:
            events_dict[e["detail_url"]] = e

        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    for i, event in enumerate(scraped_data, 1):
        resp = session.get(event["detail_url"], timeout=15)
        detail = parse_detail_page(resp.text, event["detail_url"])
        event.update(detail)
        time.sleep(1)

    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def start():
    scrape_events()

@app.route("/download_csv")
def download_csv():
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    b = BytesIO(buf.getvalue().encode("utf-8"))
    b.seek(0)
    return send_file(b, as_attachment=True, download_name="salto_events_complete.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
