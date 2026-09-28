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

## Faz 4 — Soru listesi ve cevap API'si

| Route | Açıklama |
|---|---|
| `GET /` | `NEW` ve `REOPENED` soruların basit HTML listesi |
| `GET /api/questions?limit=50` | Aynı liste, JSON (`limit` 1–200) |
| `POST /api/answer` | `{"question_id": <local id>, "text": "..."}` |

Tüm `/api/*` yanıtları: `{"ok": true, "data": ...}` veya
`{"ok": false, "error": {"code": "...", "message": "..."}}`. Trendyol'un ham hata gövdesi
asla dönmez.

```bash
curl -s -X POST http://127.0.0.1:5000/api/answer \
  -H 'Content-Type: application/json' \
  -d '{"question_id": 1, "text": "Merhaba, ürün stokta mevcuttur."}'
```

## Faz 5 — Arayüz

`http://127.0.0.1:5000/` NEW ve REOPENED soruları kart olarak gösterir; her kartta
10–2000 karakter kontrollü cevap alanı, canlı sayaç ve "Cevapla" butonu vardır.
Frontend sadece HTML + CSS + vanilla JS'tir (`app/static/`), build adımı yoktur.

Frontend birim testleri Node'un yerleşik test aracıyla çalışır (npm paketi gerekmez);
`pytest` Node kuruluysa bunları da otomatik çalıştırır:

```bash
node --test tests/js/app.test.js
```

## Faz 6 — Dayanıklılık

- **Retryable hatalar** (timeout, ağ, 5xx, 429, beklenmeyen sunucu hatası) soruyu
  `FAILED + retryable=true` yapar; soru listede kalır ve hemen tekrar cevaplanabilir.
  Kalıcı hatalar (yasaklı kelime, yetki vb.) `retryable=false` olur ve listede görünmez.
- **`retry_count`** başlatılan tekrar deneme sayısıdır: önceki denemesi hatayla biten bir
  soru yeniden kilitlendiğinde 1 artar; başarılı cevapta korunur.
- **Takılı `ANSWERING` kurtarma:** scheduler her 60 sn'de bir, 10 dakikadan uzun süredir
  `ANSWERING` olan soruları `FAILED + retryable=true` (`STALE_ANSWERING`) yapar.

Migration: `alembic upgrade head` (`questions.locked_at` kolonu eklenir).
