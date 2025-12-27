import os
import json
import time
import requests
from flask import Flask, Response
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================

BASE_URL = "https://www.salto-youth.net"
BROWSE_URL = "https://www.salto-youth.net/tools/european-training-calendar/browse/"

SPREADSHEET_NAME = "SALTO-EVENTS"
WORKSHEET_NAME = "SALTO-EVENTS"

HEADERS = [
    "title",
    "detail_url",
    "type",
    "infopack_downloads",
    "application_procedure_url",
    "participants_no",
    "participants_from",
    "recommended_for",
    "accessibility",
    "working_language",
    "organiser",
    "posted_to_instagram"
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ================= GOOGLE SHEET =================

def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    creds_dict = json.loads(creds_json)

    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

    # CREA HEADER SE FOGLIO VUOTO
    if sheet.row_count == 0 or not sheet.get_all_values():
        sheet.append_row(HEADERS)

    return sheet


# ================= SCRAPING =================

def scrape_list_page(offset):
    params = {
        "b_offset": offset,
        "b_limit": 10,
        "b_order": "applicationDeadline"
    }
    r = requests.get(BROWSE_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for item in soup.select(".search-results-list .tool-item"):
        a = item.select_one("h2 a")
        if not a:
            continue

        title = a.get_text(strip=True)
        detail_url = a["href"]
        if not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        events.append({
            "title": title,
            "detail_url": detail_url
        })

    return events


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")

    data = {k: "" for k in HEADERS}
    data["posted_to_instagram"] = "FALSE"

    cat = soup.select_one("span.h3.tool-item-category")
    if cat:
        data["type"] = cat.get_text(strip=True)

    a = soup.select_one("ul.downloads-list a[href]")
    if a:
        data["infopack_downloads"] = BASE_URL + a["href"] if a["href"].startswith("/") else a["href"]

    for a in soup.select('a[href*="/application-procedure/"]'):
        data["application_procedure_url"] = BASE_URL + a["href"] if a["href"].startswith("/") else a["href"]
        break

    h = soup.find("h3", string="Training overview")
    if h:
        for p in h.find_next_siblings("p"):
            txt = p.get_text(" ", strip=True)

            if p.find("span", string="for"):
                data["participants_no"] = txt.replace("participants", "").strip()

            elif p.find("span", string="from"):
                data["participants_from"] = txt.replace("from", "").strip()

            elif "recommended for" in txt.lower():
                nxt = p.find_next_sibling("p")
                if nxt:
                    data["recommended_for"] = nxt.get_text(" ", strip=True)

    h = soup.find("h3", string="Accessibility")
    if h:
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        data["accessibility"] = " ".join(parts)

    for p in soup.select(".microcopy"):
        if "language" in p.get_text().lower():
            data["working_language"] = p.get_text(strip=True)

    for p in soup.find_all("p"):
        if p.get_text(strip=True).lower().startswith("organiser"):
            data["organiser"] = p.get_text(strip=True).split(":", 1)[-1].strip()

    return data


# ================= EXPORT =================

def export_to_google_sheet(events, log):
    sheet = get_gsheet()

    rows = sheet.get_all_records()
    existing_urls = {r["detail_url"] for r in rows if r.get("detail_url")}

    added = 0

    for ev in events:
        if ev["detail_url"] in existing_urls:
            log.append("🔁 GIÀ ESISTENTE")
            continue

        row = [ev.get(h, "") for h in HEADERS]
        sheet.append_row(row)
        added += 1
        log.append("➕ AGGIUNTO")

    return added


# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def index():
    return """
    <h2>SALTO Events Scraper</h2>
    <a href="/api/scrape">▶ Avvia scraping</a>
    """

@app.route("/api/scrape")
def api_scrape():

    def generate():
        yield "🚀 Scraping avviato\n\n"

        offset = 0
        page = 1
        all_events = []

        while True:
            yield f"📄 Pagina {page}\n"
            html = scrape_list_page(offset)
            events = parse_list(html)

            if not events:
                break

            for ev in events:
                yield f"🔍 {ev['title']}\n"
                r = requests.get(ev["detail_url"], timeout=30)
                ev.update(parse_detail(r.text))
                all_events.append(ev)

            offset += 10
            page += 1
            time.sleep(1)

        yield f"\n📊 Eventi trovati: {len(all_events)}\n\n"

        log = []
        added = export_to_google_sheet(all_events, log)

        for l in log:
            yield f"{l}\n"

        yield f"\n✅ Nuovi eventi aggiunti: {added}\n"

    return Response(generate(), mimetype="text/plain")


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
