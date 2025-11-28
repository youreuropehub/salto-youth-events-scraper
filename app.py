import os
import time
import csv
import re
from io import StringIO, BytesIO
from flask import Flask, render_template, jsonify, send_file
from flask_socketio import SocketIO, emit
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "https://www.salto-youth.net"
SEARCH_URL = BASE_URL + "/tools/european-training-calendar/browse/"

# Variabile globale per memorizzare i risultati
scraped_data = []

# Cartella per salvare il CSV
OUTPUT_DIR = "output"


def parse_list_page(html):
    """
    Estrae gli eventi dalla pagina di lista SALTO (European Training Calendar).
    Prova due metodi:
    1. Cerca <h3> con link (metodo vecchio script)
    2. Cerca tutti i link che puntano a /tools/european-training-calendar/training/
    """
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_urls = set()

    # METODO 1: cerca <h3> con link
    for h3 in soup.find_all("h3"):
        a = h3.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        url = a.get("href", "").strip()
        if url and not url.startswith("http"):
            url = BASE_URL + url

        if url in seen_urls:
            continue
        seen_urls.add(url)

        block = h3.parent
        text = block.get_text("\n", strip=True)
        lines = [l for l in text.split("\n") if l.strip()]

        try:
            idx = lines.index(title)
        except ValueError:
            idx = 0

        type_ = ""
        dates = ""
        location = ""
        app_deadline = ""

        if idx > 0:
            type_ = lines[idx - 1]
        if idx + 1 < len(lines):
            dates = lines[idx + 1]
        if idx + 2 < len(lines):
            location = lines[idx + 2]

        for l in lines:
            if "Application deadline" in l:
                app_deadline = l.split(":", 1)[-1].strip()
                break

        events.append(
            {
                "title": title,
                "type": type_,
                "dates": dates,
                "location": location,
                "application_deadline": app_deadline,
                "detail_url": url,
            }
        )

    # METODO 2: se non ha trovato nulla con <h3>, cerca tutti i link diretti
    if not events:
        for link in soup.select(
                "a[href*='/tools/european-training-calendar/training/']"
        ):
            title = link.get_text(strip=True)
            if not title:
                continue

            detail_url = link.get("href", "").strip()
            if detail_url and not detail_url.startswith("http"):
                detail_url = BASE_URL + detail_url

            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            # Contenitore principale del blocco evento
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

            type_ = ""
            dates = ""
            location = ""
            app_deadline = ""

            if idx - 1 >= 0:
                type_ = lines[idx - 1]
            if idx + 1 < len(lines):
                dates = lines[idx + 1]
            if idx + 2 < len(lines):
                location = lines[idx + 2]

            for i, line in enumerate(lines):
                if "Application deadline" in line:
                    if i + 1 < len(lines):
                        app_deadline = lines[i + 1]
                    break

            events.append(
                {
                    "title": title,
                    "type": type_,
                    "dates": dates,
                    "location": location,
                    "application_deadline": app_deadline,
                    "detail_url": detail_url,
                }
            )

    return events


