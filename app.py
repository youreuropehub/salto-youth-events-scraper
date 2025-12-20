# ================== GEVENT PATCH (OBBLIGATORIO) ==================
from gevent import monkey
monkey.patch_all()

from gevent import sleep
import os
import csv
import re
from io import StringIO, BytesIO
from datetime import date

import requests
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup


# ================== APP ==================
app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="gevent",
    ping_interval=10,
    ping_timeout=120
)

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []


# ================== UTILITY ==================
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


def extract_application_deadline(soup: BeautifulSoup) -> str:
    text = soup.get_text(" ", strip=True)
    m = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        text
    )
    return m.group(1).strip() if m else ""


# ================== PARSING ==================
def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = {}
    for a in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = a.get_text(strip=True)
        if not title:
            continue

        url = a["href"]
        if not url.startswith("http"):
            url = BASE_URL + url

        events.setdefault(url, {
            "title": title,
            "type": "",
            "dates": "",
            "location": "",
            "application_deadline": "",
            "training_overview": "",
            "participants_no": "",
            "participants_from": "",
            "recommended_for": "",
            "accessibility": "",
            "working_language": "",
            "organiser": "",
            "participation_fee": "",
            "accommodation_food": "",
            "travel_reimbursement": "",
            "infopack_downloads": "",
            "application_procedure_url": "",
            "application_form_link": "",
            "detail_url": url
        })
    return list(events.values())


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
        return " ".join(parts).strip()

    return {
        "training_overview": section("Training overview"),
        "accessibility": section("Accessibility info"),
        "participation_fee": section("Participation fee"),
        "accommodation_food": section("Accommodation and food"),
        "travel_reimbursement": section("Travel reimbursement"),
        "application_deadline": extract_application_deadline(soup),
    }


def get_external_application_link(url):
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(x in a["href"] for x in [
                "forms.gle", "google.com/forms",
                "typeform.com", "surveymonkey.com", "jotform.com"
            ]):
                return a["href"]
    except Exception:
        pass
    return ""


# ================== SCRAPING ==================
def log_event_fields(event):
    socketio.emit("log", {"message": "----- EVENTO -----"})
    for k, v in event.items():
        socketio.emit("log", {"message": f"{k}: {v}"})
        sleep(0)
    socketio.emit("log", {"message": "------------------"})


def scrape_events():
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_map = {}
    page = 0

    while page < 50:
        socketio.emit("log", {"message": f"Caricamento pagina {page + 1}"})
        sleep(0)

        try:
            r = session.get(build_search_url(page * 10), timeout=20)
            r.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore pagina: {e}"})
            break

        events = parse_list_page(r.text)
        if not events:
            break

        for e in events:
            events_map.setdefault(e["detail_url"], e)

        page += 1
        sleep(1)

    scraped_data = list(events_map.values())
    socketio.emit("log", {"message": f"Totale eventi trovati: {len(scraped_data)}"})

    for i, ev in enumerate(scraped_data, 1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {ev['title']}"})
        sleep(0)

        try:
            r = session.get(ev["detail_url"], timeout=20)
            r.raise_for_status()
            ev.update(parse_detail_page(r.text))
        except Exception as e:
            socketio.emit("log", {"message": f"Errore dettaglio: {e}"})

        log_event_fields(ev)
        sleep(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=scraped_data[0].keys())
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": "SCRAPING COMPLETATO"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ================== ROUTES ==================
@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def start_scraping():
    emit("log", {"message": "Avvio scraping..."})
    socketio.start_background_task(scrape_events)


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato", 400

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    bio = BytesIO(buffer.getvalue().encode("utf-8"))
    bio.seek(0)
    return send_file(
        bio,
        as_attachment=True,
        download_name="salto_events_complete.csv",
        mimetype="text/csv; charset=utf-8"
    )


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
