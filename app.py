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
from bs4 import BeautifulSoup, NavigableString
import requests
from gevent.pool import Pool

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

# Variabile globale per memorizzare i risultati
scraped_data = []

# ================= UTILITY =================

def normalize_text(text: str) -> str:
    """Rimuove spazi multipli e line break inutili."""
    if not text: return ""
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

# ================= EXTRACTION LOGIC (INTEGRATA E POTENZIATA) =================

def parse_dates_robust(soup: BeautifulSoup):
    """
    Estrae Application deadline e Date of selection gestendo i tag <strong> nidificati.
    Sostituisce la vecchia logica mrgn-btm-22 con un approccio basato sul contenuto.
    """
    application_deadline = ""
    date_of_selection = ""
    
    # Cerchiamo tutti i blocchi che SALTO usa per le scadenze
    spans = soup.find_all("span", class_="call-addendum")
    
    for span in spans:
        # Il segreto è usare get_text con lo spazio come separatore:
        # trasforma "<strong>Deadline:</strong> 2025" in "Deadline: 2025"
        text = span.get_text(" ", strip=True)
        # Pulizia da parentesi e virgolette
        text = re.sub(r"\(.*?\)", "", text).replace('"', '').strip()
        
        if "Application deadline" in text:
            if ":" in text:
                application_deadline = text.split(":", 1)[1].strip()
            else:
                # Caso limite: la data è nel tag successivo
                next_node = span.find_next_sibling()
                if next_node: application_deadline = next_node.get_text(strip=True)
                
        elif "Date of selection" in text:
            if ":" in text:
                date_of_selection = text.split(":", 1)[1].strip()
    
    # Fallback se non trovato tramite classe specifica
    if not application_deadline:
        target = soup.find(string=re.compile(r"Application deadline", re.IGNORECASE))
        if target:
            # Se è dentro un tag strong, prendiamo il testo dopo il tag
            parent = target.parent
            if parent.name in ['strong', 'b']:
                sibling = parent.next_sibling
                application_deadline = sibling.strip() if isinstance(sibling, NavigableString) else ""

    return application_deadline, date_of_selection

# ================= PARSING PAGINE =================

def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title: continue
        detail_url = link.get("href", "").strip()
        if detail_url and not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url
        if detail_url in seen_urls: continue
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
            "title": normalize_text(title),
            "type": normalize_text(lines[idx - 1] if idx - 1 >= 0 else ""),
            "dates": normalize_text(lines[idx + 1] if idx + 1 < len(lines) else ""),
            "location": normalize_text(lines[idx + 2] if idx + 2 < len(lines) else ""),
            "application_deadline": "",
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
            if sib.name and sib.name.startswith("h"): break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = normalize_text("\n".join(parts))

    # Descrizioni
    training_summary = normalize_text(soup.find("div", class_="mrgn-btm-44 wysiwyg").get_text() if soup.find("div", class_="mrgn-btm-44 wysiwyg") else "")
    training_description = normalize_text(soup.find("div", class_="training-description running-text wysiwyg mrgn-btm-33").get_text() if soup.find("div", class_="training-description running-text wysiwyg mrgn-btm-33") else "")

    # Parsing campi specifici dall'overview
    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        line_l = line.lower()
        if "participants" in line_l and i > 0 and lines[i-1].lower() == "for":
            participants_no = line.replace("participants", "").strip()
        if "working language(s):" in line_l:
            working_lang = line.split(":", 1)[1].strip()
        if "organiser:" in line_l:
            organiser = line.split(":", 1)[1].strip()

    # Costi
    def get_sec(t):
        h = soup.find(lambda x: x.name in ["h3", "h4"] and t in x.get_text())
        return normalize_text(" ".join([s.get_text(" ", strip=True) for s in h.find_next_siblings() if not (s.name and s.name.startswith("h"))])) if h else ""

    # Application Procedure Link
    application_procedure_url = ""
    proc_link = soup.find("a", href=re.compile(r"/application-procedure/"))
    if proc_link:
        application_procedure_url = proc_link["href"] if proc_link["href"].startswith("http") else BASE_URL + proc_link["href"]

    # --- INTEGRAZIONE DEADLINE ---
    app_deadline, sel_date = parse_dates_robust(soup)

    return {
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "working_language": working_lang,
        "organiser": organiser,
        "participation_fee": get_sec("Participation fee"),
        "accommodation_food": get_sec("Accommodation and food"),
        "travel_reimbursement": get_sec("Travel reimbursement"),
        "application_procedure_url": application_procedure_url,
        "training_overview": training_overview,
        "training_summary": training_summary,
        "training_description": training_description,
        "application_deadline": app_deadline,
        "date_of_selection": sel_date,
    }

# ================= PROCEDURA COMPLETA =================

def get_external_application_link(url):
    try:
        r = requests.get(url, timeout=10)
        s = BeautifulSoup(r.text, "html.parser")
        for a in s.find_all("a", href=True):
            if any(d in a["href"] for d in ["forms.gle","google.com/forms","typeform.com","jotform"]):
                return a["href"]
    except: pass
    return ""

def process_event_detail(event, session, idx, total):
    try:
        resp = session.get(event["detail_url"], timeout=15)
        detail = parse_detail_page(resp.text, event["detail_url"])
        if detail["application_procedure_url"]:
            detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
        event.update(detail)
        socketio.emit("log", {"message": f"[{idx}/{total}] Elaborato: {event['title']}"})
    except Exception as e:
        print(f"Errore {event['detail_url']}: {e}")

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    
    events_dict = {}
    for page in range(5): # prime 5 pagine per test
        url = build_search_url(page * 10)
        resp = session.get(url, timeout=15)
        events = parse_list_page(resp.text)
        if not events: break
        for e in events:
            if e["detail_url"] not in events_dict: events_dict[e["detail_url"]] = e

    scraped_data = list(events_dict.values())
    pool = Pool(10)
    for i, ev in enumerate(scraped_data):
        pool.spawn(process_event_detail, ev, session, i+1, len(scraped_data))
    pool.join()

    # Salvataggio CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    if scraped_data:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=scraped_data[0].keys())
            writer.writeheader()
            writer.writerows(scraped_data)
    
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ================= FLASK =================

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
    return send_file(BytesIO(text_buffer.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="salto_export.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
