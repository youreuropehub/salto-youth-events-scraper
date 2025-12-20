# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import csv
import re
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import requests
import eventlet

eventlet.monkey_patch()

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []  # variabile globale per memorizzare i risultati

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
            r"Application deadline\s*(?:\(24h UTC\))?\s*[:]\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            text
        )
        if match:
            return match.group(1).strip()
    full_text = soup.get_text(" ", strip=True)
    match = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*[:]\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        full_text
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

        type_ = lines[idx - 1] if idx - 1 >= 0 else ""
        dates = lines[idx + 1] if idx + 1 < len(lines) else ""
        location = lines[idx + 2] if idx + 2 < len(lines) else ""

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": "",
            "detail_url": detail_url,
        })

    return events


def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts).strip()

    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i].lower()
        if line == "for" and i + 1 < len(lines) and "participants" in lines[i + 1].lower():
            participants_no = lines[i + 1].replace("participants", "").strip()
            j = i + 2
            countries = []
            while j < len(lines) and not lines[j].lower().startswith("and recommended"):
                countries.append(lines[j])
                j += 1
            participants_from = " ".join(countries).strip()
            i = j
            continue
        if "and recommended for" in line and i + 1 < len(lines):
            recommended_for = lines[i + 1].strip()
        if "working language(s):" in line:
            after = lines[i].split("Working language(s):", 1)[-1].strip()
            working_lang = after if after else (lines[i + 1].strip() if i + 1 < len(lines) else "")
        if line.startswith("organiser"):
            after = lines[i].split("Organiser", 1)[-1].replace(":", "").strip()
            organiser = after if after else (lines[i + 1].strip() if i + 1 < len(lines) else "")
        i += 1

    accessibility = ""
    h_acc = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Accessibility info" in tag.get_text())
    if h_acc:
        parts = []
        for sib in h_acc.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        accessibility = " ".join(parts).strip()

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

    infopack_downloads = ""
    downloads_heading = None
    for tag in soup.find_all(['h3', 'h4', 'h5', 'strong', 'b', 'p']):
        if "Available downloads:" in tag.get_text():
            downloads_heading = tag
            break
    if downloads_heading:
        for sib in downloads_heading.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            first_link = sib.find("a", href=True)
            if first_link:
                href = first_link["href"]
                if not href.startswith("http"):
                    href = BASE_URL + href
                infopack_downloads = href
                break

    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_href = link["href"]
            if not app_href.startswith("http"):
                app_href = BASE_URL + app_href
            application_procedure_url = app_href
            break

    application_deadline = extract_application_deadline(soup)

    return {
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
        "training_overview": training_overview,
        "application_deadline": application_deadline,
    }


def get_external_application_link(application_procedure_url):
    if not application_procedure_url:
        return ""
    try:
        eventlet.sleep(0)  # lascia che il server gestisca ping
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        external_link = soup.find("a", string=re.compile(r"Proceed to the external", re.IGNORECASE))
        if external_link and external_link.get("href"):
            return external_link["href"]
        for a in soup.find_all("a", href=True):
            if any(domain in a["href"] for domain in ["forms.gle","google.com/forms","typeform.com","surveymonkey.com","jotform.com"]):
                return a["href"]
        return ""
    except Exception as e:
        print(f"Error fetching application link from {application_procedure_url}: {e}")
        return ""


# ================= CSV =================

def save_csv_to_file():
    if not scraped_data:
        print("DEBUG: nessun dato da salvare")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fieldnames = [
        "title","type","dates","location","application_deadline","training_overview",
        "participants_no","participants_from","recommended_for","accessibility",
        "working_language","organiser","participation_fee","accommodation_food",
        "travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    print(f"DEBUG: CSV salvato in {csv_path}")
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})


# ================= SCRAPING =================

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_dict = {}
    page = 0
    max_pages = 50
    page_size = 10

    while page < max_pages:
        offset = page * page_size
        msg = f"Caricamento pagina {page + 1} (offset={offset})..."
        socketio.emit("log", {"message": msg})
        eventlet.sleep(0)
        try:
            url = build_search_url(offset)
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"Errore caricamento pagina {page + 1}: {e}")
            break

        events = parse_list_page(resp.text)
        if not events:
            break

        for event in events:
            detail_url = event.get("detail_url", "")
            if detail_url and detail_url not in events_dict:
                events_dict[detail_url] = event

        page += 1
        eventlet.sleep(0.1)

    scraped_data = list(events_dict.values())
    socketio.emit("log", {"message": f"Totale eventi trovati: {len(scraped_data)}"})
    eventlet.sleep(0)

    # batch di dettagli per evitare blocco WebSocket
    batch_size = 5
    for i in range(0, len(scraped_data), batch_size):
        batch = scraped_data[i:i+batch_size]
        for idx, event in enumerate(batch, start=i+1):
            detail_url = event.get("detail_url", "")
            if not detail_url:
                continue
            msg = f"[{idx}/{len(scraped_data)}] {event['title']}"
            socketio.emit("log", {"message": msg})
            eventlet.sleep(0)
            try:
                resp = session.get(detail_url, timeout=15)
                resp.raise_for_status()
                detail = parse_detail_page(resp.text, detail_url)
                if detail.get("application_procedure_url"):
                    detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
                else:
                    detail["application_form_link"] = ""
                event.update(detail)
            except Exception as e:
                print(f"Errore dettaglio {detail_url}: {e}")
                for key in ["participants_no","participants_from","recommended_for","accessibility",
                            "working_language","organiser","participation_fee","accommodation_food",
                            "travel_reimbursement","infopack_downloads","application_procedure_url",
                            "application_form_link","training_overview","application_deadline"]:
                    event[key] = ""
            eventlet.sleep(0.1)

    save_csv_to_file()
    socketio.emit("log", {"message": f"Scraping completato! Totale: {len(scraped_data)} eventi"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})
    eventlet.sleep(0)
    print(f"DEBUG: scraping completato! Totale eventi unici: {len(scraped_data)}")


# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping():
    emit("log", {"message": "Avvio scraping..."})
    socketio.start_background_task(scrape_events)


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400

    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline","training_overview",
        "participants_no","participants_from","recommended_for","accessibility",
        "working_language","organiser","participation_fee","accommodation_food",
        "travel_reimbursement",
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
    return jsonify({
        "status": "started",
        "message": "Scraping avviato in background."
    })


@app.route("/api/scrape_and_download", methods=["POST", "GET"])
def api_scrape_and_download():
    socketio.start_background_task(scrape_events)
    return jsonify({
        "status": "started",
        "message": "Scraping avviato in background. Puoi scaricare il CSV successivamente."
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)
