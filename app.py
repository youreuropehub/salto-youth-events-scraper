# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
scraped_data = []
OUTPUT_DIR = "output"


# ================= Selenium Setup =================
def setup_selenium():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    return driver


# ================= Helper Functions =================
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

    # Training overview
    training_summary, training_description = "", ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_summary = "\n".join(parts).strip()

    # Full description
    desc_div = soup.find("div", class_="training-description")
    if desc_div:
        training_description = desc_div.get_text("\n", strip=True)

    # Application deadline
    application_deadline = ""
    for l in soup.find_all(string=lambda s: "Application deadline" in s):
        application_deadline = l.split(":", 1)[-1].strip()
        break

    # Participants, languages, organiser
    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = training_summary.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].lower()
        if "participants" in line:
            participants_no = lines[i].replace("participants", "").strip()
        if "from" in line:
            participants_from = lines[i].replace("from", "").strip()
        if "recommended for" in line:
            recommended_for = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if "working language" in line:
            working_lang = lines[i].split(":", 1)[-1].strip()
        if "organiser" in line:
            organiser = lines[i].split(":", 1)[-1].strip()
        i += 1

    # Downloads
    infopack_downloads = ""
    downloads_heading = next((tag for tag in soup.find_all(['h3', 'h4', 'h5', 'strong', 'b', 'p'])
                              if "Available downloads:" in tag.get_text()), None)
    if downloads_heading:
        first_link = downloads_heading.find_next("a", href=True)
        if first_link:
            href = first_link["href"]
            infopack_downloads = href if href.startswith("http") else BASE_URL + href

    # Application procedure
    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_href = link["href"]
            application_procedure_url = app_href if app_href.startswith("http") else BASE_URL + app_href
            break

    # Participation, accommodation, travel costs
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

    participation_fee = section_after_heading("Participation fee")
    accommodation_food = section_after_heading("Accommodation and food")
    travel_reimbursement = section_after_heading("Travel reimbursement")

    return {
        "training_summary": training_summary,
        "training_description": training_description,
        "application_deadline": application_deadline,
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "working_language": working_lang,
        "organiser": organiser,
        "participation_fee": participation_fee,
        "accommodation_food": accommodation_food,
        "travel_reimbursement": travel_reimbursement,
        "infopack_downloads": infopack_downloads,
        "application_procedure_url": application_procedure_url,
    }


def get_external_application_link(application_procedure_url):
    if not application_procedure_url:
        return ""
    try:
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        external_link = soup.find("a", string=lambda t: t and "Proceed to the external" in t)
        if external_link and external_link.get("href"):
            return external_link["href"]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(domain in href for domain in ["forms.gle","google.com/forms","typeform.com","surveymonkey.com","jotform.com"]):
                return href
        return ""
    except Exception as e:
        print(f"Error fetching application link from {application_procedure_url}: {e}")
        return ""


def save_csv_to_file():
    if not scraped_data:
        print("DEBUG: nessun dato da salvare")
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description",
        "participants_no","participants_from","recommended_for",
        "working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})


# ================= Scraping Logic =================
def scrape_events(max_pages=50):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    })

    socketio.emit("log", {"message": "Inizio scraping pagine lista..."})
    events_dict = {}
    page, page_size = 0, 10

    while page < max_pages:
        offset = page * page_size
        socketio.emit("log", {"message": f"Caricamento pagina {page + 1} (offset={offset})..."})
        try:
            url = build_search_url(offset)
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore caricamento pagina {page + 1}: {e}"})
            break

        events = parse_list_page(resp.text)
        if not events:
            break

        for event in events:
            detail_url = event.get("detail_url", "")
            if detail_url and detail_url not in events_dict:
                events_dict[detail_url] = event
        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    driver = setup_selenium()

    for i, event in enumerate(scraped_data, start=1):
        detail_url = event.get("detail_url", "")
        if not detail_url:
            continue
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        try:
            driver.get(detail_url)
            html = driver.page_source
            detail = parse_detail_page(html, detail_url)
            if detail["application_procedure_url"]:
                detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
            else:
                detail["application_form_link"] = ""
            event.update(detail)
        except Exception as e:
            print(f"DEBUG: errore dettaglio {detail_url}: {e}")
            for key in ["training_summary","training_description","application_deadline",
                        "participants_no","participants_from","recommended_for","working_language","organiser",
                        "participation_fee","accommodation_food","travel_reimbursement","infopack_downloads",
                        "application_procedure_url","application_form_link"]:
                event[key] = ""
        time.sleep(1)

    driver.quit()
    save_csv_to_file()
    socketio.emit("log", {"message": f"Scraping completato! Totale: {len(scraped_data)} eventi"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ================== ROUTES ==================
@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping(data=None):
    socketio.start_background_task(scrape_events)


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description",
        "participants_no","participants_from","recommended_for",
        "working_language","organiser",
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
    socketio.start_background_task(scrape_events)
    return jsonify({"status": "ok", "message": "Scraping avviato"})


@app.route("/api/scrape_and_download", methods=["POST", "GET"])
def api_scrape_and_download():
    scrape_events()
    if not scraped_data:
        return jsonify({"status":"error","message":"Nessun dato trovato"}),400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description",
        "participants_no","participants_from","recommended_for",
        "working_language","organiser",
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
