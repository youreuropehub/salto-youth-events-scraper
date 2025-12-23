# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
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
OUTPUT_DIR = "output"
scraped_data = []

# ---------- Helper functions ----------

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

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

    # Metodo 1: h3 + link
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
        text = block.get_text("\n", strip=True)
        lines = [l for l in text.split("\n") if l.strip()]

        try: idx = lines.index(title)
        except ValueError: idx = 0

        type_ = lines[idx-1] if idx>0 else ""
        dates = lines[idx+1] if idx+1<len(lines) else ""
        location = lines[idx+2] if idx+2<len(lines) else ""
        app_deadline = ""
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
            "detail_url": url,
        })

    # Metodo 2: link diretti
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

        try: idx = lines.index(title)
        except ValueError: idx = 0

        type_ = lines[idx-1] if idx-1>=0 else ""
        dates = lines[idx+1] if idx+1<len(lines) else ""
        location = lines[idx+2] if idx+2<len(lines) else ""
        app_deadline = ""
        for i, line in enumerate(lines):
            if "Application deadline" in line:
                if i+1<len(lines):
                    app_deadline = lines[i+1].strip()
                break

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": app_deadline,
            "detail_url": detail_url,
        })

    return events

def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")
    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3","h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"): break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts).strip()

    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i].lower()
        if line == "for" and i+1 < len(lines) and "participants" in lines[i+1].lower():
            participants_no = lines[i+1].replace("participants","").strip()
            j = i+2
            countries = []
            while j < len(lines):
                if lines[j].lower() == "from": j+=1; continue
                if lines[j].lower().startswith("and recommended"): break
                countries.append(lines[j]); j+=1
            participants_from = " ".join(countries).strip()
            i=j; continue
        if "and recommended for" in line and i+1<len(lines):
            recommended_for = lines[i+1].strip()
        if "working language(s):" in line:
            after = lines[i].split("Working language(s):",1)[-1].strip()
            working_lang = after if after else lines[i+1].strip() if i+1<len(lines) else ""
        if line.startswith("organiser"):
            after = lines[i].split("Organiser",1)[-1].replace(":","").strip()
            organiser = after if after else lines[i+1].strip() if i+1<len(lines) else ""
        i+=1

    def section_after_heading(text):
        h = soup.find(lambda tag: tag.name in ["h3","h4"] and text in tag.get_text())
        if not h: return ""
        parts=[]
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"): break
            parts.append(sib.get_text(" ",strip=True))
        return " ".join(parts).strip()

    accessibility = section_after_heading("Accessibility info")
    participation_fee = section_after_heading("Participation fee")
    accommodation_food = section_after_heading("Accommodation and food")
    travel_reimbursement = section_after_heading("Travel reimbursement")

    # Available downloads
    infopack_downloads = ""
    for tag in soup.find_all(['h3','h4','h5','strong','b','p']):
        if "Available downloads:" in tag.get_text():
            for sib in tag.find_next_siblings():
                if sib.name and sib.name.startswith("h"): break
                first_link = sib.find("a", href=True)
                if first_link:
                    href = first_link["href"]
                    if not href.startswith("http"): href = BASE_URL+href
                    infopack_downloads = href
                    break
            if infopack_downloads: break

    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            href = link["href"]
            if not href.startswith("http"): href = BASE_URL + href
            application_procedure_url = href
            break

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
    }

def get_external_application_link(application_procedure_url):
    if not application_procedure_url: return ""
    try:
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text,"html.parser")
        ext_link = soup.find("a", string=re.compile(r"Proceed to the external", re.IGNORECASE))
        if ext_link and ext_link.get("href"): return ext_link["href"]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(domain in href for domain in ["forms.gle","google.com/forms","typeform.com","surveymonkey.com","jotform.com"]):
                return href
        return ""
    except Exception as e:
        print(f"Error fetching application link: {e}")
        return ""

def save_csv_to_file():
    if not scraped_data: return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for",
        "accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url",
        "application_form_link","detail_url"
    ]
    with open(csv_path,"w",newline="",encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})

# ---------- Scraping principale ----------

def scrape_events(max_pages:int):
    global scraped_data
    scraped_data = []
    session = requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0"})
    events_dict = {}
    page = 0
    page_size = 10

    while page < max_pages:
        offset = page*page_size
        msg = f"Caricamento pagina {page+1} (offset={offset})..."
        socketio.emit("log", {"message": msg})
        try:
            url = build_search_url(offset)
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore pagina {page+1}: {e}"})
            break

        events = parse_list_page(resp.text)
        if not events: break

        for event in events:
            detail_url = event.get("detail_url","")
            if detail_url and detail_url not in events_dict:
                events_dict[detail_url] = event
        page+=1
        time.sleep(1)

    scraped_data = list(events_dict.values())
    socketio.emit("log", {"message": f"Totale eventi trovati: {len(scraped_data)}"})

    # Dettagli
    for i, event in enumerate(scraped_data,start=1):
        detail_url = event.get("detail_url","")
        if not detail_url: continue
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        try:
            resp = session.get(detail_url, timeout=15)
            resp.raise_for_status()
            detail = parse_detail_page(resp.text, detail_url)
            detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"]) if detail["application_procedure_url"] else ""
            event.update(detail)
        except:
            event.update({k:"" for k in ["participants_no","participants_from","recommended_for","accessibility","working_language","organiser","participation_fee","accommodation_food","travel_reimbursement","infopack_downloads","application_procedure_url","application_form_link"]})
        time.sleep(1)

    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping(data):
    try:
        max_pages = int(data.get("max_pages",1))
        if max_pages<1: max_pages=1
    except:
        max_pages=1
    socketio.emit("log", {"message": f"Avvio scraping ({max_pages} pagine)..."})
    scrape_events(max_pages)

@app.route("/download_csv")
def download_csv():
    if not scraped_data: return "Nessun dato disponibile",400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for",
        "accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(scraped_data)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer,mimetype="text/csv",as_attachment=True,download_name="salto_events_complete.csv")

# ---------- Run ----------

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
