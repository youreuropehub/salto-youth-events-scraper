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


def build_search_url(offset: int) -> str:
    today = date.today()
    day, month, year = today.day, today.month, today.year

    base = (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        "?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        "&b_keyword="
        "&b_begin_date_after_day={day}&b_begin_date_after_month={month}&b_begin_date_after_year={year}"
        "&b_application_deadline_after_day={day}&b_application_deadline_after_month={month}&b_application_deadline_after_year={year}"
    )
    return base.format(offset=offset, day=day, month=month, year=year)


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
        lines = [l.strip() for l in block.get_text("\n", strip=True).split("\n") if l.strip()]
        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        type_, dates, location, app_deadline = "", "", "", ""
        if idx > 0:
            type_ = lines[idx - 1]
        if idx + 1 < len(lines):
            dates = lines[idx + 1]
        if idx + 2 < len(lines):
            location = lines[idx + 2]
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
            "detail_url": url
        })

    return events


def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    # SUMMARY e DESCRIPTION
    training_summary = ""
    training_description = ""

    summary_tag = soup.select_one(".training-summary")
    if summary_tag:
        training_summary = summary_tag.get_text(" ", strip=True)

    description_tag = soup.select_one(".training-description")
    if description_tag:
        training_description = description_tag.get_text("\n", strip=True)

    # Application deadline (regex robusto)
    application_deadline = ""
    ad_tag = soup.find(string=re.compile(r"Application deadline", re.I))
    if ad_tag:
        m = re.search(r"Application deadline\s*:?\s*(.*)", ad_tag, re.I)
        if m:
            application_deadline = m.group(1).strip()

    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    training_overview_tag = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if training_overview_tag:
        parts = []
        for sib in training_overview_tag.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        overview_lines = [l.strip() for l in "\n".join(parts).splitlines() if l.strip()]
        i = 0
        while i < len(overview_lines):
            line = overview_lines[i].lower()
            if "participants" in line and "for" in line:
                participants_no = overview_lines[i+1] if i+1 < len(overview_lines) else ""
            if "from" in line:
                j = i+1
                countries = []
                while j < len(overview_lines) and not overview_lines[j].lower().startswith("and recommended"):
                    countries.append(overview_lines[j])
                    j += 1
                participants_from = ", ".join(countries).strip()
                i = j-1
            if "recommended for" in line:
                recommended_for = overview_lines[i+1] if i+1 < len(overview_lines) else ""
            if "working language" in line:
                working_lang = overview_lines[i].split(":",1)[-1].strip()
            if line.startswith("organiser"):
                organiser = overview_lines[i].split(":",1)[-1].strip()
            i += 1

    # Accessibility, costs, downloads, application URL
    def get_section_text(title):
        h = soup.find(lambda tag: tag.name in ["h3","h4"] and title in tag.get_text())
        if not h:
            return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        return " ".join(parts).strip()

    accessibility = get_section_text("Accessibility info")
    participation_fee = get_section_text("Participation fee")
    accommodation_food = get_section_text("Accommodation and food")
    travel_reimbursement = get_section_text("Travel reimbursement")

    # Available downloads
    infopack_downloads = ""
    dl_heading = next((tag for tag in soup.find_all(['h3','h4','p','strong','b']) if "Available downloads" in tag.get_text()), None)
    if dl_heading:
        for sib in dl_heading.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            link = sib.find("a", href=True)
            if link:
                infopack_downloads = link["href"] if link["href"].startswith("http") else BASE_URL + link["href"]
                break

    # Application procedure
    application_procedure_url = ""
    for a in soup.find_all("a", href=True):
        if "/application-procedure/" in a["href"]:
            application_procedure_url = a["href"] if a["href"].startswith("http") else BASE_URL + a["href"]
            break

    # External application form
    def get_external_link(url):
        if not url:
            return ""
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            soup2 = BeautifulSoup(resp.text, "html.parser")
            a_tag = soup2.find("a", string=re.compile(r"Proceed to the external", re.I))
            if a_tag and a_tag.get("href"):
                return a_tag["href"]
            # fallback common form domains
            for a in soup2.find_all("a", href=True):
                if any(d in a["href"] for d in ["forms.gle","typeform.com","surveymonkey.com","jotform.com"]):
                    return a["href"]
            return ""
        except:
            return ""

    application_form_link = get_external_link(application_procedure_url)

    return {
        "training_summary": training_summary,
        "training_description": training_description,
        "application_deadline": application_deadline,
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "accessibility": accessibility,
        "working_language": working_lang,
        "organiser": organiser,
        "participation_fee": participation_fee,
        "accommodation_food": accommodation_food,
        "travel_reimbursement": travel_reimbursement,
        "infopack_downloads": infopack_downloads,
        "application_procedure_url": application_procedure_url,
        "application_form_link": application_form_link
    }


