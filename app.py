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
OUTPUT_DIR = "output"
scraped_data = []

# ---------- Helper functions ----------

def build_search_url(offset: int) -> str:
    today = date.today()
    day, month, year = today.day, today.month, today.year
    return (
        f"{BASE_URL}/tools/european-training-calendar/browse/?b_offset={offset}&b_limit=10"
        f"&b_order=applicationDeadline"
        f"&b_begin_date_after_day={day}&b_begin_date_after_month={month}&b_begin_date_after_year={year}"
        f"&b_application_deadline_after_day={day}&b_application_deadline_after_month={month}&b_application_deadline_after_year={year}"
    )

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a: continue
        title = a.get_text(strip=True)
        url = a.get("href","").strip()
        if url and not url.startswith("http"): url = BASE_URL + url
        if url in seen_urls: continue
        seen_urls.add(url)

        block = h3.parent
        text = block.get_text("\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        try: idx = lines.index(title)
        except ValueError: idx = 0

        type_ = lines[idx-1] if idx>0 else ""
        dates = lines[idx+1] if idx+1<len(lines) else ""
        location = lines[idx+2] if idx+2<len(lines) else ""
        application_deadline = ""
        for l in lines:
            if "Application deadline" in l:
                application_deadline = l.split(":",1)[-1].strip()
                break

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": application_deadline,
            "detail_url": url
        })

    return events

def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html,"html.parser")
    # Training description
    training_description = ""
    desc_div = soup.find("div", class_="training-description")
    if desc_div:
        training_description = desc_div.get_text("\n",strip=True)

    # Overview fields
    training_overview=""
    h3_overview = soup.find(lambda tag: tag.name in ["h3","h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts=[]
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"): break
            parts.append(sib.get_text("\n",strip=True))
        training_overview="\n".join(parts).strip()

    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]
    i=0
    while i<len(lines):
        line = lines[i].lower()
        if line=="for" and i+1<len(lines) and "participants" in lines[i+1].lower():
            participants_no = lines[i+1].replace("participants","").strip()
            j=i+2
            countries=[]
            while j<len(lines):
                if lines[j].lower()=="from": j+=1; continue
                if lines[j].lower().startswith("and recommended"): break
                countries.append(lines[j]); j+=1
            participants_from=" ".join(countries).strip()
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

    # Sections
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

    # Infopack downloads
    infopack_downloads=""
    for tag in soup.find_all(['h3','h4','h5','strong','b','p']):
        if "Available downloads:" in tag.get_text():
            for sib in tag.find_next_siblings():
                if sib.name and sib.name.startswith("h"): break
                first_link = sib.find("a", href=True)
                if first_link:
                    href = first_link["href"]
                    if not href.startswith("http"): href = BASE_URL + href
                    infopack_downloads = href
                    break
            if infopack_downloads: break

    # Application procedure
    application_procedure_url=""
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
        "training_description": training_description,
    }

def get_external_application_link(application_procedure_url):
    if not application_procedure_url: return ""
    try:
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text,"html.parser")
        ext_link = soup.find("a", string=re.compile(r"Proceed to the external", re.IGNORECASE))
        if ext_link and ext_link.get("href"): return ext_link["href"]
        return ""
    except:
        return ""

def save_csv_to_file():
    if not scraped_data: return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for","accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement","infopack_downloads",
        "application_procedure_url","application_form_link","training_description","detail_url"
    ]
    with open(csv_path,"w",newline="",encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})

def scrape_events():
    global scraped_data
    scraped_data=[]
    session = requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0"})
    events_dict={}
    page=0
    page_size=10
    while True:
        offset = page*page_size
        url = build_search_url(offset)
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        events = parse_list_page(resp.text)
        if not events: break
        for e in events:
            if e["detail_url"] not in events_dict:
                events_dict[e["detail_url"]] = e
        page+=1
        time.sleep(1)
    scraped_data = list(events_dict.values())
    # Dettaglio
    for i,e in enumerate(scraped_data,1):
        d_url = e.get("detail_url")
        if not d_url: continue
        resp = session.get(d_url, timeout=15)
        resp.raise_for_status()
        detail = parse_detail_page(resp.text, d_url)
        detail["application_form_link"] = get_external_application_link(detail["application_procedure_url"]) if detail["application_procedure_url"] else ""
        e.update(detail)
        time.sleep(1)
    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping(data=None):
    socketio.emit("log", {"message":"Avvio scraping..."})
    scrape_events()

@app.route("/download_csv")
def download_csv():
    if not scraped_data: return "Nessun dato disponibile",400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for","accessibility","working_language","organiser",
        "participation_fee","accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","training_description","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(scraped_data)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer,mimetype="text/csv",as_attachment=True,download_name="salto_events_complete.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
