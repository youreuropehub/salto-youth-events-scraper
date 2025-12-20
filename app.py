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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

BASE_URL = "https://www.salto-youth.net"
OUTPUT_DIR = "output"

scraped_data = []

# ==================== UTILITY ====================
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
            r"Application deadline\s*(?:\(24h UTC\))?\s*[:]\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            text
        )
        if match:
            return match.group(1).strip()
    full_text = soup.get_text(" ", strip=True)
    match = re.search(
        r"Application deadline\s*(?:\(24h UTC\))?\s*[:]\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        full_text
    )
    if match:
        return match.group(1).strip()
    return ""

# ==================== PARSING ====================
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
            "training_summary": "",
            "training_description": ""
        })

    return events

def parse_detail_page(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")

    # ---------- Training summary ----------
    training_summary = ""
    summary_tag = soup.find("div", class_=re.compile(r"\btraining-summary\b"))
    if summary_tag:
        training_summary = summary_tag.get_text(" ", strip=True)

    # ---------- Training description ----------
    training_description = ""
    description_tag = soup.find("div", class_=re.compile(r"\btraining-description\b"))
    if description_tag:
        training_description = description_tag.get_text("\n", strip=True)

    # ---------- Training overview ----------
    training_overview = ""
    h3_overview = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text())
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join([l.strip() for l in "\n".join(parts).splitlines() if l.strip()])

    # ---------- Other fields ----------
    participants_no = participants_from = recommended_for = working_lang = organiser = ""
    lines = [l.strip() for l in training_description.splitlines() if l.strip()]
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
            working_lang = after if after else (lines[i + 1].strip() if i + 1 < len(lines) else "")
        if line.startswith("organiser"):
            after = lines[i].split("Organiser", 1)[-1].replace(":", "").strip()
            organiser = after if after else (lines[i + 1].strip() if i + 1 < len(lines) else "")
        i += 1

    # ---------- Accessibility ----------
    accessibility = ""
    h_acc = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Accessibility info" in tag.get_text())
    if h_acc:
        parts = []
        for sib in h_acc.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        accessibility = " ".join(parts).strip()

    # ---------- Costs ----------
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

    participation_fee = section_after_heading("Participation fee")
    accommodation_food = section_after_heading("Accommodation and food")
    travel_reimbursement = section_after_heading("Travel reimbursement")

    # ---------- Downloads ----------
    infopack_downloads = ""
    downloads_heading = None
    for tag in soup.find_all(['h3', 'h4', 'h5', 'strong', 'b', 'p']):
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

    # ---------- Application procedure ----------
    application_procedure_url = ""
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_href = link["href"]
            if not app_href.startswith("http"):
                app_href = BASE_URL + app_href
            application_procedure_url = app_href
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
        "training_summary": training_summary,
        "training_overview": training_overview,
        "training_description": training_description,
        "application_deadline": application_deadline,
    }
