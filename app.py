# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

from gevent import sleep
import os
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
    day, month, year = today.day, today.month, today.year

    base = (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        "?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        "&b_keyword="
        "&b_begin_date_after_day={day}"
        "&b_begin_date_after_month={month}"
        "&b_begin_date_after_year={year}"
        "&b_begin_date_before_day="
        "&b_begin_date_before_month="
        "&b_begin_date_before_year="
        "&b_end_date_after_day="
        "&b_end_date_after_month="
        "&b_end_date_after_year="
        "&b_end_date_before_day="
        "&b_end_date_before_month="
        "&b_end_date_before_year="
        "&b_activity_type="
        "&b_country="
        "&b_participating_countries="
        "&b_application_deadline_after_day={day}"
        "&b_application_deadline_after_month={month}"
        "&b_application_deadline_after_year={year}"
        "&b_application_deadline_before_day="
        "&b_application_deadline_before_month="
        "&b_application_deadline_before_year="
    )

    return base.format(offset=offset, day=day, month=month, year=year)


def extract_application_deadline(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(class_=re.compile(r"mrgn-btm")):
        text = tag.get_text(" ", strip=True)
        match = re.search(
            r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            text,
        )
        if match:
            return match.group(1).strip()

    full_text = soup.get_text(" ", strip=True)
    match = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        full_text,
    )
    return match.group(1).strip() if match else ""


# ================= PARSING =================

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

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

        events.append({
            "title": title,
            "type": lines[idx - 1] if idx - 1 >= 0 else "",
            "dates": lines[idx + 1] if idx + 1 < len(lines) else "",
            "location": lines[idx + 2] if idx + 2 < len(lines) else "",
            "application_deadline": "",
            "detail_url": detail_url,
        })

    return events


def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")

    def section_after_heading(text):
        h = soup.find(lambda tag: tag.name in ["h3", "h4"] and text in tag.get_text())
        if not h:
            return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        return " ".join(parts).strip()

    return {
        "training_overview": section_after_heading("Training overview"),
        "accessibility": section_after_heading("Accessibility info"),
        "participation_fee": section_after_heading("Participation fee"),
        "accommodation_food": section_after_heading("Accommodation and food"),
        "travel_reimbursement": section_after_heading("Travel reimbursement"),
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
                "forms.gle", "google.com/forms", "typeform.com",
                "surveymonkey.com", "jotform.com"
            ]):
                return a["href"]
    except Exception:
        pass
    return ""


# ================= SCRAPING =================

def scrape_events():
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_dict = {}
    page = 0

    while page < 50:
        offset = page * 10
        socketio.emit("log", {"message": f"Caricamento pagina {page + 1}"})

        try:
            resp = session.get(build_search_url(offset), timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore pagina {page + 1}: {e}"})
            break

        events = parse_list_page(resp.text)
        if not events:
            break

        for ev in events:
            events_dict.setdefault(ev["detail_url"], ev)

        page += 1
        sleep(1)

    scraped_data = list(events_dict.values())
    socketio.emit("log", {"message": f"Totale eventi: {len(scraped_data)}"})

    for i, ev in enumerate(scraped_data, 1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {ev['title']}"})
        try:
            r = session.get(ev["detail_url"], timeout=15)
            r.raise_for_status()
            detail = parse_detail_page(r.text)
            ev.update(detail)
        except Exception:
            pass
        sleep(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=scraped_data[0].keys())
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ================= ROUTES =================

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
    return send_file(bio, as_attachment=True,
                     download_name="salto_events_complete.csv",
                     mimetype="text/csv; charset=utf-8")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
