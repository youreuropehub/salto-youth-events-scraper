# IMPORTANTE: monkey patching di gevent PRIMA di qualsiasi altro import
from gevent import monkey
monkey.patch_all()

import os
import time
import csv
from io import StringIO, BytesIO
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import re
import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
ETC_URL = "https://www.salto-youth.net/tools/european-training-calendar/browse/"
OUTPUT_DIR = "output"
scraped_data = []

# ================== Selenium Setup ==================

def setup_selenium():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=chrome_options)
    return driver

# ================== Helper functions ==================

def get_text_or_empty(soup, selector):
    tag = soup.select_one(selector)
    return tag.get_text(" ", strip=True) if tag else ""

def get_application_form_link(application_procedure_url):
    if not application_procedure_url:
        return ""
    try:
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        external_link = soup.find("a", string=re.compile(r"Proceed to the external", re.IGNORECASE))
        if external_link and external_link.get("href"):
            return external_link["href"]
        for a in soup.find_all("a", href=True):
            if any(domain in a["href"] for domain in ["forms.gle","google.com/forms","typeform.com","surveymonkey.com","jotform.com"]):
                return a["href"]
        return ""
    except Exception as e:
        print(f"Errore fetching application link: {e}")
        return ""

# ================== Scraper ==================

def scrape_events(max_pages=5):
    global scraped_data
    scraped_data = []

    socketio.emit("log", {"message": "Avvio scraping con Selenium..."})
    driver = setup_selenium()
    driver.get(ETC_URL)
    time.sleep(5)

    # Scorri pagine
    for _ in range(max_pages - 1):
        try:
            btn = driver.find_element(By.CSS_SELECTOR, ".search-result-list-navigation .next-page")
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            btn.click()
            time.sleep(3)
        except Exception:
            break

    soup = BeautifulSoup(driver.page_source, "html.parser")
    items = soup.select(".tool-item")
    socketio.emit("log", {"message": f"Trovati {len(items)} eventi nella lista..."})

    for idx, item in enumerate(items, start=1):
        try:
            title_tag = item.select_one("h2 a")
            if not title_tag:
                continue
            title = title_tag.text.strip()
            detail_url = title_tag["href"]
            if not detail_url.startswith("http"):
                detail_url = BASE_URL + detail_url

            type_ = get_text_or_empty(item, ".tool-item-category")
            dates_text = get_text_or_empty(item, ".training-dates")
            location_text = get_text_or_empty(item, ".training-location")

            # Application deadline dalla lista
            deadline_text = ""
            match_deadline = item.find(string=re.compile(r"Application deadline", re.IGNORECASE))
            if match_deadline:
                deadline_text = match_deadline.strip().split(":", 1)[-1].strip()

            # Detail page
            driver.get(detail_url)
            time.sleep(3)
            det_soup = BeautifulSoup(driver.page_source, "html.parser")

            # Training overview fields
            training_summary = get_text_or_empty(det_soup, ".training-summary")
            training_description = get_text_or_empty(det_soup, ".training-description")

            participants_no = participants_from = recommended_for = working_lang = organiser = ""
            # Training overview parsing
            overview_header = det_soup.find(lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.text)
            if overview_header:
                lines = [l.strip() for l in overview_header.find_next_siblings(text=True)]
                for line in lines:
                    if "participants" in line.lower():
                        participants_no = line.replace("participants", "").strip()
                    if "from" in line.lower():
                        participants_from = line.replace("from", "").strip()
                    if "recommended for" in line.lower():
                        recommended_for = line.split("recommended for")[-1].strip()
            # Working language & organiser
            wl_tag = det_soup.find(lambda tag: tag.name and "Working language" in tag.text)
            if wl_tag:
                working_lang = wl_tag.get_text(" ", strip=True).split(":",1)[-1].strip()
            org_tag = det_soup.find(lambda tag: tag.name and "Organiser" in tag.text)
            if org_tag:
                organiser = org_tag.get_text(" ", strip=True).split(":",1)[-1].strip()

            # Accessibility
            accessibility = ""
            acc_tag = det_soup.find(lambda tag: tag.name in ["h3","h4"] and "Accessibility info" in tag.text)
            if acc_tag:
                parts = []
                for sib in acc_tag.find_next_siblings():
                    if sib.name and sib.name.startswith("h"):
                        break
                    parts.append(sib.get_text(" ", strip=True))
                accessibility = " ".join(parts).strip()

            # Costs sections
            def section_after_heading(text):
                h = det_soup.find(lambda tag: tag.name in ["h3","h4"] and text in tag.text)
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

            # Infopack downloads
            infopack_downloads = ""
            downloads_heading = next((tag for tag in det_soup.find_all(['h3','h4','h5','strong','b','p']) if "Available downloads:" in tag.text), None)
            if downloads_heading:
                a_tag = downloads_heading.find_next("a", href=True)
                if a_tag:
                    infopack_downloads = a_tag["href"]
                    if not infopack_downloads.startswith("http"):
                        infopack_downloads = BASE_URL + infopack_downloads

            # Application procedure
            application_procedure_url = ""
            for link in det_soup.find_all("a", href=True):
                if "/application-procedure/" in link["href"]:
                    application_procedure_url = link["href"]
                    if not application_procedure_url.startswith("http"):
                        application_procedure_url = BASE_URL + application_procedure_url
                    break
            application_form_link = get_application_form_link(application_procedure_url)

            scraped_data.append({
                "title": title,
                "type": type_,
                "dates": dates_text,
                "location": location_text,
                "application_deadline": deadline_text,
                "training_summary": training_summary,
                "training_description": training_description,
                "participants_no": participants_no,
                "participants_from": participants_from,
                "recommended_for": recommended_for,
                "working_language": working_lang,
                "organiser": organiser,
                "accessibility": accessibility,
                "participation_fee": participation_fee,
                "accommodation_food": accommodation_food,
                "travel_reimbursement": travel_reimbursement,
                "infopack_downloads": infopack_downloads,
                "application_procedure_url": application_procedure_url,
                "application_form_link": application_form_link,
                "detail_url": detail_url
            })
            socketio.emit("log", {"message": f"[{idx}/{len(items)}] {title}"})

        except Exception as e:
            socketio.emit("log", {"message": f"Errore evento {title if 'title' in locals() else idx}: {e}"})

    driver.quit()

    # Salva CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description","participants_no",
        "participants_from","recommended_for","working_language",
        "organiser","accessibility","participation_fee",
        "accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"Scraping completato! Totale: {len(scraped_data)} eventi"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# ================== ROUTES ==================

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("start_scraping")
def handle_start_scraping(data=None):
    emit("log", {"message": "Avvio scraping..."})
    scrape_events()

@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400
    text_buffer = StringIO()
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description","participants_no",
        "participants_from","recommended_for","working_language",
        "organiser","accessibility","participation_fee",
        "accommodation_food","travel_reimbursement",
        "infopack_downloads","application_procedure_url","application_form_link","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in scraped_data:
        writer.writerow(row)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="salto_events_complete.csv")

@app.route("/api/scrape", methods=["POST", "GET"])
def api_scrape():
    scrape_events()
    return jsonify({
        "status": "ok",
        "count": len(scraped_data),
        "csv_path": f"{OUTPUT_DIR}/salto_events_complete.csv",
        "message": "Scraping completato. CSV salvato."
    })

# ================== RUN ==================

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
