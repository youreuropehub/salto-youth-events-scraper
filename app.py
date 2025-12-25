# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
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

scraped_data = []
OUTPUT_DIR = "output"

DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_EVENTS = 1000


def build_search_url(offset: int) -> str:
    today = date.today()
    return (
        "https://www.salto-youth.net/tools/european-training-calendar/browse/"
        f"?b_offset={offset}&b_limit=10"
        "&b_order=applicationDeadline"
        "&b_keyword="
        f"&b_begin_date_after_day={today.day}"
        f"&b_begin_date_after_month={today.month}"
        f"&b_begin_date_after_year={today.year}"
        f"&b_application_deadline_after_day={today.day}"
        f"&b_application_deadline_after_month={today.month}"
        f"&b_application_deadline_after_year={today.year}"
    )


# ===================== PARSE LIST PAGE =====================
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

        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        type_ = lines[idx - 1] if idx > 0 else ""
        dates = lines[idx + 1] if idx + 1 < len(lines) else ""
        location = lines[idx + 2] if idx + 2 < len(lines) else ""

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

    # Metodo 2: tutti i link diretti
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

        type_ = lines[idx - 1] if idx > 0 else ""
        dates = lines[idx + 1] if idx + 1 < len(lines) else ""
        location = lines[idx + 2] if idx + 2 < len(lines) else ""

        app_deadline = ""
        for i, l in enumerate(lines):
            if "Application deadline" in l:
                if i + 1 < len(lines):
                    app_deadline = lines[i + 1]
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


# ===================== PARSE DETAIL PAGE COMPLETO =====================
def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    # ---------- Training overview ----------
    training_overview = ""
    h3_overview = soup.find(lambda t: t.name in ["h3", "h4"] and "Training overview" in t.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts).strip()

    # Nuovi campi
    training_description = training_overview
    training_summary = training_overview.split("\n")[0] if training_overview else ""

    participants_no = ""
    participants_from = ""
    recommended_for = ""
    working_lang = ""
    organiser = ""

    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i].lower()
        if line == "for" and i + 1 < len(lines):
            participants_no = lines[i + 1].replace("participants", "").strip()
            j = i + 2
            countries = []
            while j < len(lines):
                if lines[j].lower() == "from":
                    j += 1
                    continue
                if lines[j].lower().startswith("and recommended"):
                    break
                countries.append(lines[j])
                j += 1
            participants_from = " ".join(countries).strip()
            i = j
            continue
        if "and recommended for" in line and i + 1 < len(lines):
            recommended_for = lines[i + 1].strip()
        if "working language(s):" in line:
            after = lines[i].split("Working language(s):",1)[-1].strip()
            if after:
                working_lang = after
            elif i + 1 < len(lines):
                working_lang = lines[i + 1].strip()
        if line.startswith("organiser"):
            after = lines[i].split("Organiser",1)[-1].replace(":","").strip()
            if after:
                organiser = after
            elif i + 1 < len(lines):
                organiser = lines[i + 1].strip()
        i += 1

    # ---------- Accessibility ----------
    accessibility = ""
    h_acc = soup.find(lambda t: t.name in ["h3","h4"] and "Accessibility info" in t.get_text())
    if h_acc:
        parts = []
        for sib in h_acc.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        accessibility = " ".join(parts).strip()

    # ---------- Costs ----------
    def section_after_heading(text):
        h = soup.find(lambda t: t.name in ["h3","h4"] and text in t.get_text())
        if not h: return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ",strip=True))
        return " ".join(parts).strip()

    participation_fee = section_after_heading("Participation fee")
    accommodation_food = section_after_heading("Accommodation and food")
    travel_reimbursement = section_after_heading("Travel reimbursement")

    # ---------- Available downloads ----------
    infopack_downloads = ""
    downloads_heading = None
    for tag in soup.find_all(['h3','h4','h5','strong','b','p']):
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
    if not infopack_downloads:
        for element in soup.find_all(string=re.compile(r"Available downloads:")):
            parent = element.parent
            for link in parent.find_next_siblings():
                if link.name == "a" and link.get("href"):
                    href = link["href"]
                    if not href.startswith("http"):
                        href = BASE_URL + href
                    infopack_downloads = href
                    break
                first_a = link.find("a", href=True)
                if first_a:
                    href = first_a["href"]
                    if not href.startswith("http"):
                        href = BASE_URL + href
                    infopack_downloads = href
                    break
            if infopack_downloads:
                break

    # ---------- Application procedure URL ----------
    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_href = link["href"]
            if not app_href.startswith("http"):
                app_href = BASE_URL + app_href
            application_procedure_url = app_href
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
        "training_summary": training_summary,
        "training_description": training_description,
    }


def get_external_application_link(url):
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(d in a["href"] for d in ["forms.gle","google.com/forms","typeform.com","jotform.com"]):
                return a["href"]
    except Exception:
        return ""
    return ""


def save_csv_to_file():
    if not scraped_data:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for","accessibility",
        "working_language","organiser","participation_fee","accommodation_food",
        "travel_reimbursement","infopack_downloads","application_procedure_url",
        "application_form_link","detail_url",
        "training_summary","training_description"
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)
    socketio.emit("log", {"message": f"CSV salvato in {path}"})


# ===================== SCRAPER =====================
def scrape_events(max_pages=DEFAULT_MAX_PAGES, max_events=DEFAULT_MAX_EVENTS):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0"})
    events_dict = {}

    page = 0
    page_size = 10

    while page < max_pages:
        offset = page*page_size
        socketio.emit("log", {"message": f"Pagina {page+1} (offset {offset})"})
        try:
            resp = session.get(build_search_url(offset), timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"Errore pagina {page+1}: {e}"})
            break

        events = parse_list_page(resp.text)
        if not events:
            break

        for event in events:
            url = event["detail_url"]
            if url not in events_dict:
                events_dict[url] = event
                if len(events_dict) >= max_events:
                    break

        if len(events_dict) >= max_events:
            break
        page += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())

    for i, event in enumerate(scraped_data, start=1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        try:
            resp = session.get(event["detail_url"], timeout=15)
            resp.raise_for_status()
            detail = parse_detail_page(resp.text, event["detail_url"])
            event.update(detail)
            event["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
        except Exception as e:
            print(f"Errore dettaglio {event['detail_url']}: {e}")
        time.sleep(1)

    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})


# ===================== ROUTES =====================
@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping(data):
    scrape_events(
        int(data.get("max_pages", DEFAULT_MAX_PAGES)),
        int(data.get("max_events", DEFAULT_MAX_EVENTS))
    )


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato", 400

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    mem = BytesIO(buffer.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True,
                     download_name="salto_events_complete.csv",
                     mimetype="text/csv")


@app.route("/api/scrape", methods=["GET", "POST"])
def api_scrape():
    scrape_events(
        int(request.values.get("max_pages", DEFAULT_MAX_PAGES)),
        int(request.values.get("max_events", DEFAULT_MAX_EVENTS))
    )
    return jsonify({"status":"ok","count":len(scraped_data)})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
