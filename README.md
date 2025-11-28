# SALTO Youth Events Scraper

Applicazione Flask per estrarre eventi dal calendario europeo SALTO-YOUTH, visualizzarli via web e scaricare i risultati in CSV.

## Avvio locale

```bash
pip install -r requirements.txt
python app.py
```

Poi apri: [http://localhost:5000](http://localhost:5000)

## Deploy su Render

1. Carica questa cartella su GitHub
2. Crea un nuovo Web Service su Render collegato al repo
3. Build command:
```bash
pip install -r requirements.txt
```
4. Start command:
```bash
gunicorn app:app
```