def scrape_events(max_pages=50, max_events=None):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    })

    socketio.emit("log", {"message": "Inizio scraping pagine lista..."})

    events_dict = {}
    page = 0
    page_size = 10

    while page < max_pages:
        offset = page * page_size
        url = build_search_url(offset)
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore caricamento pagina {page+1}: {e}"})
            break

        events = parse_list_page(resp.text)
        if not events:
            break

        for event in events:
            detail_url = event.get("detail_url")
            if detail_url and detail_url not in events_dict:
                events_dict[detail_url] = event
            if max_events and len(events_dict) >= max_events:
                break
        if max_events and len(events_dict) >= max_events:
            break
        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    for i, event in enumerate(scraped_data, start=1):
        detail_url = event.get("detail_url")
        if not detail_url:
            continue
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        try:
            resp = session.get(detail_url, timeout=15)
            resp.raise_for_status()
            detail_data = parse_detail_page(resp.text, detail_url)
            event.update(detail_data)
        except Exception as e:
            socketio.emit("log", {"message": f"Errore dettaglio {detail_url}: {e}"})
            for key in ["training_summary","training_description","application_deadline",
                        "participants_no","participants_from","recommended_for","accessibility",
                        "working_language","organiser","participation_fee","accommodation_food",
                        "travel_reimbursement","infopack_downloads","application_procedure_url","application_form_link"]:
                event[key] = ""
        time.sleep(1)

    # save CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description",
        "participants_no","participants_from","recommended_for","accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"Scraping completato! Totale: {len(scraped_data)} eventi"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ================== ROUTES ==================
@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping(data=None):
    max_pages = int(data.get("max_pages",50)) if data else 50
    max_events = int(data.get("max_events",0)) if data else None
    emit("log", {"message": "Avvio scraping..."})
    scrape_events(max_pages=max_pages, max_events=max_events)


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description",
        "participants_no","participants_from","recommended_for","accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in scraped_data:
        writer.writerow(row)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="salto_events_complete.csv")


@app.route("/api/scrape", methods=["POST", "GET"])
def api_scrape():
    max_pages = int(request.args.get("max_pages",50))
    max_events = int(request.args.get("max_events",0)) or None
    scrape_events(max_pages=max_pages, max_events=max_events)
    return jsonify({
        "status": "ok",
        "count": len(scraped_data),
        "csv_path": f"{OUTPUT_DIR}/salto_events_complete.csv",
        "message": "Scraping completato. CSV salvato."
    })


@app.route("/api/scrape_and_download", methods=["POST", "GET"])
def api_scrape_and_download():
    max_pages = int(request.args.get("max_pages",50))
    max_events = int(request.args.get("max_events",0)) or None
    scrape_events(max_pages=max_pages, max_events=max_events)
    if not scraped_data:
        return jsonify({"status":"error","message":"Nessun dato trovato"}),400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description",
        "participants_no","participants_from","recommended_for","accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in scraped_data:
        writer.writerow(row)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="salto_events_complete.csv")


if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
