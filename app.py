# =========================
# EVENTLET MUST BE FIRST
# =========================
import eventlet
eventlet.monkey_patch()

# =========================
# STANDARD IMPORTS
# =========================
import csv
import os
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_file
from flask_socketio import SocketIO

# =========================
# FLASK APP
# =========================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

CSV_FILE = "events.csv"
BASE_URL = "https://www.salto-youth.net"

# =========================
# SCRAPER
# =========================
def scrape_events():
    events = []

    list_url = f"{BASE_URL}/tools/european-training-calendar/"
    response = requests.get(list_url, timeout=30)
    soup = BeautifulSoup(response.text, "lxml")

    event_links = [
        BASE_URL + a["href"]
        for a in soup.select("a.tc-title")
        if a.get("href")
    ]

    for idx, detail_url in enumerate(event_links, start=1):
        socketio.emit("log", f"[{idx}/{len(event_links)}] {detail_url}")

        try:
            r = requests.get(detail_url, timeout=30)
            s = BeautifulSoup(r.text, "lxml")

            def text_or_empty(selector):
                el = s.select_one(selector)
                return el.get_text(strip=True) if el else ""

            # =========================
            # REQUIRED FIELDS
            # =========================
            title = text_or_empty("h1")
            type_ = text_or_empty(".training-type")
            dates = text_or_empty(".training-dates")
            location = text_or_empty(".training-location")
            application_deadline = text_or_empty(".application-deadline")

            # =========================
            # NEW FIELDS (FIXED)
            # =========================
            training_summary = text_or_empty(
                "div.training-summary"
            )

            training_description = (
                s.select_one("div.training-description")
                .get_text("\n", strip=True)
                if s.select_one("div.training-description")
                else ""
            )

            # =========================
            # OTHER FIELDS (UNCHANGED)
            # =========================
            training_overview = text_or_empty(".training-overview")
            participants_no = text_or_empty(".participants-no")
            participants_from = text_or_empty(".participants-from")
            recommended_for = text_or_empty(".recommended-for")
            accessibility = text_or_empty(".accessibility")
            working_language = text_or_empty(".working-language")
            organiser = text_or_empty(".organiser")
            participation_fee = text_or_empty(".participation-fee")
            accommodation_food = text_or_empty(".accommodation-food")
            travel_reimbursement = text_or_empty(".travel-reimbursement")

            infopack = s.select_one("a.infopack")
            infopack_downloads = BASE_URL + infopack["href"] if infopack else ""

            application_procedure_url = (
                BASE_URL + s.select_one("a.application-procedure")["href"]
                if s.select_one("a.application-procedure")
                else ""
            )

            application_form_link = (
                s.select_one("a.application-form")["href"]
                if s.select_one("a.application-form")
                else ""
            )

            events.append({
                "title": title,
                "type": type_,
                "dates": dates,
                "location": location,
                "application_deadline": application_deadline,
                "training_summary": training_summary,
                "training_description": training_description,
                "training_overview": training_overview,
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
                "application_form_link": application_form_link,
                "detail_url": detail_url,
            })

        except Exception as e:
            socketio.emit("log", f"✗ Error: {e}")

    # =========================
    # WRITE CSV
    # =========================
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=events[0].keys())
        writer.writeheader()
        writer.writerows(events)

    return events

# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    return "SALTO scraper is running."

@app.route("/api/scrape")
def api_scrape():
    data = scrape_events()
    return jsonify({"events": len(data)})

@app.route("/download_csv")
def download_csv():
    return send_file(CSV_FILE, as_attachment=True)

@app.route("/api/scrape_and_download")
def scrape_and_download():
    scrape_events()
    return send_file(CSV_FILE, as_attachment=True)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=10000)
