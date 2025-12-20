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

# Variabile globale per memorizzare i risultati
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
        match = re.search(
            r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            text
        )
        if match:
            return match.group(1).strip()

    full_text = soup.get_text(" ", strip=True)
    match = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        full_text
    )
    return match.group(1).strip() if match else ""


# ================= PARSING =================

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    events = []

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

        type_ = lines[idx - 1] if idx - 1 >= 0 else ""
        dates = lines[idx + 1] if idx + 1 < len(lines) else ""
        location = lines[idx + 2] if idx + 2 < len(lines) else ""

        events.append({
            "title": title,
            "type": type_,
            "dates": dates,
            "location": location,
            "application_deadline": "",
            "detail_url": detail_url,
        })

    return events


def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts).strip()

    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i].lower()
        if line == "for" and i + 1 < len(lines) and "participants" in lines[i + 1].lower():
            participants_no = lines[i + 1].replace("participants", "").strip()
            j = i + 2
            countries = []
            while j < len(lines) and not lines[j].lower().startswith("and recommended"):
                countries.append(lines[j])
                j += 1
            participants_from = " ".join(countries).strip()
            i = j
            continue
        if "and recommended for" in line and i + 1 < len(lines):
            recommended_for = lines[i + 1].strip()
        if "working language(s):" in line:
            after = lines[i].split("Working language(s):", 1)[-1].strip()
            working_lang = after or (lines[i + 1].strip() if i + 1 < len(lines) else "")
        if line.startswith("organiser"):
            after = lines[i].split("Organiser", 1)[-1].replace(":", "").strip()
            organiser = after or (lines[i + 1].strip() if i + 1 < len(lines) else "")
        i += 1

    def section_after_heading(text):
        h = soup.find(lambda tag: tag.name in ["h3", "h4"] and text in tag.get_text())
        if not h:
            return ""
        parts = []
        for sib in h.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        return " ".join(parts).strip()

    accessibility = section_after_heading("Accessibility info")
    participation_fee = section_after_heading("Participation fee")
    accommodation_food = section_after_heading("Accommodation and food")
    travel_reimbursement = section_after_heading("Travel reimbursement")

    infopack_downloads = ""
    for tag in soup.find_all(["h3", "h4", "h5", "strong", "b", "p"]):
        if "Available downloads:" in tag.get_text():
            for sib in tag.find_next_siblings():
                if sib.name and sib.name.startswith("h"):
                    break
                a = sib.find("a", href=True)
                if a:
                    infopack_downloads = a["href"]
                    if not infopack_downloads.startswith("http"):
                        infopack_downloads = BASE_URL + infopack_downloads
                    break
            break

    application_procedure_url = ""
    for a in soup.find_all("a", href=True):
        if "/application-procedure/" in a["href"]:
            application_procedure_url = a["href"]
            if not application_procedure_url.startswith("http"):
                application_procedure_url = BASE_URL + application_procedure_url
            break

    application_deadline = extract_application_deadline(soup)

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
        "training_overview": training_overview,
        "application_deadline": application_deadline,
    }


def get_external_application_link(application_procedure_url):
    if not application_procedure_url:
        return ""
    try:
        resp = requests.get(application_procedure_url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if any(d in a["href"] for d in [
                "forms.gle", "google.com/forms",
                "typeform.com", "surveymonkey.com", "jotform.com"
            ]):
                return a["href"]
        return ""
    except Exception:
        return ""


# ================= CSV =================

def save_csv_to_file():
    if not scraped_data:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fieldnames = [
        "title","type","dates","location","application_deadline","training_overview",
        "participants_no","participants_from","recommended_for","accessibility",
        "working_language","organiser","participation_fee","accommodation_food",
        "travel_reimbursement","infopack_downloads",
        "application_procedure_url","application_form_link","detail_url"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})


# ================= SCRAPING =================

def scrape_events():
    global scraped_data
    scraped_data = []

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        max_retries=2
    )
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    events_dict = {}
    page = 0

    while page < 50:
        offset = page * 10
        socketio.emit("log", {"message": f"Caricamento pagina {page + 1}..."})

        resp = ses
