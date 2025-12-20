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

scraped_data = []

# ================= UTILITY =================

def normalize_text(text: str) -> str:
    if not text: return ""
    return re.sub(r"\s+", " ", text).strip()

def build_search_url(offset: int) -> str:
    today = date.today()
    base = (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        "?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        "&b_begin_date_after_day={day}&b_begin_date_after_month={month}&b_begin_date_after_year={year}"
    ).format(offset=offset, day=today.day, month=today.month, year=today.year)
    return base

# ================= NUOVO ESTRATTORE SCADENZE (MIRATO) =================

def parse_dates_specific(soup: BeautifulSoup):
    """
    Estrae le date cercando specificamente i blocchi 'call-addendum' 
    presenti nelle pagine come quella di esempio.
    """
    application_deadline = ""
    date_of_selection = ""
    
    # Cerchiamo tutti i tag con classe 'call-addendum' (quelli nel box laterale/top)
    addendum_spans = soup.find_all("span", class_="call-addendum")
    
    for span in addendum_spans:
        text = span.get_text(" ", strip=True)
        
        # Se lo span contiene "Application deadline"
        if "Application deadline" in text:
            # Rimuoviamo il testo "Application deadline" e prendiamo il resto
            # Spesso il formato è "Application deadline: 12 January 2025"
            parts = text.split("deadline")
            if len(parts) > 1:
                val = parts[1].replace(":", "").strip()
                # Puliamo da eventuali scritte tipo "(midnight CET)"
                application_deadline = re.sub(r"\(.*?\)", "", val).strip()
                
        # Se lo span contiene "Date of selection"
        if "Date of selection" in text:
            parts = text.split("selection")
            if len(parts) > 1:
                val = parts[1].replace(":", "").strip()
                date_of_selection = re.sub(r"\(.*?\)", "", val).strip()

    # Se non abbiamo trovato nulla con le classi, proviamo una ricerca testuale profonda
    if not application_deadline:
        target = soup.find(string=re.compile(r"Application deadline", re.IGNORECASE))
        if target:
            parent_text = target.parent.get_text(" ", strip=True)
            if ":" in parent_text:
                application_deadline = parent_text.split(":", 1)[1].strip()

    return application_deadline, date_of_selection

# ================= PARSING PAGINA LISTA =================

def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title: continue
        
        detail_url = link.get("href", "").strip()
        if not detail_url.startswith("http"): detail_url = BASE_URL + detail_url
        if detail_url in seen_urls: continue
        seen_urls.add(detail_url)

        container = link.find_parent()
        for _ in range(4):
            if container and container.name not in ["body", "html"]:
                container = container.parent

        text_block = container.get_text("\n", strip=True) if container else ""
        lines = [l.strip() for l in text_block.split("\n") if l.strip()]

        try: idx = lines.index(title)
        except ValueError: idx = 0

        events.append({
            "title": normalize_text(title),
            "type": lines[idx - 1] if idx - 1 >= 0 else "",
            "dates": lines[idx + 1] if idx + 1 < len(lines) else "",
            "location": lines[idx + 2] if idx + 2 < len(lines) else "",
            "detail_url": detail_url
        })
    return events

# ================= PARSING PAGINA DETTAGLIO =================

def parse_detail_page(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # Estrazione date con la nuova funzione
    app_deadline, sel_date = parse_dates_specific(soup)

    # Training Overview
    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"): break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts)

    # Descrizioni e Summary
    summary = ""
    sum_div = soup.find("div", class_="mrgn-btm-44 wysiwyg")
    if sum_div: summary = sum_div.get_text("\n", strip=True)

    desc = ""
    desc_div = soup.find("div", class_="training-description running-text wysiwyg mrgn-btm-33")
    if desc_div: desc = desc_div.get_text("\n", strip=True)

    # Link Candidatura
    app_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_procedure_url = link["href"] if link["href"].startswith("http") else BASE_URL + link["href"]
            break

    return {
        "application_deadline": app_deadline,
        "date_of_selection": sel_date,
        "training_overview": normalize_text(training_overview),
        "training_summary": normalize_text(summary),
        "training_description": normalize_text(desc),
        "application_procedure_url": app_procedure_url
    }

# ================= LOGICA DI PROCESSO =================

def process_event_detail(event, session, idx, total):
    try:
        resp = session.get(event["detail_url"], timeout=15)
        detail = parse_detail_page(resp.text)
        
        # Cerca link esterno (Google Form, etc.)
        if detail["application_procedure_url"]:
            try:
                r_p = session.get(detail["application_procedure_url"], timeout=10)
                s_p = BeautifulSoup(r_p.text, "html.parser")
                for a in s_p.find_all("a", href=True):
                    if any(d in a["href"] for d in ["forms.gle","google.com/forms","typeform.com"]):
                        detail["application_form_link"] = a["href"]
                        break
            except: pass
            
        event.update(detail)
        socketio.emit("log", {"message": f"[{idx}/{total}] {event['title']}"})
    except Exception as e:
        print(f"Errore {event['detail_url']}: {e}")

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # 1. Trova eventi
    events_list = []
    for page in range(5): # prime 5 pagine
        url = build_search_url(page * 10)
        resp = session.get(url)
        events_list.extend(parse_list_page(resp.text))

    # 2. Dettagli in parallelo
    pool = Pool(10)
    for i, ev in enumerate(events_list):
        pool.spawn(process_event_detail, ev, session, i+1, len(events_list))
    pool.join()

    scraped_data = events_list
    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

def save_csv_to_file():
    if not scraped_data: return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline","date_of_selection",
        "training_overview","training_summary","training_description",
        "application_procedure_url","application_form_link","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(scraped_data)

# ================= FLASK ROUTES =================

@app.route("/")
def index(): return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping(): scrape_events()

@app.route("/download_csv")
def download_csv():
    if not scraped_data: return "Nessun dato", 400
    text_buffer = StringIO()
    writer = csv.DictWriter(text_buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)
    buf = BytesIO(text_buffer.getvalue().encode("utf-8"))
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="salto_results.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
