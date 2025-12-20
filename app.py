# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import csv
import re
from io import StringIO, BytesIO
from datetime import date

from gevent import sleep
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import requests


# ================= APP =================

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="gevent",
    ping_interval=25,
    ping_timeout=120
)

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []


# ================= UTILITY =================

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
        "&b_begin_date_before_day="
        "&b_begin_date_before_month="
        "&b_begin_date_before_year="
        "&b_end_date_after_day="
        "&b_end_date_after_month="
        "&b_end_date_after_year="
        "&b_end_date_before_day="
        "&b_end_date_before_month="
        "&b_end_date_before_year="
        "&b_activity_type="
        "&b_country="
        "&b_participating_countries="
        "&b_application_deadline_after_day={day}"
        "&b_application_deadline_after_month={month}"
        "&b_application_deadline_after_year={year}"
        "&b_application_deadline_before_day="
        "&b_application_deadline_before_month="
        "&b_application_deadline_before_year="
    )
    return base.format(offset=offset, day=day, month=month, year=year)


def extract_application_deadline(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(class_=re.compile(r"mrgn-btm")):
        text = tag.get_text(" ", strip=True)
        m = re.search(
            r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            text
        )
        if m:
            return m.group(1).strip()

    text = soup.get_text(" ", strip=True)
    m = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        text
    )
    return m.group(1).strip() if m else ""


# ================= PARSING =================

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    events = []

    for link in soup.select("a[href*='/tools/european-training-calendar/training/']"):
        title = link.get_text(strip=True)
        if not title:
            continue

        detail_url = link.get("href", "")
        if detail_url and not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        if detail_url in seen:
            continue
        seen.add(detail_url)

        container = link.find_parent()
        for _ in range(4):
            if container and container.name not in ("body", "html"):
                container = container.parent

        text = container.get_text("\n", strip=True) if container else ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        events.append({
            "title": title,
            "type": lines[idx - 1] if idx - 1 >= 0 else "",
            "dates": lines[idx + 1] if idx + 1 < len(lines) else "",
            "location": lines[idx + 2] if idx + 2 < len(lines) else "",
            "application_deadline": "",
            "detail_url": detail_url,
        })

    return events


def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    training_overview = ""
    h = soup.find(lambda t: t.name in ("h3", "h4") and "Training overview" in t.get_text())
    if h:
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts).strip()

    participants_no = participants_from = recommended_for = ""
    working_language = organiser = ""

    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        l = lines[i].lower()
        if l == "for" and i + 1 < len(lines) and "participants" in lines[i + 1].lower():
            participants_no = lines[i + 1].replace("participants", "").strip()
            j = i + 2
            countries = []
            while j < len(lines) and not lines[j].lower().startswith("and recommended"):
                countries.append(lines[j])
                j += 1
            participants_from = " ".join(countries)
            i = j
            continue
        if "and recommended for" in l and i + 1 < len(lines):
            recommended_for = lines[i + 1]
        if "working language(s):" in l:
            working_language = lines[i].split(":", 1)[-1].strip()
        if l.startswith("organiser"):
            organiser = lines[i].split(":", 1)[-1].strip()
        i += 1

    def section(name):
        h = soup.find(lambda t: t.name in ("h3", "h4") and name in t.get_text())
        if not h:
            return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        return " ".join(parts)

    accessibility = section("Accessibility info")
    participation_fee = section("Participation fee")
    accommodation_food = section("Accommodation and food")
    travel_reimbursement = section("Travel reimbursement")

    infopack = ""
    for t in soup.find_all(["h3", "h4", "p", "strong", "b"]):
        if "Available downloads:" in t.get_text():
            for sib in t.find_next_siblings():
                if sib.name and sib.name.startswith("h"):
                    break
                a = sib.find("a", href=True)
                if a:
                    infopack = a["href"]
                    if not infopack.startswith("http"):
                        infopack = BASE_URL + infopack
                    break
            break

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
        "infopack_downloads": infopack,
        "application_procedure_url": application_procedure_url,
        "training_overview": training_overview,
        "application_deadline": extract_application_deadline(soup),
    }


def get_external_application_link(url):
    if not url:
        return ""
    try:
        socketio.sleep(0)
        resp = requests.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(x in a["href"] for x in (
                "forms.gle", "google.com/forms",
                "typeform.com", "surveymonkey.com", "jotform.com"
            )):
                return a["href"]
        return ""
    except Exception:
        return ""


# ================= CSV =================

def save_csv():
    if not scraped_data:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=scraped_data[0].keys())
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato in {path}"})
    socketio.sleep(0)


# ================= SCRAPING =================

def scrape_events():
    global scraped_data
    scraped_data = []

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=2)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_map = {}
    page = 0

    while page < 50:
        socketio.emit("log", {"message": f"Caricamento pagina {page + 1}..."})
        socketio.sleep(0)

        resp = session.get(build_search_url(page * 10), timeout=15)
        events = parse_list_page(resp.text)
        if not events:
            break

        for e in events:
            events_map.setdefault(e["detail_url"], e)

        page += 1
        sleep(0.2)

    scraped_data = list(events_map.values())
    socketio.emit("log", {"message": f"Totale eventi trovati: {len(scraped_data)}"})
    socketio.sleep(0)

    for i, event in enumerate(scraped_data, 1):
        socketio.emit("log", {"message": f"[{i}/{len(scraped_data)}] {event['title']}"})
        socketio.sleep(0)

        try:
            resp = session.get(event["detail_url"], timeout=15)
            detail = parse_detail_page(resp.text, event["detail_url"])
            detail["application_form_link"] = get_external_application_link(
                detail.get("application_procedure_url")
            )
            event.update(detail)
        except Exception:
            pass

        sleep(0.2)

    save_csv()
    socketio.emit("scraping_done", {"count": len(scraped_data)})
    socketio.sleep(0)


# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def start_scraping():
    emit("log", {"message": "Avvio scraping..."})
    socketio.start_background_task(scrape_events)


@app.route("/api/scrape", methods=["POST", "GET"])
def api_scrape():
    socketio.start_background_task(scrape_events)
    return jsonify({"status": "started"})


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=scraped_data[0].keys())
    writer.writeheader()
    writer.writerows(scraped_data)

    return send_file(
        BytesIO(buf.getvalue().encode("utf-8")),
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="salto_events_complete.csv"
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)