def parse_detail_page(html, detail_url):
    """
    Estrae info aggiuntive dalla pagina di dettaglio:
    - participants_no, participants_from, recommended_for
    - accessibility, working_language, organiser
    - participation_fee, accommodation_food, travel_reimbursement
    - infopack_downloads (tutti i link nella sezione "Available downloads")
    - application_procedure_url (link "Apply now!")
    """
    soup = BeautifulSoup(html, "html.parser")

    # ---------- Training overview (blocchetto centrale) ----------
    training_overview = ""
    h3_overview = soup.find(
        lambda tag: tag.name in ["h3", "h4"] and "Training overview" in tag.get_text()
    )
    if h3_overview:
        parts = []
        for sib in h3_overview.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text("\n", strip=True))
        training_overview = "\n".join(parts).strip()

    participants_no = ""
    participants_from = ""
    recommended_for = ""
    working_lang = ""
    organiser = ""

    lines = [l.strip() for l in training_overview.splitlines() if l.strip()]

    # Cerca "for" + "X participants" (possono essere su righe separate)
    i = 0
    while i < len(lines):
        line = lines[i]
        low = line.lower()

        # Cerca "for"
        if low == "for":
            # La riga successiva dovrebbe essere "X participants"
            if i + 1 < len(lines) and "participants" in lines[i + 1].lower():
                participants_no = lines[i + 1].replace("participants", "").strip()

                # Ora cerca "from" nelle righe successive
                j = i + 2
                countries = []
                while j < len(lines):
                    if lines[j].lower() == "from":
                        j += 1
                        continue
                    if lines[j].lower().startswith("and recommended"):
                        break
                    # Raccogli tutte le righe fino a "and recommended for"
                    countries.append(lines[j])
                    j += 1

                participants_from = " ".join(countries).strip()
                i = j
                continue

        # recommended for
        if "and recommended for" in low:
            # L'elenco è sulla riga successiva
            if i + 1 < len(lines):
                recommended_for = lines[i + 1].strip()

        # working language
        if "working language(s):" in low:
            # se la lingua è sulla stessa riga
            after = line.split("Working language(s):", 1)[-1].strip()
            if after:
                working_lang = after
            elif i + 1 < len(lines):
                # altrimenti riga successiva
                working_lang = lines[i + 1].strip()

        # Organiser:
        if low.startswith("organiser"):
            # stesso pattern: può essere sulla riga stessa o successiva
            after = line.split("Organiser", 1)[-1].replace(":", "").strip()
            if after:
                organiser = after
            elif i + 1 < len(lines):
                organiser = lines[i + 1].strip()

        i += 1

    # ---------- Accessibility info ----------
    accessibility = ""
    h_acc = soup.find(
        lambda tag: tag.name in ["h3", "h4"] and "Accessibility info" in tag.get_text()
    )
    if h_acc:
        parts = []
        for sib in h_acc.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            parts.append(sib.get_text(" ", strip=True))
        accessibility = " ".join(parts).strip()

    # ---------- Costs section ----------
    def section_after_heading(text):
        h = soup.find(
            lambda tag: tag.name in ["h3", "h4"] and text in tag.get_text()
        )
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

    # ---------- Available downloads (infopack) ----------
    # Cerca un elemento specifico (non <html>!) che contiene "Available downloads:"
    # Salva solo l'URL del PRIMO link trovato
    infopack_downloads = ""

    # Strategia 1: cerca un heading o strong/b con "Available downloads"
    downloads_heading = None
    for tag in soup.find_all(['h3', 'h4', 'h5', 'strong', 'b', 'p']):
        if "Available downloads:" in tag.get_text():
            downloads_heading = tag
            break

    if downloads_heading:
        # Naviga i siblings successivi fino al prossimo heading
        for sib in downloads_heading.find_next_siblings():
            if sib.name and sib.name.startswith("h"):
                break
            # Trova il PRIMO link in questo sibling
            first_link = sib.find("a", href=True)
            if first_link:
                href = first_link["href"]
                if not href.startswith("http"):
                    href = BASE_URL + href
                infopack_downloads = href
                break  # Prendi solo il primo link

    # Strategia 2: se non trovato, cerca nel parent del testo "Available downloads:"
    if not infopack_downloads:
        for element in soup.find_all(string=re.compile(r"Available downloads:")):
            parent = element.parent
            # Cerca link nel parent e nei siblings
            for link in parent.find_next_siblings():
                if link.name == "a" and link.get("href"):
                    href = link["href"]
                    if not href.startswith("http"):
                        href = BASE_URL + href
                    infopack_downloads = href
                    break
                # Cerca anche dentro i siblings
                first_a = link.find("a", href=True)
                if first_a:
                    href = first_a["href"]
                    if not href.startswith("http"):
                        href = BASE_URL + href
                    infopack_downloads = href
                    break
            if infopack_downloads:
                break

    # ---------- Application procedure URL ("Apply now!") ----------
    application_procedure_url = ""

    # Il link contiene "Apply now!" ma anche altro testo attaccato
    # Cerca un link il cui href contiene "/application-procedure/"
    for link in soup.find_all("a", href=True):
        if "/application-procedure/" in link["href"]:
            app_href = link["href"]
            if not app_href.startswith("http"):
                app_href = BASE_URL + app_href
            application_procedure_url = app_href
            break

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
    }


