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

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
scraped_data = []
OUTPUT_DIR = "output"


def build_search_url(offset: int) -> str:
    today = date.today()
    day, month, year = today.day, today.month, today.year
    return (
        f"https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10"
        f"&b_order=applicationDeadline"
        f"&b_keyword="
        f"&b_begin_date_after_day={day}&b_begin_date_after_month={month}&b_begin_date_after_year={year}"
        f"&b_application_deadline_after_day={day}&b_application_deadline_after_month={month}&b_application_deadline_after_year={year}"
    )


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_urls = set()

    for article in soup.select(".search-result-list .tool-item"):
        title_tag = article.select_one("h2 a")
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)
        url = title_tag.get("href", "")
        if url and not url.startswith("http"):
            url = BASE_URL + url
        if url in seen_urls:
            continue
        seen_urls.add(url)

        type_ = article.select_one(".tool-item-category")
        type_ = type_.get_text(strip=True) if type_ else ""

        dates = article.select_one(".training-dates")
        dates = dates.get_text(strip=True) if dates else ""

        location = article.select_one(".training-location")
        location = location.get_text(strip=True) if location else ""

        app_deadline = ""
        deadline_tag = article.find(lambda tag: "Application deadline" in tag.get_text())
        if deadline_tag:
            app_deadline = deadline_tag.get_text(strip=True).split(":", 1)[-1].strip()

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

    # Summary e description
    summary = ""
    description = ""
    sum_section = soup.select_one(".training-summary")
    desc_section = soup.select_one(".training-description")
    if sum_section:
        summary = sum_section.get_text(" ", strip=True)
    if desc_section:
        description = desc_section.get_text(" ", strip=True)

    # Training overview
    participants_no = participants_from = recommended_for = working_language = organiser = ""
    overview_h3 = soup.find(lambda tag: tag.name == "h3" and "Training overview" in tag.get_text())
    if overview_h3:
        overview_text = "\n".join([sib.get_text(" ", strip=True) for sib in overview_h3.find_next_siblings() if sib.name != "h3"])
        lines = [l.strip() for l in overview_text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            low = line.lower()
            if "participants" in low and participants_no == "":
                participants_no = line
            elif "from" in low:
                participants_from = line.split("from", 1)[-1].strip()
            elif "recommended for" in low:
                recommended_for = line.split("recommended for", 1)[-1].strip()
            elif "working language" in low:
                working_language = line.split(":", 1)[-1].strip()
            elif "organiser" in low:
                organiser = line.split(":", 1)[-1].strip()

    # Costs
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

    # Downloads
    infopack_downloads = ""
    downloads_heading = next((tag for tag in soup.find_all(['h3', 'h4', 'strong', 'b', 'p']) if "Available downloads" in tag.get_text()), None)
    if downloads_heading:
        first_link = downloads_heading.find_next("a", href=True)
        if first_link:
            infopack_downloads = first_link["href"]
            if not infopack_downloads.startswith("http"):
                infopack_downloads = BASE_URL + infopack_downloads

    # Application procedure link
    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            application_procedure_url = link["href"]
            if not application_procedure_url.startswith("http"):
                application_procedure_url = BASE_URL + application_procedure_url
            break

    return {
        "summary": summary,
        "description": description,
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "working_language": working_language,
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
        external_link = soup.find("a", string=re.compile(r"Proceed to the external", re.IGNORECASE))
        if external_link and external_link.get("href"):
            return external_link["href"]
        for a in soup.find_all("a", href=True):
            if any(domain in a["href"] for domain in ["forms.gle", "google.com/forms", "typeform.com", "surveymonkey.com", "jotform.com"]):
                return a["href"]
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
        "summary","description",
        "participants_no","participants_from","recommended_for","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})


def scrape_events(max_pages=5, max_events=50):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0"})

    socketio.emit("log", {"message": "Inizio scraping pagine lista..."})

    events_dict = {}
    page_size = 10

    for page in range(max_pages):
        offset = page * page_size
        socketio.emit("log", {"message": f"Caricamento pagina {page+1} (offset={offset})..."})
        try:
            url = build_search_url(offset)
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
            if detail_url and detail_url not in events_dict and len(events_dict) < max_events:
                events_dict[detail_url] = event
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
            detail = parse_detail_page(resp.text, detail_url)
            if detail["application_procedure_url"]:
                detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
            else:
                detail["application_form_link"] = ""
            event.update(detail)
        except Exception as e:
            print(f"DEBUG: errore dettaglio {detail_url}: {e}")
            for key in ["summary","description","participants_no","participants_from","recommended_for",
                        "working_language","organiser","participation_fee","accommodation_food",
                        "travel_reimbursement","infopack_downloads","application_procedure_url",
                        "application_form_link"]:
                event[key] = ""
        time.sleep(1)

    save_csv_to_file()
    socketio.emit("log", {"message": f"Scraping completato! Totale: {len(scraped_data)} eventi"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ================== ROUTES ==================

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping(data=None):
    emit("log", {"message": "Avvio scraping..."})
    max_pages = int(data.get("max_pages", 5)) if data else 5
    max_events = int(data.get("max_events", 50)) if data else 50
    scrape_events(max_pages=max_pages, max_events=max_events)


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "summary","description",
        "participants_no","participants_from","recommended_for","working_language","organiser",
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
