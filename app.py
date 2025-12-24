from gevent import monkey
monkey.patch_all()

import os
import time
import csv
import re
from io import StringIO, BytesIO
from datetime import date, datetime
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup

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

def setup_selenium():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=chrome_options)
    return driver

def parse_deadline(text):
    try:
        return datetime.strptime(text.strip(), "%d %B %Y").date()
    except Exception:
        return None

def scrape_events(max_pages=5):
    global scraped_data
    scraped_data = []
    driver = setup_selenium()

    socketio.emit("log", {"message": "Caricamento pagina con Selenium..."})
    driver.get(ETC_URL)
    time.sleep(5)  # let JS load

    # Scroll or load more if needed
    for _ in range(max_pages - 1):
        try:
            btn = driver.find_element(By.CSS_SELECTOR, ".search-result-list-navigation .next-page")
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            btn.click()
            time.sleep(3)
        except Exception:
            break

    soup = BeautifulSoup(driver.page_source, "html.parser")
    items = soup.select(".tool-item")  # each event

    socketio.emit("log", {"message": f"Trovati {len(items)} eventi nella lista..."})

    for idx, item in enumerate(items, start=1):
        title_tag = item.select_one("h2 a")
        if not title_tag:
            continue
        title = title_tag.text.strip()
        detail_url = title_tag["href"]
        if not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        type_ = item.select_one(".tool-item-category")
        type_text = type_.text.strip() if type_ else ""

        dates = item.select_one(".training-dates")
        dates_text = dates.text.strip() if dates else ""

        location = item.select_one(".training-location")
        location_text = location.text.strip() if location else ""

        # Deadline shown in list
        deadline_text = ""
        match_deadline = item.find(string=re.compile(r"Application deadline", re.IGNORECASE))
        if match_deadline:
            deadline_text = match_deadline.strip().split(":")[-1].strip()

        # Detail page
        try:
            driver.get(detail_url)
            time.sleep(3)
            det_soup = BeautifulSoup(driver.page_source, "html.parser")

            summary_tag = det_soup.select_one(".training-summary")
            training_summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            desc_tag = det_soup.select_one(".training-description")
            training_description = desc_tag.get_text(" ", strip=True) if desc_tag else ""

            scraped_data.append({
                "title": title,
                "type": type_text,
                "dates": dates_text,
                "location": location_text,
                "application_deadline": deadline_text,
                "training_summary": training_summary,
                "training_description": training_description,
                "detail_url": detail_url
            })
            socketio.emit("log", {"message": f"[{idx}] {title}"})
        except Exception as e:
            socketio.emit("log", {"message": f"Errore dettaglio {title}: {e}"})

    driver.quit()

    # Save CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")
    fieldnames = [
        "title","type","dates","location","application_deadline",
        "training_summary","training_description","detail_url"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    socketio.emit("log", {"message": f"Scraping completato! Totale: {len(scraped_data)} eventi"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})

# Routes

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
        "training_summary","training_description","detail_url"
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in scraped_data:
        writer.writerow(row)
    bytes_buffer = BytesIO(text_buffer.getvalue().encode("utf-8"))
    bytes_buffer.seek(0)
    return send_file(bytes_buffer, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="salto_events_complete.csv")

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
