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
from gevent.pool import Pool

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []  # Variabile globale per memorizzare i risultati

# ================= UTILITY =================

def normalize_text(text: str) -> str:
    """Rimuove spazi multipli e line break inutili."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()

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
        "&b_application_deadline_after_day={day}"
        "&b_application_deadline_after_month={month}"
        "&b_application_deadline_after_year={year}"
    )
    return base.format(offset=offset, day=day, month=month, year=year)

# ================= ESTRAZIONE DATE (MODIFICATA) =================

def parse_dates_robust(soup: BeautifulSoup):
    """
    Estrae Application deadline e Date of selection cercando le keyword nel testo.
    Questa versione è più robusta rispetto alla ricerca di classi CSS fisse.
    """
    application_deadline = ""
    date_of_selection = ""
    
    # Cerchiamo i contenitori comuni dove SALTO mette queste info
    # (spesso in span con classe block o in tabelle/liste)
    tags = soup.find_all(["span", "div", "td", "li", "p"])
    
    for tag in tags:
        text = tag.get_text(" ", strip=True)
        
        # 1. Cerca Application Deadline
        if "Application deadline" in text and not application_deadline:
            if ":" in text:
                # Estrae ciò che segue i due punti
                val = text.split(":", 1)[1].strip()
                application_deadline = re.sub(r"\(.*?\)", "", val).strip()
            else:
                # Se il testo è solo l'etichetta, prendi il contenuto del tag successivo
                application_deadline = tag.find_next().get_text(strip=True)
                
        # 2. Cerca Date of Selection
        if "Date of selection" in text and not date_of_selection:
            if ":" in text:
                val = text.split(":", 1)[1].strip()
                date_of_selection = re.sub(r"\(.*?\)", "", val).strip()
            else:
                date_of_selection = tag.find_next().get_text(strip=True)

    # Pulizia finale da eventuali residui di virgolette o parentesi rimaste
    application_deadline = application_deadline.replace('"', '').strip()
    date_of_selection = date_of_selection.replace('"', '').strip()
    
    return application_deadline, date_of_selection

# ================= PARSING =================

def parse_list_page(html: str):
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
            "title": normalize_text(title),
            "type": normalize_text(type_),
            "dates": normalize_text(dates),
            "location": normalize_text(location),
            "application_deadline": "",  # Popolato in detail
            "date_of_selection": "",     
            "detail_url": detail_url,
        })

    return events

def parse_detail_page(html: str, detail_url: str):
    soup = BeautifulSoup(html, "html.parser")

    # Training Overview
    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = normalize_text("\n".join(parts))

    # Altre sezioni descrittive
    training_summary = ""
    summary_div = soup.find("div", class_="mrgn-btm-44 wysiwyg")
    if summary_div:
        training_summary = normalize_text(summary_div.get_text("\n", strip=True))

    training_description = ""
    desc_div = soup.find("div", class_="training-description running-text wysiwyg mrgn-btm-33")
    if desc_div:
        training_description = normalize_text(desc_div.get_text("\n", strip=True))

    # Dati partecipanti e organizzatore
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

    # Costi e Download
    def section_after_heading(text: str):
        h = soup.find(lambda tag: tag.name in ["h3", "h4"] and text in tag.get_text())
        if not h: return ""
        parts = [sib.get_text(" ", strip=True) for sib in h.find_next_siblings() if not (sib.name and sib.name.startswith("h"))]
        return normalize_text(" ".join(parts))

    participation_fee = section_after_heading("Participation fee")
    accommodation_food = section_after_heading("Accommodation and food")
    travel_reimbursement = section_after_heading("Travel reimbursement")
    
    # Procedura candidatura
    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_href = link["href"]
            application_procedure_url = app_href if app_href.startswith("http") else BASE_URL + app_href
            break

    # ESTRAZIONE DATE (UTILIZZA LA NUOVA FUNZIONE ROBUSTA)
    app_deadline, sel_date = parse_dates_robust(soup)

    return {
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "working_language": working_lang,
        "organiser": organiser,
        "participation_fee": participation_fee,
        "accommodation_food": accommodation_food,
        "travel_reimbursement": travel_reimbursement,
        "application_procedure_url": application_procedure_url,
        "training_overview": training_overview,
        "training_summary": training_summary,
        "training_description": training_description,
        "application_deadline": app_deadline,
        "date_of_selection": sel_date,
    }

# ================= FUNZIONI DI SUPPORTO =================

def get_external_application_link(application_procedure_url: str) -> str:
    if not application_procedure_url: return ""
    try:
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        ext = soup.find("a", string=re.compile(r"Proceed to the external", re.IGNORECASE))
        if ext and ext.get("href"): return ext["href"]
        for a in soup.find_all("a", href=True):
            if any(d in a["href"] for d in ["forms.gle","google.com/forms","typeform.com","surveymonkey.com"]):
                return a["href"]
        return ""
    except: return ""

def save_csv_to_file():
    if not scraped_data: return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline","date_of_selection",
        "training_overview","training_summary","training_description",
        "participants_no","participants_from","recommended_for",
        "working_language","organiser","participation_fee","accommodation_food",
        "travel_reimbursement","application_procedure_url","application_form_link","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

def process_event_detail(event, session, idx, total):
    url = event.get("detail_url")
    if not url: return
    try:
        resp = session.get(url, timeout=15)
        detail = parse_detail_page(resp.text, url)
        if detail.get("application_procedure_url"):
            detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
        event.update(detail)
        socketio.emit("log", {"message": f"[{idx}/{total}] Elaborato: {event['title']}"})
    except Exception as e:
        print(f"Errore {url}: {e}")

# ================= SCRAPING CORE =================

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    
    events_dict = {}
    page, max_pages = 0, 50

    while page < max_pages:
        try:
            url = build_search_url(page * 10)
            resp = session.get(url, timeout=15)
            events = parse_list_page(resp.text)
            if not events: break
            for e in events:
                if e["detail_url"] not in events_dict:
                    events_dict[e["detail_url"]] = e
            page += 1
        except: break

    scraped_data = list(events_dict.values())
    pool = Pool(10)
    for idx, event in enumerate(scraped_data):
        pool.spawn(process_event_detail, event, session, idx+1, len(scraped_data))
    pool.join()

    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ================= ROUTES =================

@app.route("/")
def index(): return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping(): scrape_events()

@app.route("/download_csv")
def download_csv():
    if not scraped_data: return "No data", 400
    text_buffer = StringIO()
    writer = csv.DictWriter(text_buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)
    buf = BytesIO(text_buffer.getvalue().encode("utf-8"))
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="salto_events.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
