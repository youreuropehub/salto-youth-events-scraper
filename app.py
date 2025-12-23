def scrape_events():
    """Scraping ciclando tutte le pagine della lista eventi SALTO e visitando ogni dettaglio"""
    global scraped_data
    scraped_data = []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    url = "https://www.salto-youth.net/tools/european-training-calendar/browse/"
    events_dict = {}
    page_num = 1

    while url:
        socketio.emit("log", {"message": f"[DEBUG] Caricamento pagina {page_num}: {url}"})
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            socketio.emit("log", {"message": f"[ERROR] Pagina {page_num}: {e}"})
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select(".search-results-list .tool-item")

        if not rows:
            socketio.emit("log", {"message": f"[DEBUG] Nessun evento trovato a pagina {page_num}"})
            break

        for row in rows:
            title_tag = row.select_one("h2 a")
            title = title_tag.get_text(strip=True) if title_tag else ""
            detail_url = title_tag.get("href") if title_tag else ""
            if detail_url and not detail_url.startswith("http"):
                detail_url = BASE_URL + detail_url

            if detail_url in events_dict:
                continue

            type_ = row.select_one("span.h3.tool-item-category")
            type_ = type_.get_text(strip=True) if type_ else ""

            dates = row.select_one("p.h5")
            dates = dates.get_text(strip=True) if dates else ""

            location = row.select_one(".microcopy.mrgn-btm-17")
            location = location.get_text(strip=True) if location else ""

            # Application deadline
            app_deadline = ""
            callout = row.select_one("div.callout-module")
            if callout:
                p_tags = callout.find_all("p")
                for i, p in enumerate(p_tags):
                    if "Application deadline" in p.get_text(strip=True):
                        if i + 1 < len(p_tags):
                            app_deadline = p_tags[i + 1].get_text(strip=True)
                        break

            # Inizializza evento con dati dalla lista
            event_data = {
                "title": title,
                "type": type_,
                "dates": dates,
                "location": location,
                "application_deadline": app_deadline,
                "detail_url": detail_url,
                # campi extra da dettaglio
                "participants_no": "",
                "participants_from": "",
                "recommended_for": "",
                "accessibility": "",
                "working_language": "",
                "organiser": "",
                "participation_fee": "",
                "accommodation_food": "",
                "travel_reimbursement": "",
                "infopack_downloads": "",
                "application_procedure_url": "",
                "application_form_link": "",
                "training_description": "",
            }

            # Visita pagina dettaglio
            try:
                detail_resp = session.get(detail_url, timeout=15)
                detail_resp.raise_for_status()
                detail_soup = BeautifulSoup(detail_resp.text, "html.parser")

                # ---------- Training description ----------
                desc_div = detail_soup.find("div", class_="training-description")
                if desc_div:
                    event_data["training_description"] = desc_div.get_text("\n", strip=True)

                # ---------- Training overview ----------
                h3_overview = detail_soup.find(lambda tag: tag.name in ["h3","h4"] and "Training overview" in tag.get_text())
                if h3_overview:
                    lines = [l.strip() for l in h3_overview.find_next_siblings(text=True)]
                    for i, line in enumerate(lines):
                        line_low = line.lower()
                        if line_low == "for" and i+1 < len(lines) and "participants" in lines[i+1].lower():
                            event_data["participants_no"] = lines[i+1].replace("participants","").strip()
                            # countries
                            countries = []
                            j = i + 2
                            while j < len(lines) and not lines[j].lower().startswith("and recommended"):
                                countries.append(lines[j])
                                j += 1
                            event_data["participants_from"] = " ".join(countries).strip()
                        if "and recommended for" in line_low and i+1 < len(lines):
                            event_data["recommended_for"] = lines[i+1].strip()
                        if "working language(s):" in line_low:
                            after = line.split("Working language(s):",1)[-1].strip()
                            event_data["working_language"] = after if after else lines[i+1].strip() if i+1 < len(lines) else ""
                        if line_low.startswith("organiser"):
                            after = line.split("Organiser",1)[-1].replace(":","").strip()
                            event_data["organiser"] = after if after else lines[i+1].strip() if i+1 < len(lines) else ""

                # ---------- Other sections ----------
                def section_after_heading(text):
                    h = detail_soup.find(lambda tag: tag.name in ["h3","h4"] and text in tag.get_text())
                    if not h: return ""
                    parts=[]
                    for sib in h.find_next_siblings():
                        if sib.name and sib.name.startswith("h"): break
                        parts.append(sib.get_text(" ",strip=True))
                    return " ".join(parts).strip()

                event_data["accessibility"] = section_after_heading("Accessibility info")
                event_data["participation_fee"] = section_after_heading("Participation fee")
                event_data["accommodation_food"] = section_after_heading("Accommodation and food")
                event_data["travel_reimbursement"] = section_after_heading("Travel reimbursement")

                # Available downloads
                event_data["infopack_downloads"] = ""
                downloads_heading = detail_soup.find(lambda tag: tag.name in ['h3','h4','h5','strong','b','p'] and "Available downloads" in tag.get_text())
                if downloads_heading:
                    for sib in downloads_heading.find_next_siblings():
                        if sib.name and sib.name.startswith("h"): break
                        link = sib.find("a", href=True)
                        if link:
                            event_data["infopack_downloads"] = BASE_URL + link["href"] if not link["href"].startswith("http") else link["href"]
                            break

                # Application procedure
                app_proc_link = detail_soup.find("a", href=lambda h: h and "/application-procedure/" in h)
                if app_proc_link:
                    href = app_proc_link["href"]
                    if not href.startswith("http"): href = BASE_URL + href
                    event_data["application_procedure_url"] = href
                    event_data["application_form_link"] = get_external_application_link(href)

            except Exception as e:
                socketio.emit("log", {"message": f"[ERROR] Dettaglio evento {title}: {e}"})

            events_dict[detail_url] = event_data
            socketio.emit("log", {"message": f"[DEBUG] Evento dettagliato aggiunto: {title}"})

        # Pagina successiva
        next_page = soup.select_one(".search-result-list-navigation a.next-page")
        url = BASE_URL + next_page.get("href") if next_page else None
        page_num += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())
    socketio.emit("log", {"message": f"[DEBUG] Totale eventi raccolti: {len(scraped_data)}"})
    save_csv_to_file()
    socketio.emit("scraping_done", {"count": len(scraped_data)})
