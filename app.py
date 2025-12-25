# ===================== GEVENT PATCH =====================
from gevent import monkey
monkey.patch_all()

# ===================== IMPORT =====================
import os
import time
import csv
import re
from io import StringIO, BytesIO
from datetime import date
from flask import Flask, render_template, jsonify, send_file, request
from flask_socketio import SocketIO
from bs4 import BeautifulSoup
import requests

# ===================== APP =====================
app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"
scraped_data = []

DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_EVENTS = 1000


# ===================== SEARCH URL =====================
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
    seen = set()
    events = []

    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a:
            continue

        title = a.get_text(strip=True)
        url = a.get("href", "")
        if not url.startswith("http"):
            url = BASE_URL + url

        if url in seen:
            continue
        seen.add(url)

        block = h3.parent.get_text("\n", strip=True)
        lines = [l for l in block.split("\n") if l.strip()]
        idx = lines.index(title)

        app_deadline = ""
        for l in lines:
            if "Application deadline" in l:
                app_deadline = l.split(":", 1)[-1].strip()

        events.append({
            "title": title,
            "type": lines[idx - 1] if idx > 0 else "",
            "dates": lines[idx + 1] if idx + 1 < len(lines) else "",
            "location": lines[idx + 2] if idx + 2 < len(lines) else "",
            "application_deadline": app_deadline,
            "detail_url": url,
        })

    return events


# ===================== PARSE DETAIL PAGE =====================
def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    # ===== training_summary (TESTO ASSOCIATO, NON LINK) =====
    training_summary = ""
    summary_div = soup.find("div", class_=re.compile(r"training-summary"))
    if summary_div:
        a = summary_div.find("a")
        if a:
            training_summary = a.get_text(strip=True)
        else:
            training_summary = summary_div.get_text(strip=True)

    # ===== training_description =====
    description_div = soup.find("div", class_=re.compile(r"training-description"))
    training_description = description_div.get_text("\n", strip=True) if description_div else ""

    participants_no = ""
    participants_from = ""
    recommended_for = ""
    working_language = ""
    organiser = ""

    lines = [l.strip() for l in training_description.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        l = lines[i].lower()

        if l == "for" and i + 1 < len(lines):
            participants_no = lines[i + 1].replace("participants", "").strip()
            i += 2
            countries = []
            while i < len(lines) and lines[i].lower() not in ["and recommended for"]:
                countries.append(lines[i])
                i += 1
            participants_from = " ".join(countries)
            continue

        if "and recommended for" in l and i + 1 < len(lines):
            recommended_for = lines[i + 1]

        if "working language" in l:
            working_language = lines[i].split(":", 1)[-1].strip()

        if l.startswith("organiser") and i + 1 < len(lines):
            organiser = lines[i + 1]

        i += 1

    # ===== accessibility =====
    accessibility = ""
    h = soup.find(lambda t: t.name in ["h3", "h4"] and "Accessibility info" in t.get_text())
    if h:
        accessibility = " ".join(
            sib.get_text(" ", strip=True)
            for sib in h.find_next_siblings()
            if not sib.name.startswith("h")
        )

    # ===== costs =====
    def section(title):
        h = soup.find(lambda t: t.name in ["h3", "h4"] and title in t.get_text())
        if not h:
            return ""
        return " ".join(
            sib.get_text(" ", strip=True)
            for sib in h.find_next_siblings()
            if not sib.name.startswith("h")
        )

    participation_fee = section("Participation fee")
    accommodation_food = section("Accommodation and food")
    travel_reimbursement = section("Travel reimbursement")

    # ===== downloads =====
    infopack_downloads = ""
    for a in soup.find_all("a", href=True):
        if "download" in a["href"]:
            infopack_downloads = a["href"]
            if not infopack_downloads.startswith("http"):
                infopack_downloads = BASE_URL + infopack_downloads
            break

    # ===== application procedure =====
    application_procedure_url = ""
    for a in soup.find_all("a", href=True):
        if "/application-procedure/" in a["href"]:
            application_procedure_url = a["href"]
            if not application_procedure_url.startswith("http"):
                application_procedure_url = BASE_URL + application_procedure_url
            break

    return {
        "participants_no": participants_no,
        "participants_from": participants_from,
        "recommended_for": recommended_for,
        "accessibility": accessibility,
        "working_language": working_language,
        "organiser": organiser,
        "participation_fee": participation_fee,
        "accommodation_food": accommodation_food,
        "travel_reimbursement": travel_reimbursement,
        "infopack_downloads": infopack_downloads,
        "application_procedure_url": application_procedure_url,
        "training_summary": training_summary,
        "training_description": training_description,
    }


# ===================== APPLICATION FORM LINK =====================
def get_external_application_link(url):
    if not url:
        return ""
    try:
        soup = BeautifulSoup(requests.get(url, timeout=10).text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(x in a["href"] for x in ["forms.gle", "typeform", "jotform"]):
                return a["href"]
    except Exception:
        pass
    return ""


# ===================== CSV SAVE =====================
def save_csv():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fields = [
        "title","type","dates","location","application_deadline",
        "participants_no","participants_from","recommended_for","accessibility",
        "working_language","organiser","participation_fee","accommodation_food",
        "travel_reimbursement","infopack_downloads","application_procedure_url",
        "application_form_link","detail_url",
        "training_summary","training_description"
    ]

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(scraped_data)


# ===================== SCRAPER =====================
def scrape_events(max_pages, max_events):
    global scraped_data
    scraped_data = []

    session = requests.Session()
    events = {}
    page = 0

    while page < max_pages:
        html = session.get(build_search_url(page * 10)).text
        for e in parse_list_page(html):
            if e["detail_url"] not in events:
                events[e["detail_url"]] = e
                if len(events) >= max_events:
                    break
        page += 1
        time.sleep(1)

    scraped_data = list(events.values())

    for e in scraped_data:
        html = session.get(e["detail_url"]).text
        detail = parse_detail_page(html, e["detail_url"])
        e.update(detail)
        e["application_form_link"] = get_external_application_link(detail["application_procedure_url"])
        time.sleep(1)

    save_csv()


# ===================== ROUTES =====================
@app.route("/api/scrape")
def api_scrape():
    scrape_events(
        int(request.args.get("max_pages", DEFAULT_MAX_PAGES)),
        int(request.args.get("max_events", DEFAULT_MAX_EVENTS))
    )
    return jsonify({"status": "ok", "count": len(scraped_data)})


@app.route("/download_csv")
def download_csv():
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)
    mem = BytesIO(buf.getvalue().encode("utf-8"))
    return send_file(mem, as_attachment=True, download_name="salto_events_complete.csv")


# ===================== RUN =====================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
