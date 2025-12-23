# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
from io import StringIO, BytesIO
from flask import Flask, render_template, send_file
from flask_socketio import SocketIO, emit
from datetime import date
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"
scraped_data = []

# ---------- Playwright scraping ----------
def scrape_events_playwright(max_pages=50):
    global scraped_data
    scraped_data = []

    socketio.emit("log", {"message": f"Avvio scraping con Playwright..."})
    print("DEBUG: Avvio scraping con Playwright...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_size = 10
        current_offset = 0
        events_dict = {}

        while current_offset < max_pages * page_size:
            search_url = f"{BASE_URL}/tools/european-training-calendar/browse/?b_offset={current_offset}&b_limit={page_size}"
            print(f"DEBUG: Caricamento pagina lista: {search_url}")
            socketio.emit("log", {"message": f"Caricamento pagina offset={current_offset}"})
            page.goto(search_url, timeout=60000)
            time.sleep(2)  # attendi caricamento JS

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Trova tutti i blocchi evento
            event_links = soup.select("a[href*='/tools/european-training-calendar/training/']")
            if not event_links:
                print("DEBUG: Nessun evento trovato, fine paginazione")
                break

            for link in event_links:
                title = link.get_text(strip=True)
                detail_url = link.get("href")
                if detail_url and not detail_url.startswith("http"):
                    detail_url = BASE_URL + detail_url

                if detail_url in events_dict:
                    continue

                block = link.find_parent()
                for _ in range(4):
                    if block and block.name not in ["body", "html"]:
                        block = block.parent

                # Application deadline
                app_deadline = ""
                callout = block.select_one("div.callout-module")
                if callout:
                    p_tags = callout.find_all("p")
                    for i, p in enumerate(p_tags):
                        if "Application deadline" in p.get_text(strip=True):
                            if i + 1 < len(p_tags):
                                app_deadline = p_tags[i + 1].get_text(strip=True)
                            break

                # Dates, type, location
                lines = [l.strip() for l in block.get_text("\n", strip=True).split("\n") if l.strip()]
                try:
                    idx = lines.index(title)
                except ValueError:
                    idx = 0
                type_ = lines[idx - 1] if idx - 1 >= 0 else ""
                dates = lines[idx + 1] if idx + 1 < len(lines) else ""
                location = lines[idx + 2] if idx + 2 < len(lines) else ""

                events_dict[detail_url] = {
                    "title": title,
                    "type": type_,
                    "dates": dates,
                    "location": location,
                    "application_deadline": app_deadline,
                    "detail_url": detail_url,
                }
                print(f"DEBUG: Evento trovato: {title} - deadline: {app_deadline}")

            current_offset += page_size
            time.sleep(1)

        # Visita ogni dettaglio
        for i, event in enumerate(events_dict.values(), start=1):
            detail_url = event["detail_url"]
            socketio.emit("log", {"message": f"[{i}/{len(events_dict)}] Caricamento dettaglio: {event['title']}"})
            print(f"DEBUG: [{i}/{len(events_dict)}] Caricamento dettaglio: {event['title']}")
            page.goto(detail_url, timeout=60000)
            time.sleep(2)
            soup = BeautifulSoup(page.content(), "html.parser")

            # Training description
            desc_div = soup.select_one("div.training-description")
            training_description = desc_div.get_text("\n", strip=True) if desc_div else ""

            event.update({
                "training_description": training_description,
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
            })

            time.sleep(1)

        browser.close()
        scraped_data = list(events_dict.values())

    socketio.emit("log", {"message": f"Scraping completato! Totale eventi: {len(scraped_data)}"})
    print(f"DEBUG: Scraping completato! Totale eventi: {len(scraped_data)}")

# ---------- CSV ----------
def save_csv():
    if not scraped_data:
        print("DEBUG: Nessun dato da salvare")
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for",
        "accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link",
        "training_description","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)
    print(f"DEBUG: CSV salvato in {csv_path}")
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})

# ---------- Flask Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping():
    socketio.emit("log", {"message": "Avvio scraping..."})
    scrape_events_playwright()
    save_csv()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400
    from io import StringIO, BytesIO
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for",
        "accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link",
        "training_description","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(scraped_data)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer, mimetype="text/csv", as_attachment=True, download_name="salto_events_complete.csv")

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
