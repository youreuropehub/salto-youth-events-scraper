def scrape_events():
    """Scraping ciclando tutte le pagine della lista eventi SALTO"""
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

            events_dict[detail_url] = {
                "title": title,
                "type": type_,
                "dates": dates,
                "location": location,
                "application_deadline": app_deadline,
                "detail_url": detail_url,
            }
            socketio.emit("log", {"message": f"[DEBUG] Evento trovato: {title}, deadline: {app_deadline}"})

        # Trova il link alla pagina successiva
        next_page = soup.select_one(".search-result-list-navigation a.next-page")
        url = BASE_URL + next_page.get("href") if next_page else None
        page_num += 1
        time.sleep(1)

    scraped_data = list(events_dict.values())
    socketio.emit("log", {"message": f"[DEBUG] Totale eventi raccolti dalla lista: {len(scraped_data)}"})

    # Se vuoi, qui puoi ciclare ogni detail_url per estrarre description ecc.
