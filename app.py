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

    return client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)


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

    # defaults
    data = {
        "type": "",
        "infopack_downloads": "",
        "application_procedure_url": "",
        "participants_no": "",
        "participants_from": "",
        "recommended_for": "",
        "accessibility": "",
        "working_language": "",
        "organiser": ""
    }

    # TYPE (category)
    cat = soup.select_one("span.h3.tool-item-category")
    if cat:
        data["type"] = cat.get_text(strip=True)

    # INFO PACK
    a = soup.select_one("ul.downloads-list a[href]")
    if a:
        href = a["href"]
        data["infopack_downloads"] = href if href.startswith("http") else BASE_URL + href

    # APPLICATION PROCEDURE
    for a in soup.select('a[href*="/application-procedure/"]'):
        href = a["href"]
        data["application_procedure_url"] = href if href.startswith("http") else BASE_URL + href
        break

    # TRAINING OVERVIEW (SOLID STRUCTURE)
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

    # ACCESSIBILITY
    h = soup.find("h3", string="Accessibility")
    if h:
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        data["accessibility"] = " ".join(parts)

    # WORKING LANGUAGE
    for p in soup.select(".microcopy"):
        if "language" in p.get_text().lower():
            data["working_language"] = p.get_text(strip=True)

    # ORGANISER
    for p in soup.find_all("p"):
        if p.get_text(strip=True).lower().startswith("organiser"):
            data["organiser"] = p.get_text(strip=True).split(":", 1)[-1].strip()

    return data


# ================= EXPORT =================

def export_to_google_sheet(events, log):
    sheet = get_gsheet()

    headers = [
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

    existing = sheet.get_all_records()
    existing_urls = {row["detail_url"] for row in existing if row.get("detail_url")}

    new_rows = []

    for ev in events:
        if ev["detail_url"] in existing_urls:
            continue

        row = [
            ev["title"],
            ev["detail_url"],
            ev["type"],
            ev["infopack_downloads"],
            ev["application_procedure_url"],
            ev["participants_no"],
            ev["participants_from"],
            ev["recommended_for"],
            ev["accessibility"],
            ev["working_language"],
            ev["organiser"],
            "FALSE"
        ]
        new_rows.append(row)
        log.append(f"➕ Aggiunto: {ev['title']}")

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="RAW")

    return len(new_rows)


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

        all_events = []
        offset = 0
        page = 1

        while True:
            yield f"📄 Pagina {page}\n"
            html = scrape_list_page(offset)
            events = parse_list(html)

            if not events:
                break

            for ev in events:
                r = requests.get(ev["detail_url"], timeout=30)
                detail = parse_detail(r.text)
                ev.update(detail)
                all_events.append(ev)
                yield f"   🔍 {ev['title']}\n"

            offset += 10
            page += 1
            time.sleep(1)

        yield f"\n📊 Eventi trovati: {len(all_events)}\n"

        added = export_to_google_sheet(all_events, [])
        yield f"✅ Nuovi eventi aggiunti: {added}\n"

    return Response(generate(), mimetype="text/plain")


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
