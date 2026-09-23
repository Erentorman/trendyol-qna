# trendyol-qna

## Faz 0 — Ortam kurulumu

Gereksinimler: Python 3.10+, Docker (Compose v2).

> macOS'ta sistem `python3` 3.9 olabilir. `python3 --version` 3.10'dan küçükse
> `python3.11` (veya kurulu 3.10+ sürüm) kullanın.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d
cp .env.example .env
python run.py
curl http://127.0.0.1:5000/health
```

Beklenen yanıt:

```json
{"database": "ok", "status": "ok"}
```

DB erişilemezse `/health` 503 ve `{"status": "error", "database": "unavailable"}` döner.

> macOS'ta 5000 portu AirPlay Receiver tarafından kullanılıyor olabilir.
> Bu durumda AirPlay Receiver'ı kapatın veya `PORT=5001 python run.py` ile başlatın.
