# Trendyol Stage uçtan uca test

Durum: **Gerçek Stage kabul testi henüz tamamlanmadı** — Stage erişimi (Cloudflare/IP / IP yetkilendirmesi) bekleniyor.
Local/mock testler (pytest) Stage testinin yerine geçmez. Güncel kabul kontrol listesi: `docs/endpoint-raporu.md` sonu.

## Ön koşullar (Trendyol'dan alınacak)

- [ ] Stage **test satıcısı** sellerId → `TY_SUPPLIER_ID`
- [ ] Stage API key / secret → `TY_API_KEY`, `TY_API_SECRET` (yalnızca `.env`; commit edilmez)
- [ ] `TY_BASE_URL=https://stageapigw.trendyol.com`
- [ ] `TY_USER_AGENT` boş bırakılabilir → `"<sellerId> - SelfIntegration"`
- [ ] Stage erişimi için IP yetkilendirmesi gerekip gerekmediği Trendyol'a sorulmalı
- [ ] Stage'de test ürünü (`contentId`) ve **test sorusu oluşturma** servisi: resmi dokümanda vardır
      (`POST …/sellers/{sellerId}/questions`, Stage-only; bkz. `docs/endpoint-raporu.md` Bölüm 4).
      Oluşan soru Trendyol'da değerlendirmeye girer; listede görünmesi gecikebilir.

## Test sırası

```bash
source .venv/bin/activate
flask --app app qna config-check        # hepsi "set", base_url host: ... (STAGE)
# 1-2) Stage'de test sorusu oluştur, questionId'yi not et  → QID
flask --app app qna stage-e2e --question-id QID --answer "Merhaba, ürün stoktadır." --dry-run
#   poll → tek satır + NEW → HTML'de görünüyor → kısa cevap 400 (Trendyol'a istek yok)
flask --app app qna stage-e2e --question-id QID --answer "Merhaba, ürün stoktadır." --send
#   (dry-run'dan sonra soru hâlâ NEW olmalı) geçerli cevap → ANSWERING sırasında gönderim →
#   ANSWERED → ikinci cevap 409 ALREADY_LOCKED (Trendyol'a 2. istek yok) → 2. poll → tek satır
flask --app app qna question QID        # row_count=1, duplicate=false
flask --app app qna runs --limit 5      # PollRun metrikleri
```

Tarayıcı kontrolü (manuel): `python run.py` → `http://127.0.0.1:5000/` → soru kartı görünüyor,
10 karakter altı cevapta buton kapalı, geçerli cevapta "Cevap gönderiliyor…" → "ANSWERED".
(Tarayıcıdan cevap verilecekse `--send` adımı yerine bu yapılır; ikisi birden değil.)

## Kaydedilecek sonuçlar

| Alan | Değer |
|---|---|
| questionId | |
| poll_1: fetched / inserted / updated / status | |
| cevap sonrası local_status | |
| Trendyol sonucu (answer_id veya normalize hata kodu) | |
| ikinci cevap: HTTP / error_code | |
| poll_2: fetched / inserted / updated / status | |
| poll_2 sonrası local_status (ANSWERED veya REOPENED) | |
| duplicate row var mı (row_count) | |

`stage-e2e` çıktısı bu alanların hepsini JSON olarak verir; secret değer yazmaz.
