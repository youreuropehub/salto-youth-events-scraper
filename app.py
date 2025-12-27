import os
import json
import threading
import time
from datetime import date
from flask import Flask, Response, render_template
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================

BASE_URL = "https://www.salto-youth.net"
SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

HEADERS = [
    "title",
    "detail_url",
    "type",
    "dates",
    "location",
    "infopack_downloads",
    "application_procedure_url",
    "participants_no",
    "participants_from",
    "recommended_for",
    "accessibility",
    "working_language",
    "organiser",
    "posted_to_instagram",
]

scrape_running = False

# ================= GOOGLE SHEET =================

def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    values = sheet.get_all_values()
    if not values:
        sheet.append_row(HEADERS)

    return sheet


def get_existing_urls(sheet):
    values = sheet.get_all_values()
    if len(values) < 2:
        return set()

    header = values[0]
    if "detail_url" not in header:
        return set()

    idx = header.index("detail_url")
    return {
        row[idx]
        for row in values[1:]
        if len(row) > idx and row[idx]
    }

# ================= SCRAPER =================

def build_search_url(offset):
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


def absolute_url(url):
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return BASE_URL + url


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for item in soup.select(".tool-item"):
        a = item.select_one("h2 a")
        if not a:
            continue

        events.append({
            "title": a.get_text(strip=True),
            "detail_url": absolute_url(a["href"]),
            "type": item.select_one(".tool-item-category").get_text(strip=True) if item.select_one(".tool-item-category") else "",
            "dates": item.select_one("p.h5").get_text(strip=True) if item.select_one("p.h5") else "",
            "location": item.select_one(".microcopy").get_text(strip=True) if item.select_one(".microcopy") else "",
        })

    return events


def parse_detail(url):
    res = requests.get(url, timeout=30)
    soup = BeautifulSoup(res.text, "html.parser")

    def text(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else ""

    data = {
        "participants_no": "",
        "participants_from": "",
        "recommended_for": "",
        "accessibility": "",
        "working_language": "",
        "organiser": "",
        "infopack_downloads": "",
        "application_procedure_url": "",
    }

    data["accessibility"] = text("h3:contains('Accessibility') + p")
    data["working_language"] = text("p:contains('Working language')")
    data["organiser"] = text("p:contains('Organiser')")

    for a in soup.select("a[href]"):
        href = a["href"]
        if "application-procedure" in href:
            data["application_procedure_url"] = absolute_url(href)
        if "Download" in href or "download" in href:
            data["infopack_downloads"] = absolute_url(href)

    overview = soup.find("h3", string="Training overview")
    if overview:
        block = overview.find_next_sibling("p")
        if block:
            data["participants_no"] = block.get_text(strip=True)

    return data


# ================= CORE LOGIC =================

def scrape_events(log):
    sheet = get_gsheet()
    existing_urls = get_existing_urls(sheet)

    offset = 0
    new_count = 0

    while True:
        log(f"📄 Pagina {offset // 10 + 1}")
        url = build_search_url(offset)
        res = requests.get(url, timeout=30)

        events = parse_list_page(res.text)
        if not events:
            break

        for ev in events:
            log(f"🔍 {ev['title']}")

            if ev["detail_url"] in existing_urls:
                continue

            detail = parse_detail(ev["detail_url"])
            row = [
                ev["title"],
                ev["detail_url"],
                ev["type"],
                ev["dates"],
                ev["location"],
                detail["infopack_downloads"],
                detail["application_procedure_url"],
                detail["participants_no"],
                detail["participants_from"],
                detail["recommended_for"],
                detail["accessibility"],
                detail["working_language"],
                detail["organiser"],
                "FALSE",
            ]

            sheet.append_row(row, value_input_option="RAW")
            existing_urls.add(ev["detail_url"])
            new_count += 1

        offset += 10
        time.sleep(1)

    log(f"✅ Nuovi eventi aggiunti: {new_count}")


# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start")
def api_start():
    global scrape_running

    if scrape_running:
        return {"status": "already_running"}

    def bg():
        global scrape_running
        scrape_running = True
        try:
            scrape_events(lambda x: None)
        finally:
            scrape_running = False

    threading.Thread(target=bg, daemon=True).start()
    return {"status": "started"}


@app.route("/api/scrape")
def api_scrape():
    def stream():
        global scrape_running
        scrape_running = True

        yield "🚀 Scraping avviato\n\n"

        def log(msg):
            yield_msg = f"{msg}\n\n"
            stream.buffer.append(yield_msg)

        stream.buffer = []

        def logger(msg):
            stream.buffer.append(f"{msg}\n\n")

        try:
            scrape_events(logger)
        finally:
            scrape_running = False

        for msg in stream.buffer:
            yield msg

    return Response(stream(), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
