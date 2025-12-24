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

# ================= CONFIG =================

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []

# ================= UTILS =================

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

# ================= LIST PAGE =================

def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue

        title = a.get_text(strip=True)
        detail_url = a["href"]
        if not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        if detail_url in seen:
            continue
        seen.add(detail_url)

        block = h3.parent
        lines = [l.strip() for l in block.get_text("\n", strip=True).split("\n") if l.strip()]

        type_ = ""
        dates = ""
        location = ""
        application_deadline = ""

        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        if idx > 0:
            type_ = lines[idx - 1]
        if idx + 1 < len(lines):
            dates = lines[idx + 1]
        if idx + 2 < len(lines):
            location = lines[idx + 2]

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
            "detail_url": detail_url,
        })

    return events

# ================= DETAIL PAGE =================

def section_after_heading(soup, text):
    h = soup.find(lambda t: t.name in ["h3", "h4"] and text in t.get_text())
    if not h:
        return ""
    parts = []
    for sib in h.find_next_siblings():
        if sib.name and sib.name.startswith("h"):
            break
        parts.append(sib.get_text(" ", strip=True))
    return " ".join(parts).strip()

def parse_detail_page(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # -------- Training summary / description --------
    training_summary = ""
    training_description = ""

    summary_div = soup.select_one(".training-summary")
    if summary_div:
        training_summary = " ".join(summary_div.stripped_strings)

    desc_div = soup.select_one(".training-description")
    if desc_div:
        training_description = " ".join(desc_div.stripped_strings)

    # -------- Training overview --------
    overview_text = section_after_heading(soup, "Training overview")
    lines = [l.strip() for l in overview_text.splitlines() if l.strip()]

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
                participants_from = " ".join(countries).strip()
                i = j
                continue

        if "recommended for" in low and i + 1 < len(lines):
            recommended_for = lines[i + 1]

        if "working language" in low:
            working_language = lines[i].split(":", 1)[-1].strip()

        if low.startswith("organiser"):
            organiser = lines[i].split(":", 1)[-1].strip()

        i += 1

    # -------- Accessibility --------
    accessibility = section_after_heading(soup, "Accessibility info")

    # -------- Costs --------
    participation_fee = section_after_heading(soup, "Participation fee")
    accommodation_food = section_after_heading(soup, "Accommodation and food")
    travel_reimbursement = section_after_heading(soup, "Travel reimbursement")

    # -------- Infopack --------
    infopack_downloads = ""
    for a in soup.select("a[href]"):
        if "download" in a.get_text().lower():
            href = a["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            infopack_downloads = href
            break

    # -------- Application procedure --------
    application_procedure_url = ""
    for a in soup.select("a[href*='/application-procedure/']"):
        href = a["href"]
        if not href.startswith("http"):
            href = BASE_URL + href
        application_procedure_url = href
        break

    return {
        "training_summary": training_summary,
        "training_description": training_description,
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "accessibility": accessibility,
        "working_language": working_language,
        "organiser": organiser,
        "participation_fee": participation_fee,
        "accommodation_food": accommodation_food,
        "travel_reimbursement": travel_reimbursement,
        "infopack_downloads": infopack_downloads,
        "application_procedure_url": application_procedure_url,
    }

# ================= SCRAPER =================

def scrape_events():
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    events_dict = {}
    page = 0

    while True:
        offset = page * 10
        socketio.emit("log", {"message": f"Pagina {page + 1}"})

        resp = session.get(build_search_url(offset), timeout=15)
        resp.raise_for_status()

        events = parse_list_page(resp.text)
        if not events:
            break

        for ev in events:
            if ev["detail_url"] not in events_dict:
                events_dict[ev["detail_url"]] = ev

        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    for i, event in enumerate(scraped_data, 1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        resp = session.get(event["detail_url"], timeout=15)
        detail = parse_detail_page(resp.text)
        event.update(detail)
        time.sleep(1)

    save_csv()

# ================= CSV =================

def save_csv():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fieldnames = list(scraped_data[0].keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def start_scraping():
    scrape_events()
    emit("scraping_done", {"count": len(scraped_data)})

@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato", 400

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    mem = BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)

    return send_file(mem, as_attachment=True, download_name="salto_events_complete.csv")

# ================= MAIN =================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
