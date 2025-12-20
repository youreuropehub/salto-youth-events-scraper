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

# ================= PARSING LISTA (MODIFICATO PER SCADENZE) =================

def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    
    # Cerchiamo tutti i blocchi evento (solitamente contenuti in div che circondano i link)
    # Nella pagina browse di SALTO, ogni evento è un blocco coerente
    items = soup.find_all("div", class_="training-calendar-container") 
    
    # Se la classe sopra non dovesse bastare, usiamo i link come ancora
    if not items:
        # Fallback: cerchiamo i link e risaliamo al container dell'evento
        links = soup.select("a[href*='/tools/european-training-calendar/training/']")
        seen_urls = set()
        for link in links:
            detail_url = link.get("href", "").strip()
            if not detail_url.startswith("http"): detail_url = BASE_URL + detail_url
            if detail_url in seen_urls: continue
            seen_urls.add(detail_url)
            
            # Risaliamo al container che contiene titolo e scadenze
            container = link.find_parent("div", class_="training-calendar-item") or link.find_parent("div")
            
            # ESTRAZIONE SCADENZA DALLA LISTA
            deadline_text = ""
            # Cerchiamo il testo "Deadline:" o "Application deadline:" nel container dell'elenco
            deadline_tag = container.find(string=re.compile(r"Deadline:", re.IGNORECASE))
            if deadline_tag:
                # Spesso è "Deadline: 12.12.2024" all'interno dello stesso tag o nel parent
                full_text = deadline_tag.parent.get_text(strip=True)
                deadline_text = full_text.split("Deadline:")[-1].strip()

            title = link.get_text(strip=True)
            
            events.append({
                "title": normalize_text(title),
                "application_deadline": normalize_text(deadline_text), # Preso dalla lista!
                "detail_url": detail_url,
                "type": "",
                "dates": "",
                "location": "",
                "date_of_selection": ""
            })
    return events

# ================= PARSING DETTAGLIO =================

def parse_detail_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    
    # Training Overview
    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = [sib.get_text("\n", strip=True) for sib in h3_overview.find_next_siblings() if not (sib.name and sib.name.startswith("h"))]
        training_overview = normalize_text("\n".join(parts))

    # Descrizioni
    summary = normalize_text(soup.find("div", class_="mrgn-btm-44 wysiwyg").get_text() if soup.find("div", class_="mrgn-btm-44 wysiwyg") else "")
    
    # Link applicazione
    app_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_procedure_url = link["href"] if link["href"].startswith("http") else BASE_URL + link["href"]
            break

    return {
        "training_overview": training_overview,
        "training_summary": summary,
        "application_procedure_url": app_procedure_url
    }

# ================= CORE LOGIC =================

def process_event_full(event, session, idx, total):
    try:
        resp = session.get(event["detail_url"], timeout=15)
        detail_data = parse_detail_page(resp.text)
        event.update(detail_data)
        
        # Se c'è un link alla procedura, proviamo a prendere il form esterno (Google Form etc)
        if event["application_procedure_url"]:
            try:
                r_proc = session.get(event["application_procedure_url"], timeout=10)
                s_proc = BeautifulSoup(r_proc.text, "html.parser")
                for a in s_proc.find_all("a", href=True):
                    if any(d in a["href"] for d in ["forms.gle","google.com/forms","typeform.com"]):
                        event["application_form_link"] = a["href"]
                        break
            except: pass
            
        socketio.emit("log", {"message": f"[{idx}/{total}] Completato: {event['title']}"})
    except Exception as e:
        print(f"Errore {event['detail_url']}: {e}")

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    
    # 1. Scraping della lista (prendiamo titoli, URL e DEADLINE subito)
    page = 0
    all_events = []
    while page < 5: # Limitato a 5 pagine per test, aumenta a 50 se vuoi tutto
        url = build_search_url(page * 10)
        resp = session.get(url, timeout=15)
        page_events = parse_list_page(resp.text)
        if not page_events: break
        all_events.extend(page_events)
        page += 1
    
    # 2. Arricchimento dati in parallelo (Overview, etc.)
    pool = Pool(10)
    for idx, ev in enumerate(all_events):
        pool.spawn(process_event_full, ev, session, idx+1, len(all_events))
    pool.join()
    
    scraped_data = all_events
    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

def save_csv_to_file():
    if not scraped_data: return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=scraped_data[0].keys())
        writer.writeheader()
        writer.writerows(scraped_data)

# ================= FLASK ROUTES =================

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
