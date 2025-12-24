# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
from io import StringIO, BytesIO
from datetime import datetime, date
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net/tools/european-training-calendar"
OUTPUT_DIR = "output"
scraped_data = []

# Helper to parse a date string like "25 December 2025" into a date object
def parse_deadline(date_str):
    try:
        return datetime.strptime(date_str.strip(), "%d %B %Y").date()
    except Exception:
        return None

def scrape_events(max_pages=50, page_size=10):
    global scraped_data
    scraped_data = []
    today = datetime.utcnow().date()
    session = requests.Session()
    session.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    })

    socketio.emit("log", {"message": "Inizio scraping pagine lista..."})
    page = 0
    events_set = set()

    while page < max_pages:
        offset = page * page_size
        url = f"{BASE_URL}/browse/"
        params = {
            'b_limit': page_size,
            'b_offset': offset,
            'b_order': 'applicationDeadline',
            'b_begin_date_after_day': today.day,
            'b_begin_date_after_month': today.month,
            'b_begin_date_after_year': today.year,
            'b_application_deadline_after_day': today.day,
            'b_application_deadline_after_month': today.month,
            'b_application_deadline_after_year': today.year,
        }

        msg = f"Caricamento pagina {page + 1} (offset={offset})..."
        socketio.emit("log", {"message": msg})

        try:
            resp = session.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore caricamento pagina {page+1}: {e}"})
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/tools/european-training-calendar/training/' in href:
                link = requests.compat.urljoin(BASE_URL, href)
                if link not in events_set:
                    links.append(link)
                    events_set.add(link)

        if not links:
            break

        # Fetch details for each event
        for i, link in enumerate(links, start=1):
            try:
                resp_event = session.get(link, timeout=15)
                resp_event.raise_for_status()
                soup_event = BeautifulSoup(resp_event.text, "html.parser")

                # Title
                h1 = soup_event.find('h1')
                title = h1.get_text(strip=True) if h1 else ""

                # Type
                type_tag = h1.find_next('p') if h1 else None
                event_type = type_tag.get_text(strip=True) if type_tag else ""

                # Dates & Location
                dates_location_tag = type_tag.find_next('p') if type_tag else None
                dates, location = "", ""
                if dates_location_tag:
                    text_dl = dates_location_tag.get_text(" ", strip=True)
                    parts = [part.strip() for part in text_dl.split("|")]
                    if len(parts) >= 2:
                        dates, location = parts[0], parts[1]
                    else:
                        dates = parts[0]

                # Summary
                summary_tag = dates_location_tag.find_next('p') if dates_location_tag else None
                training_summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

                # Application deadline
                app_deadline = ""
                apply_link = soup_event.find('a', string=re.compile(r'Application deadline', re.I))
                if apply_link:
                    text = apply_link.get_text()
                    m = re.search(r'Application deadline.*?(\d{1,2} \w+ \d{4})', text)
                    if m:
                        app_deadline = m.group(1)
                deadline_date = parse_deadline(app_deadline)
                if not deadline_date or deadline_date <= today:
                    continue  # skip past deadlines

                # Training description
                training_description_parts = []
                if summary_tag:
                    for sib in summary_tag.find_next_siblings():
                        if sib.name == 'a' and 'Apply now' in sib.get_text():
                            break
                        if sib.name in ['h3','h4','h5','h6'] and 'Disclaimer' in sib.get_text():
                            break
                        text = sib.get_text(" ", strip=True)
                        if text:
                            training_description_parts.append(text)
                training_description = " ".join(training_description_parts)

                scraped_data.append({
                    "title": title,
                    "type": event_type,
                    "dates": dates,
                    "location": location,
                    "application_deadline": app_deadline,
                    "training_summary": training_summary,
                    "training_description": training_description,
                    "detail_url": link
                })

                socketio.emit("log", {"message": f"[{len(scraped_data)}] {title}"})
                time.sleep(0.5)
            except Exception as e:
                socketio.emit("log", {"message": f"Errore evento {link}: {e}"})
                continue
        page += 1
        time.sleep(1)

    # Save CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["title","type","dates","location","application_deadline","training_summary","training_description","detail_url"]
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
    emit("log", {"message": "Avvio scraping..."})
    scrape_events()

@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400
    text_buffer = StringIO()
    fieldnames = ["title","type","dates","location","application_deadline","training_summary","training_description","detail_url"]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in scraped_data:
        writer.writerow(row)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="salto_events_complete.csv")

@app.route("/api/scrape", methods=["POST","GET"])
def api_scrape():
    scrape_events()
    return jsonify({
        "status": "ok",
        "count": len(scraped_data),
        "csv_path": f"{OUTPUT_DIR}/salto_events_complete.csv",
        "message": "Scraping completato. CSV salvato."
    })

@app.route("/api/scrape_and_download", methods=["POST","GET"])
def api_scrape_and_download():
    scrape_events()
    if not scraped_data:
        return jsonify({"status":"error","message":"Nessun dato trovato"}), 400
    text_buffer = StringIO()
    fieldnames = ["title","type","dates","location","application_deadline","training_summary","training_description","detail_url"]
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
