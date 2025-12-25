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

# ================== CONFIG ==================
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_EVENTS = 100
OUTPUT_DIR = "output"

scraped_data = []

# ============================================================
# ================== URL BUILDER =============================
# ============================================================

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

# ============================================================
# ================== LIST PAGE PARSER =========================
# ============================================================

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_urls = set()

    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a:
            continue

        title = a.get_text(strip=True)
        url = a.get("href", "").strip()
        if not url:
            continue
        if not url.startswith("http"):
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
        application_deadline = ""

        for l in lines:
            if "Application deadline" in l:
                application_deadline = l.split(":", 1)[-1].strip()
                break

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": application_deadline,
            "detail_url": url
        })

    return events

# ============================================================
# ================== DETAIL PAGE PARSER =======================
# ============================================================

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

    # Training overview
    training_overview = section("Training overview")
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]

    participants_no = ""
    participants_from = ""
    recommended_for = ""
    working_language = ""
    organiser = ""

    i = 0
    while i < len(lines):
        low = lines[i].lower()

        if low == "for" and i + 1 < len(lines):
            if "participants" in lines[i + 1].lower():
                participants_no = lines[i + 1].replace("participants", "").strip()
                j = i + 2
                countries = []
                while j < len(lines):
                    if lines[j].lower().startswith("and recommended"):
                        break
                    countries.append(lines[j])
                    j += 1
                participants_from = " ".join(countries)
                i = j
                continue

        if "recommended for" in low and i + 1 < len(lines):
            recommended_for = lines[i + 1]

        if "working language" in low:
            working_language = lines[i].split(":", 1)[-1].strip()

        if low.startswith("organiser"):
            organiser = lines[i].split(":", 1)[-1].strip()

        i += 1

    # Downloads
    infopack_downloads = ""
    for tag in soup.find_all(string=re.compile("Available downloads")):
        parent = tag.parent
        link = parent.find_next("a", href=True)
        if link:
            infopack_downloads = link["href"]
            if not infopack_downloads.startswith("http"):
                infopack_downloads = BASE_URL + infopack_downloads
            break

    # Application procedure
    application_procedure_url = ""
    for a in soup.find_all("a", href=True):
        if "/application-procedure/" in a["href"]:
            application_procedure_url = a["href"]
            if not application_procedure_url.startswith("http"):
                application_procedure_url = BASE_URL + application_procedure_url
            break

    return {
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "accessibility": section("Accessibility"),
        "working_language": working_language,
        "organiser": organiser,
        "participation_fee": section("Participation fee"),
        "accommodation_food": section("Accommodation and food"),
        "travel_reimbursement": section("Travel reimbursement"),
        "infopack_downloads": infopack_downloads,
        "application_procedure_url": application_procedure_url
    }

# ============================================================
# ================== EXTERNAL FORM LINK =======================
# ============================================================

def get_external_application_link(url):
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(x in a["href"] for x in [
                "forms.gle", "google.com/forms",
                "typeform.com", "jotform.com"
            ]):
                return a["href"]
    except Exception:
        pass
    return ""

# ============================================================
# ================== CSV SAVE ================================
# ============================================================

def save_csv():
    if not scraped_data:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fieldnames = scraped_data[0].keys()
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato: {path}"})

# ============================================================
# ================== SCRAPER CORE =============================
# ============================================================

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
                    break

        if len(events_dict) >= max_events:
            break

        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    for i, ev in enumerate(scraped_data, start=1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {ev['title']}"})
        resp = session.get(ev["detail_url"], timeout=15)
        resp.raise_for_status()

        detail = parse_detail_page(resp.text)
        ev.update(detail)
        ev["application_form_link"] = get_external_application_link(
            ev.get("application_procedure_url")
        )
        time.sleep(1)

    save_csv()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ============================================================
# ================== ROUTES ==================================
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def start_scraping(data):
    max_pages = int(data.get("max_pages", DEFAULT_MAX_PAGES))
    max_events = int(data.get("max_events", DEFAULT_MAX_EVENTS))
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
    return jsonify({"status": "ok", "count": len(scraped_data)})

# ============================================================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