def get_external_application_link(application_procedure_url):
    """
    Segue la pagina /application-procedure/... e estrae il link del bottone
    "Proceed to the external online application" (es. Google Forms)
    """
    if not application_procedure_url:
        return ""

    try:
        resp = requests.get(application_procedure_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Cerca il link "Proceed to the external online application"
        external_link = soup.find(
            "a", string=re.compile(r"Proceed to the external", re.IGNORECASE)
        )
        if external_link and external_link.get("href"):
            return external_link["href"]

        # Alternativa: cerca qualsiasi link a form esterni noti
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(
                    domain in href
                    for domain in [
                        "forms.gle",
                        "google.com/forms",
                        "typeform.com",
                        "surveymonkey.com",
                        "jotform.com",
                    ]
            ):
                return href

        return ""
    except Exception as e:
        print(f"    Error fetching application link from {application_procedure_url}: {e}")
        return ""


def save_csv_to_file():
    """
    Salva il CSV nella cartella output/ per Make.com o altri flussi automatici
    """
    if not scraped_data:
        print("DEBUG: nessun dato da salvare")
        return

    # Crea la cartella output se non esiste
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    csv_path = os.path.join(OUTPUT_DIR, "salto_events_complete.csv")

    fieldnames = [
        "title",
        "type",
        "dates",
        "location",
        "application_deadline",
        "participants_no",
        "participants_from",
        "recommended_for",
        "accessibility",
        "working_language",
        "organiser",
        "participation_fee",
        "accommodation_food",
        "travel_reimbursement",
        "infopack_downloads",
        "application_procedure_url",
        "application_form_link",
        "detail_url",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scraped_data)

    print(f"DEBUG: CSV salvato in {csv_path}")
    socketio.emit("log", {"message": f"CSV salvato in {csv_path}"})


def scrape_events():
    """
    Funzione principale di scraping: raccoglie eventi dalle pagine lista,
    poi visita ogni dettaglio per estrarre tutti i campi.
    """
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/115.0.0.0 Safari/537.36"
        }
    )

    print("DEBUG: inizio scraping pagine lista...")
    # Ciclo sulle pagine di lista (6 pagine)
    for page in range(1, 7):
        msg = f"Caricamento pagina {page}/6..."
        socketio.emit("log", {"message": msg})
        print(f"DEBUG: {msg}")

        try:
            # Costruisci URL con parametro page
            url = f"{SEARCH_URL}?page={page}"
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            print(f"DEBUG: URL chiamato: {resp.url}")
        except Exception as e:
            err = f"Errore caricamento pagina {page}: {e}"
            socketio.emit("log", {"message": err})
            print(f"DEBUG: {err}")
            continue

        events = parse_list_page(resp.text)
        print(f"DEBUG: pagina {page}, eventi trovati: {len(events)}")
        scraped_data.extend(events)
        time.sleep(1)

    print(f"DEBUG: totale eventi raccolti dalla lista: {len(scraped_data)}")
    socketio.emit("log", {"message": f"Totale eventi trovati: {len(scraped_data)}"})

    # Ora visita ogni dettaglio per estrarre tutti i campi
    for i, event in enumerate(scraped_data, start=1):
        detail_url = event.get("detail_url", "")
        if not detail_url:
            continue

        msg = f"[{i}/{len(scraped_data)}] {event['title']}"
        socketio.emit("log", {"message": msg})
        print(f"DEBUG: {msg}")

        try:
            resp = session.get(detail_url, timeout=15)
            resp.raise_for_status()
            detail = parse_detail_page(resp.text, detail_url)

            # Get external application form link
            if detail["application_procedure_url"]:
                print(f"    → Getting application form link...")
                external_form_link = get_external_application_link(
                    detail["application_procedure_url"]
                )
                detail["application_form_link"] = external_form_link
            else:
                detail["application_form_link"] = ""

            # Merge detail info into event
            event.update(detail)

        except Exception as e:
            print(f"DEBUG: errore dettaglio {detail_url}: {e}")
            # Set default empty values for all detail fields
            event["participants_no"] = ""
            event["participants_from"] = ""
            event["recommended_for"] = ""
            event["accessibility"] = ""
            event["working_language"] = ""
            event["organiser"] = ""
            event["participation_fee"] = ""
            event["accommodation_food"] = ""
            event["travel_reimbursement"] = ""
            event["infopack_downloads"] = ""
            event["application_procedure_url"] = ""
            event["application_form_link"] = ""

        time.sleep(1)

    # Salva automaticamente il CSV
    save_csv_to_file()

    socketio.emit("log", {"message": "Scraping completato!"})
    socketio.emit("scraping_done", {"count": len(scraped_data)})
    print("DEBUG: scraping completato!")


# ========== ROUTES ==========

@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("start_scraping")
def handle_start_scraping():
    emit("log", {"message": "Avvio scraping..."})
    scrape_events()


@app.route("/download_csv")
def download_csv():
    if not scraped_data:
        return "Nessun dato disponibile", 400

    # Crea CSV in memoria (testo)
    text_buffer = StringIO()
    fieldnames = [
        "title",
        "type",
        "dates",
        "location",
        "application_deadline",
        "participants_no",
        "participants_from",
        "recommended_for",
        "accessibility",
        "working_language",
        "organiser",
        "participation_fee",
        "accommodation_food",
        "travel_reimbursement",
        "infopack_downloads",
        "application_procedure_url",
        "application_form_link",
        "detail_url",
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in scraped_data:
        writer.writerow(row)

    # Converte in bytes per send_file
    text_value = text_buffer.getvalue()
    bytes_buffer = BytesIO(text_value.encode("utf-8"))
    bytes_buffer.seek(0)

    return send_file(
        bytes_buffer,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="salto_events_complete.csv",
    )


# ========== ENDPOINT REST PER MAKE.COM ==========

@app.route("/api/scrape", methods=["POST", "GET"])
def api_scrape():
    """
    Endpoint REST per avviare lo scraping senza interfaccia web.
    Utile per Make.com, cron job, ecc.

    Esempio di chiamata:
    POST https://TUO-PROGETTO.onrender.com/api/scrape

    Risposta JSON:
    {
        "status": "ok",
        "count": 60,
        "csv_path": "output/salto_events_complete.csv"
    }
    """
    print("DEBUG: /api/scrape chiamato")
    scrape_events()

    return jsonify({
        "status": "ok",
        "count": len(scraped_data),
        "csv_path": f"{OUTPUT_DIR}/salto_events_complete.csv",
        "message": "Scraping completato. CSV salvato."
    })


if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)