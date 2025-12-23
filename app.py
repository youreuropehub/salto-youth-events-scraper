# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file, request
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
        f"{BASE_URL}/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10&b_order=applicationDeadline"
        f"&b_keyword="
        f"&b_begin_date_after_day={day}&b_begin_date_after_month={month}&b_begin_date_after_year={year}"
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
        lines = [l.strip() for l in block.get_text("\n",strip=True).split("\n") if l.strip()]
        try: idx = lines.index(title)
        except ValueError: idx=0

        type_ = lines[idx-1] if idx>0 else ""
        dates = lines[idx+1] if idx+1<len(lines) else ""
        location = lines[idx+2] if idx+2<len(lines) else ""

        # ---------- Nuovo: application_deadline dalla lista eventi ----------
        application_deadline=""
        callout = block.find("div", class_="callout-module")
        if callout:
            p_tags = callout.find_all("p")
            for i, p in enumerate(p_tags):
                if "Application deadline" in p.get_text(strip=True):
                    if i+1 < len(p_tags):
                        application_deadline = p_tags[i+1].get_text(strip=True)
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

def parse_detail_page(html):
    soup = BeautifulSoup(html,"html.parser")
    training_description=""
    desc_div = soup.find("div", class_="training-description")
    if desc_div:
        training_description = desc_div.get_text("\n",strip=True)
    return {"training_description": training_description}

def get_external_application_link(url):
    if not url: return ""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text,"html.parser")
        link = soup.find("a", string=lambda t: t and "Proceed to the external" in t)
        if link and link.get("href"): return link["href"]
        for a in soup.find_all("a", href=True):
            if any(d in a["href"] for d in ["forms.gle","typeform.com","jotform.com","surveymonkey.com"]):
                return a["href"]
    except: return ""
    return ""

def save_csv_to_file():
    if not scraped_data: return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR,"salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_description","detail_url"
    ]
    with open(csv_path,"w",newline="",encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

def scrape_events(max_pages:int):
    global scraped_data
    scraped_data=[]
    session = requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0"})
    events_dict={}
    page=0
    page_size=10

    while page<max_pages:
        offset=page*page_size
        try:
            resp = session.get(build_search_url(offset),timeout=15)
            resp.raise_for_status()
        except Exception as e:
            break

        events=parse_list_page(resp.text)
        if not events: break
        for e in events:
            detail_url=e["detail_url"]
            if detail_url not in events_dict:
                events_dict[detail_url]=e
        page+=1
        time.sleep(1)

    scraped_data=list(events_dict.values())

    # Recupero training_description da dettaglio
    for e in scraped_data:
        url=e["detail_url"]
        try:
            resp=session.get(url,timeout=15)
            resp.raise_for_status()
            detail=parse_detail_page(resp.text)
            e.update(detail)
        except:
            e.update({"training_description":""})
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
        max_pages=int(data.get("max_pages",1))
        if max_pages<1: max_pages=1
    except:
        max_pages=1
    scrape_events(max_pages)

@app.route("/download_csv")
def download_csv():
    if not scraped_data: return "Nessun dato disponibile",400
    text_buffer = StringIO()
    fieldnames = ["title","type","dates","location","application_deadline","training_description","detail_url"]
    writer = csv.DictWriter(text_buffer,fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(scraped_data)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer,mimetype="text/csv",as_attachment=True,download_name="salto_events_complete.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
