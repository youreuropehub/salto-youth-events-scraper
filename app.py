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

scraped_data = []

# ================= UTILITY =================

def normalize_text(text: str) -> str:
    if not text: return ""
    # Rimuove caratteri non ASCII strani e spazi multipli
    text = re.sub(r"\s+", " ", text).strip()
    return text

def build_search_url(offset: int) -> str:
    today = date.today()
    base = (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        "?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        "&b_begin_date_after_day={day}&b_begin_date_after_month={month}&b_begin_date_after_year={year}"
    ).format(offset=offset, day=today.day, month=today.month, year=today.year)
    return base

# ================= PARSING DATE (CORREZIONE CRITICA) =================

def parse_dates_force(soup: BeautifulSoup):
    """
    Estrae le date gestendo la struttura nidificata con <strong>.
    Esempio HTML target:
    <span class="block call-addendum"><strong>Application deadline:</strong> 16 February 2025</span>
    """
    application_deadline = ""
    date_of_selection = ""

    # Strategia 1: Cerca specificamente nei tag 'call-addendum' (tipici del link che hai mandato)
    spans = soup.find_all("span", class_="call-addendum")
    
    for span in spans:
        # Prende tutto il testo visibile nello span, ignorando i tag interni
        # Esempio: "Application deadline: 16 February 2025"
        full_text = span.get_text(" ", strip=True) 
        
        if "Application deadline" in full_text:
            # Divide per i due punti e prende la parte destra
            if ":" in full_text:
                raw_date = full_text.split(":", 1)[1]
                application_deadline = normalize_text(re.sub(r"\(.*?\)", "", raw_date))
        
        if "Date of selection" in full_text:
            if ":" in full_text:
                raw_date = full_text.split(":", 1)[1]
                date_of_selection = normalize_text(re.sub(r"\(.*?\)", "", raw_date))

    # Strategia 2 (Fallback): Cerca 'Application deadline' ovunque nel documento
    # Se la strategia 1 fallisce (es. layout diverso)
    if not application_deadline:
        # Cerca il nodo di testo che contiene "Application deadline"
        target = soup.find(string=re.compile(r"Application deadline", re.IGNORECASE))
        if target:
            # Caso A: il target è dentro un <strong> o <b> e la data è nel nodo successivo
            parent = target.parent
            if parent.name in ['strong', 'b', 'label']:
                # Cerca il prossimo elemento fratello o testo navigabile
                next_node = parent.next_sibling
                if isinstance(next_node, NavigableString):
                    application_deadline = normalize_text(str(next_node))
                elif next_node:
                    application_deadline = normalize_text(next_node.get_text())
            
            # Caso B: il testo è tutto insieme ("Application deadline: 2025...")
            else:
                full_text = target.strip()
                if ":" in full_text:
                     application_deadline = normalize_text(full_text.split(":", 1)[1])

    # Pulizia finale brutale (rimuove eventuali 'Date of selection' se finiti dentro per errore)
    if "Date of selection" in application_deadline:
        application_deadline = application_deadline.split("Date of selection")[0].strip()

    return application_deadline, date_of_selection

# ================= PARSING PAGINE =================

def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

    # Selettore generico per i link agli eventi
    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title: continue
        
        detail_url = link.get("href", "").strip()
        if not detail_url.startswith("http"): detail_url = BASE_URL + detail_url
        
        # Evita duplicati
        if detail_url in seen_urls: continue
        seen_urls.add(detail_url)

        # Recupera metadati dalla lista (Type, Dates, Location)
        container = link.find_parent()
        for _ in range(4): # Risale l'albero per trovare il container del testo
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

def parse_detail_page(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # 1. DATE (Fix applicato qui)
    app_deadline, sel_date = parse_dates_force(soup)

    # 2. OVERVIEW
    training_overview = ""
    h_ov = soup.find(lambda t: t.name in ["h3", "h4"] and "Training overview" in t.get_text())
    if h_ov:
        parts = []
        for sib in h_ov.find_next_siblings():
            if sib.name and sib.name.startswith("h"): break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts)

    # 3. LINK APPLICATION
    app_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            href = link["href"]
            app_procedure_url = href if href.startswith("http") else BASE_URL + href
            break

    # 4. ALTRI DATI (Costi, ecc - Opzionale, per completezza)
    # ... (si possono aggiungere altri campi qui se servono)

    return {
        "application_deadline": app_deadline,
        "date_of_selection": sel_date,
        "training_overview": normalize_text(training_overview),
        "application_procedure_url": app_procedure_url
    }

# ================= CORE LOGIC =================

def process_event(event, session, idx, total):
    try:
        resp = session.get(event["detail_url"], timeout=20)
        detail = parse_detail_page(resp.text)
        
        # Cerca link form esterno (Google Form ecc)
        if detail["application_procedure_url"]:
            try:
                r_p = session.get(detail["application_procedure_url"], timeout=10)
                s_p = BeautifulSoup(r_p.text, "html.parser")
                for a in s_p.find_all("a", href=True):
                    if any(d in a["href"] for d in ["forms.gle","google.com/forms","typeform.com","jotform"]):
                        detail["application_form_link"] = a["href"]
                        break
            except: pass
            
        event.update(detail)
        socketio.emit("log", {"message": f"[{idx}/{total}] Preso: {event['title']} -> Deadline: {detail['application_deadline']}"})
    except Exception as e:
        print(f"Errore {event['detail_url']}: {e}")

def scrape_events():
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_list = []
    
    # Esegue scraping su 3 pagine per test (aumenta range(3) se vuoi di più)
    for page in range(3): 
        url = build_search_url(page * 10)
        socketio.emit("log", {"message": f"Scraping lista pagina {page+1}..."})
        try:
            resp = session.get(url, timeout=15)
            events_list.extend(parse_list_page(resp.text))
        except Exception as e:
            print(f"Errore pagina {page}: {e}")

    socketio.emit("log", {"message": f"Trovati {len(events_list)} eventi. Inizio download dettagli..."})

    # Pool per parallelizzare il download dei dettagli
    pool = Pool(8)
    for i, ev in enumerate(events_list):
        pool.spawn(process_event, ev, session, i+1, len(events_list))
    pool.join()

    scraped_data = events_list
    
    # Salvataggio CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_fixed.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline","date_of_selection",
        "training_overview","application_procedure_url","application_form_link","detail_url"
    ]
    
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ================= FLASK ROUTES =================

@app.route("/")
def index(): return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping(): 
    emit("log", {"message": "Avvio scraping corretto..."})
    scrape_events()

@app.route("/download_csv")
def download_csv():
    if not scraped_data: return "Nessun dato", 400
    text_buffer = StringIO()
    # Usa le chiavi del primo elemento o un elenco fisso
    keys = scraped_data[0].keys() if scraped_data else []
    writer = csv.DictWriter(text_buffer, fieldnames=keys)
    writer.writeheader()
    writer.writerows(scraped_data)
    buf = BytesIO(text_buffer.getvalue().encode("utf-8"))
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="salto_events_fixed.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
