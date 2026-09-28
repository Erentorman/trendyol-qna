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

## Faz 1 — Veritabanı şeması ve CRUD

```bash
pip install -r requirements-dev.txt        # pytest dahil
alembic upgrade head                        # questions + poll_runs tablolarını oluşturur

# Testler ayrı bir *_test veritabanı kullanır (tablolar her testte temizlenir)
docker compose exec db createdb -U trendyol trendyol_qna_test
pytest -q
```

Model değişikliğinden sonra yeni migration:

```bash
alembic revision --autogenerate -m "açıklama"   # üretilen dosyayı gözden geçirin
alembic upgrade head
```

## Faz 3 — Periyodik polling

`python run.py`, `.env` içindeki `TY_*` değerleri doluysa bir `BackgroundScheduler` başlatır:
her `POLL_INTERVAL_SECONDS` saniyede (boşsa 300) Trendyol'daki tüm `WAITING_FOR_ANSWER`
sayfaları çekilir, `questions` tablosuna upsert edilir ve her çalışma `poll_runs`
tablosuna metrikleriyle yazılır. `TY_*` eksikse uygulama çalışır ama poller devre dışı kalır
(logda `Poller disabled` uyarısı).

Son poll'ları görmek için:

```bash
docker compose exec db psql -U trendyol -d trendyol_qna \
  -c "SELECT id, status, fetched_count, inserted_count, updated_count, duration_ms, error_message
      FROM poll_runs ORDER BY id DESC LIMIT 10"
```

> **Production notu:** Scheduler, uygulama sürecinin içinde çalışır ve tek süreç varsayar.
> Çoklu Gunicorn worker ortamında her worker kendi scheduler'ını başlatır ve aynı poll
> birden fazla kez çalışır. Production'da poller ayrı bir process/service olarak
> çalıştırılmalı **veya** poll başında PostgreSQL advisory lock
> (`pg_try_advisory_lock`) alınarak tek poller garanti edilmelidir.
