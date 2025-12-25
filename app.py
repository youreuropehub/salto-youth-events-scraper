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

# ================= CONFIGURAZIONE DEFAULT =================
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_EVENTS = 100
OUTPUT_DIR = "output"

# Variabile globale
scraped_data = []


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


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title:
            continue

        url = link["href"]
        if not url.startswith("http"):
            url = BASE_URL + url

        if url in seen:
            continue
        seen.add(url)

        container = link.find_parent("div")
        text = container.get_text("\n", strip=True) if container else ""
        lines = text.split("\n")

        events.append({
            "title": title,
            "type": lines[0] if len(lines) > 0 else "",
            "dates": lines[1] if len(lines) > 1 else "",
            "location": lines[2] if len(lines) > 2 else "",
            "application_deadline": "",
            "detail_url": url
        })

    return events


def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")

    def section(title):
        h = soup.find(lambda t: t.name in ["h3", "h4"] and title in t.get_text())
        if not h:
            return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        return " ".join(parts)

    return {
        "participants_no": "",
        "participants_from": "",
        "recommended_for": "",
        "accessibility": section("Accessibility"),
        "working_language": "",
        "organiser": "",
        "participation_fee": section("Participation fee"),
        "accommodation_food": section("Accommodation and food"),
        "travel_reimbursement": section("Travel reimbursement"),
        "infopack_downloads": "",
        "application_procedure_url": "",
        "application_form_link": ""
    }


def save_csv():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fields = list(scraped_data[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato: {path}"})


def scrape_events(max_pages, max_events):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    events_dict = {}

    page = 0
    page_size = 10

    while page < max_pages:
        offset = page * page_size
        socketio.emit("log", {"message": f"Pagina {page + 1} (offset {offset})"})

        resp = session.get(build_search_url(offset), timeout=15)
        resp.raise_for_status()

        events = parse_list_page(resp.text)
        if not events:
            break

        for ev in events:
            if ev["detail_url"] not in events_dict:
                events_dict[ev["detail_url"]] = ev

                if len(events_dict) >= max_events:
                    socketio.emit("log", {"message": "Limite eventi raggiunto"})
                    break

        if len(events_dict) >= max_events:
            break

        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())
    socketio.emit("log", {"message": f"Eventi lista: {len(scraped_data)}"})

    for i, ev in enumerate(scraped_data, start=1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {ev['title']}"})
        resp = session.get(ev["detail_url"], timeout=15)
        resp.raise_for_status()
        ev.update(parse_detail_page(resp.text))
        time.sleep(1)

    save_csv()
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def start_scraping(data):
    max_pages = int(data.get("max_pages", DEFAULT_MAX_PAGES))
    max_events = int(data.get("max_events", DEFAULT_MAX_EVENTS))

    emit("log", {"message": f"Avvio scraping: {max_pages} pagine / {max_events} eventi"})
    scrape_events(max_pages, max_events)


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

    return send_file(mem, as_attachment=True,
                     download_name="salto_events_complete.csv",
                     mimetype="text/csv")


@app.route("/api/scrape", methods=["GET", "POST"])
def api_scrape():
    max_pages = int(request.values.get("max_pages", DEFAULT_MAX_PAGES))
    max_events = int(request.values.get("max_events", DEFAULT_MAX_EVENTS))

    scrape_events(max_pages, max_events)

    return jsonify({
        "status": "ok",
        "count": len(scraped_data),
        "csv": f"{OUTPUT_DIR}/salto_events_complete.csv"
    })


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
