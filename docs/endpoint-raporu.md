# Trendyol QnA API Endpoint Raporu

Rapor tarihi: 2026-10-12 · Kaynak: yalnızca resmi `developers.trendyol.com` dokümanı + repodaki kod ve otomatik testler.

Etiketler:
**[DOK]** resmi dokümanda var · **[TEST]** repodaki otomatik testle doğrulandı (sahte HTTP; Trendyol'a gitmez) · **[DOĞRULANAMADI]** resmi dokümanda yok / dokümanlar çelişiyor · **[STAGE-2xx]** gerçek Stage'den canlı 2xx alındı.

> **Bu raporda hiçbir işlem için [STAGE-2xx] etiketi yoktur.** Bu uygulamadan gerçek Stage'e yapılan hiçbir istek henüz başarıyla tamamlanmadı (erişim engeli: bkz. Bölüm 6). Kod ve otomatik test seviyesi ile canlı doğrulama bilinçli olarak ayrı tutulmuştur.

---

## 0. Amaç

Bu entegrasyonun amacı:

- müşteri sorularını çekmek,
- mümkün olan tüm soru verisini kaybetmeden saklamak,
- soruları cevaplamak,
- Stage ortamında uçtan uca test yapabilmek.

## Ortak bilgiler (tüm endpointler)

| | |
|---|---|
| PROD base | `https://apigw.trendyol.com` |
| STAGE base | `https://stageapigw.trendyol.com` |
| Base URL ayarı | `TY_BASE_URL` (`.env`) |
| Authentication | HTTP Basic, kullanıcı = API Key, parola = API Secret (`requests.Session.auth`). Değerler `.env`'de kalır; log, hata ve ekrana yazılmaz **[TEST]** |
| User-Agent | QnA ve Product sayfalarında **belirtilmiyor** **[DOĞRULANAMADI]**. Client her istekte `"<sellerId> - SelfIntegration"` (veya `TY_USER_AGENT`) gönderir. Sunucunun zorunlu kıldığı doküman ile kanıtlı değil |
| Stage erişimi | Resmi doküman: Stage için IP yetkilendirmesi gerekir; yetkisiz IP'de 503 dönebilir. PROD'da IP yetkilendirmesi gerekmez **[DOK]** ([Canlı-Test Ortam Bilgileri](https://developers.trendyol.com/docs/3-canl%C4%B1-test-ortam-bilgileri)) |
| Timeout | 5 sn bağlantı / 20 sn okuma |
| Retry (ortak `_request`) | 429, 5xx, timeout/bağlantı hatası: en çok 3 deneme (bekleme 1 sn, 2 sn; `Retry-After` en çok 60 sn). Diğer 4xx tekrar denenmez. Cloudflare engeli (`ACCESS_BLOCKED`) tekrar denenmez. Stage soru oluşturma tek denemedir |
| Rate limit | Changelog (17.03.2026): soru çekme 1000 req/dk, soru cevaplama 500 req/dk (27.03.2026'dan itibaren). Tek soru detayı ve soru oluşturma için limit belirtilmemiş **[DOK]** |
| Hata gövdesi şeması | Dokümante değil; sadece mesaj metinleri var. Kod, bilinen Türkçe/İngilizce mesaj kalıplarını alan koda çevirir (`DOMAIN_PATTERNS`) |

---

## 1. Müşteri Sorularını Listeleme

| | |
|---|---|
| 1. Endpoint adı | getQuestionFilter — Müşteri Sorularını Listeleme |
| 2. HTTP Method | GET |
| 3. PROD URL | `https://apigw.trendyol.com/integration/qna/sellers/{sellerId}/questions/filter` **[DOK]** |
| 4. STAGE URL | `https://stageapigw.trendyol.com/integration/qna/sellers/{sellerId}/questions/filter` **[DOK]** |
| 5. Amaç | Satıcıya gelen müşteri sorularını sayfalı çekmek |
| 6. Projedeki rolü | Ana veri kaynağı: otomatik senkron (scheduler), `flask qna poll`, Stage ekranı "Soruları Yenile", `stage-e2e` |
| 7. Path parametreleri | `sellerId` (int64, zorunlu) |
| 8. Query parametreleri | Aşağıdaki tablo |
| 9. Request body | Yok |
| 10. Response yapısı | `{content: [Question], page, size, totalElements, totalPages}`; Question alanları aşağıda |
| 11. Kullandığımız önemli alanlar | Aşağıdaki tablo |
| 12. Authentication | HTTP Basic |
| 13. User-Agent | Dokümanda belirtilmiyor; client standart User-Agent gönderir |
| 14. Başarılı status | 200 (dokümanda tek tanımlı kod) |
| 15. Önemli hatalar | Bölüm 7 |
| 16. Retry | Ortak kural (429/5xx/ağ: 3 deneme) |
| 17. Kod | `app/trendyol_client.py:TrendyolClient.fetch_waiting_questions`, `_map_question`; `app/scheduler.py:poll_job/_run_poll`; `app/crud.py:upsert_questions` |
| 18. Durum | **AKTİF KULLANILIYOR** (kod/test seviyesi doğrulandı; gerçek Stage'de denenmedi) |

**Query parametreleri**

| Parametre | Doküman | Kodun davranışı |
|---|---|---|
| `supplierId` | TR rehber "zorunlu" der, reference listelemez | Her istekte gönderilir |
| `status` | `WAITING_FOR_ANSWER`, `ANSWERED`, `REPORTED`, `REJECTED`, `UNANSWERED` (EN rehber ayrıca `WAITING_FOR_APPROVE`; reference'ta `UNANSWERED` yok) | Sabit `WAITING_FOR_ANSWER` (metot yalnızca bekleyen soruları çeker) |
| `page` | 0 tabanlı | 0'dan başlar, `int >= 0` doğrulanır |
| `size` | TR rehber: varsayılan 20, **max 50**; reference: varsayılan 10, **max 200** (çelişki) | Varsayılan 50, `1–50` doğrulanır (güvenli olan düşük sınır) |
| `barcode` | Tek barkod için filtre | İsteğe bağlı argüman `barcode`; boş değilse gönderilir |
| `startDate`, `endDate` | Epoch ms. **Tarih verilmezse son 1 hafta**; aralık en çok 2 hafta (uzunsa `endDate = startDate + 2 hafta`) | İsteğe bağlı `start_date`/`end_date` (int ≥ 0, başlangıç ≤ bitiş). **Scheduler tarih vermez, yani Trendyol'un "son 1 hafta" varsayılanı geçerlidir** |
| `orderByField` | `CreatedDate`, `LastModifiedDate` | Varsayılan `CreatedDate`; değerler doğrulanır |
| `orderByDirection` | `ASC`, `DESC` | Varsayılan `ASC` (sayfa numarası sabit kalsın); değerler doğrulanır |

**Question (content[]) alanları — resmi dokümandaki tam liste**

| Alan | Tip | Modelde ayrı kolon | Notu |
|---|---|---|---|
| `id` | int64 | `trendyol_question_id` (unique) | Soru kimliği |
| `text` | string | `question_text` | |
| `status` | string | `trendyol_status` | Yerel iş akışı `local_status` ayrıdır |
| `creationDate` | int64 (epoch ms) | `question_created_at` | UTC saklanır, ekranda İstanbul saati |
| `userName` | string | `customer_name` | Maskeli; `showUserName=false` ise boş |
| `productName` | string | `product_name` | |
| `productMainId` | string | `product_main_id` | Reference şemasında var, bazı örneklerde yok |
| `customerId` | int64 | — (`raw_payload`) | |
| `showUserName` | boolean | — (`raw_payload`) | |
| `public` | boolean | — (`raw_payload`) | |
| `imageUrl`, `webUrl` | string | — (`raw_payload`) | Arayüz kartında `raw_payload`'dan okunur (yalnız `https://`) |
| `answeredDateMessage` | string | — (`raw_payload`) | |
| `answer` | nesne | — (`raw_payload`) | Alt alanlar dokümanlar arasında **çelişiyor**: reference `text, creationDate, status`; EN rehber `id, text, creationDate, hasPrivateInfo` |
| `rejectedAnswer` | nesne | — (`raw_payload`) | Aynı çelişki; rehberde ayrıca `reason` |
| `rejectedDate`, `reason` | int64 / string | — (`raw_payload`) | |
| `reportReason`, `reportedDate` | string / int64 | — (`raw_payload`) | |
| `barcode` | string | `barcode` | Dokümante response'ta **yok**; gelirse alınır |

**Sayfalama, tekrar ve güncelleme davranışı (kod + test)**

- `page=0`'dan başlar; her sayfada `totalPages` yeniden okunur; son sayfada veya boş `content`'te durur; en çok 200 sayfa (10.000 soru) güvenlik sınırı vardır, aşılırsa poll `FAILED` olur **[TEST]**.
- Her sayfa ayrı commit edilir. Ara sayfada kalıcı hata olursa (3 deneme sonrası) poll **`FAILED`** kaydedilir, hata `error_message`'a ve loga yazılır, önceki sayfalar korunur; poll sessizce "başarılı" görünmez **[TEST]** (`tests/test_pagination.py`).
- Aynı `id` tekrar gelirse yeni satır oluşmaz (`ON CONFLICT DO UPDATE`); aynı istekte iki sayfada tekrar etse bile tek satır, son gelen değer kazanır **[TEST]**.
- Mevcut soru değişirse (`raw_payload` veya `status` farklıysa) kaynak alanlar güncellenir; değişmediyse atlanır. Yerel iş akışı alanlarına (`answer_*`, `error_*`, `retry_count`, `local_status` kuralları) dokunulmaz **[TEST]**.
- Trendyol statüsü → yerel durum: `WAITING_FOR_ANSWER` → `NEW`; `ANSWERED/REJECTED/REPORTED/UNANSWERED` → `CLOSED`; yerelde `ANSWERED/FAILED/CLOSED` iken Trendyol yine `WAITING_FOR_ANSWER` derse `REOPENED`.
- `totalPages=0` ve `content=[]` güvenle işlenir **[TEST]**. Yanıtta `totalPages` hiç yoksa kod 0 sayar ve ilk sayfadan sonra durur (dokümana göre alan her yanıtta bulunur; gerçek yanıtla doğrulanmadı).

---

## 2. Tek Müşteri Sorusu Detayı

| | |
|---|---|
| 1. Endpoint adı | getQuestion — Soru Detayını Getirme |
| 2. HTTP Method | GET |
| 3. PROD URL | `https://apigw.trendyol.com/integration/qna/sellers/{sellerId}/questions/{id}` **[DOK]** |
| 4. STAGE URL | `https://stageapigw.trendyol.com/integration/qna/sellers/{sellerId}/questions/{id}` **[DOK]** |
| 5. Amaç | Tek bir sorunun güncel tüm alanlarını çekmek (ör. cevap sonrası durum teyidi) |
| 6. Projedeki rolü | Gerektiğinde çağrılacak client metodu. **Poll, scheduler, route veya CLI çağırmaz** |
| 7. Path parametreleri | `sellerId` (int64), `id` (int64, Trendyol soru id) |
| 8. Query parametreleri | Yok |
| 9. Request body | Yok |
| 10. Response yapısı | Liste elemanıyla **aynı** `Question` şeması (Bölüm 1 tablosu); sarmalayıcı yok |
| 11. Kullandığımız alanlar | `_map_question` ile liste öğesi gibi eşlenir; tam nesne `raw_payload` olarak döner |
| 12. Authentication | HTTP Basic |
| 13. User-Agent | Dokümanda belirtilmiyor |
| 14. Başarılı status | 200 ("Soru detayı başarıyla getirildi") |
| 15. Önemli hatalar | 401/403 → `AUTH_ERROR`; 404 → `UNKNOWN_ERROR` (`status_code=404`, özel "bulunamadı" kodu yok, tekrar denenmez); 429 → `RATE_LIMITED`; 5xx/timeout → tekrar denenip `SERVER_ERROR`/`NETWORK_ERROR`; 200 ama JSON nesnesi/`id` yoksa → `UNKNOWN_ERROR`. Dokümanda hata yanıtları tanımlı değil |
| 16. Retry | Ortak kural |
| 17. Kod | `app/trendyol_client.py:TrendyolClient.get_question`, `merge_question_payload` |
| 18. Durum | **DESTEKLEYİCİ / İHTİYAÇ HALİNDE** (kod/test seviyesi doğrulandı; gerçek Stage'de denenmedi) |

**Neden polling'e bağlanmadı:** Detay, listeden **ek alan döndürmüyor** (iki şema aynı). Poll'a bağlamak 100 soru = 100 ek istek demek, bilgi kazancı yok ve rate limite yaklaştırır. Bu yüzden "alınabilen tüm bilgiler" şartı liste + `raw_payload` ile karşılanır.

**Ne zaman kullanılır:** tek bir sorunun cevap sonrası durumunu (`ANSWERED/REJECTED`) kesinleştirmek, `answerId` düz string yanıtta gelmediğinde cevabın Trendyol'daki halini görmek, arızalı bir sorunun güncel halini sorgulamak. `merge_question_payload(liste, detay)` ikisini birleştirir: detay kazanır, detaydaki `null` listedeki değeri silmez, tek tarafta olan anahtarlar korunur **[TEST]**.

---

## 3. Müşteri Sorusunu Cevaplama

| | |
|---|---|
| 1. Endpoint adı | createAnswer — Soruyu Cevaplama |
| 2. HTTP Method | POST |
| 3. PROD URL | `https://apigw.trendyol.com/integration/qna/sellers/{sellerId}/questions/{id}/answers` **[DOK]** |
| 4. STAGE URL | `https://stageapigw.trendyol.com/integration/qna/sellers/{sellerId}/questions/{id}/answers` **[DOK]** |
| 5. Amaç | `WAITING_FOR_ANSWER` durumundaki soruya satıcı cevabı göndermek. Yalnız bu statü cevaplanabilir; `ANSWERED`, `REPORTED`, `REJECTED`, `UNANSWERED` cevaplanamaz |
| 6. Projedeki rolü | Ana yazma akışı: arayüz `POST /api/answer` → `submit_answer` → bu endpoint |
| 7. Path parametreleri | `sellerId` (int64), `id` = Trendyol soru id (`trendyol_question_id`; yerel DB id **değil**) |
| 8. Query parametreleri | Yok |
| 9. Request body | `{"text": "..."}` — `text` zorunlu, **min 10 – max 2000 karakter** **[DOK]** |
| 10. Response yapısı | Dokümanlar çelişiyor: reference başarı gövdesi düz string (`"Cevabınız başarıyla kaydedilmiştir."`), rehber `{"answerId": 0}` |
| 11. Kullandığımız alanlar | `answerId` (varsa) → `questions.answer_id`. Ham gövde ve HTTP status client sonucunda `response` / `http_status` olarak döner (ayrı kolonda saklanmaz) |
| 12. Authentication | HTTP Basic + `Content-Type: application/json` |
| 13. User-Agent | Dokümanda belirtilmiyor |
| 14. Başarılı status | Reference açıklaması 200 ("Cevap başarıyla kaydedildi. Değerlendirilmeye gönderildi."); rehber kod vermiyor. Kod her 2xx'i başarı sayar |
| 15. Önemli hatalar | Aşağıda ve Bölüm 7 |
| 16. Retry | 429/5xx/ağ: 3 deneme. Bu POST için güvenli: Trendyol aynı soruya ikinci cevabı reddeder, çift cevap oluşmaz. Yan etki: istek ulaşıp yanıt kaybolursa ikinci deneme "zaten cevaplanmış" döner, yerel soru `CLOSED` olur ve cevap metni yerelde saklanmaz |
| 17. Kod | `app/trendyol_client.py:TrendyolClient.send_answer`, `validate_answer_text`, `_answer_id`, `DOMAIN_PATTERNS`; `app/answer_service.py:submit_answer`; `app/crud.py:try_lock_for_answering/mark_answered/mark_failed/mark_closed`; `app/routes.py:answer` (`POST /api/answer`); `app/static/app.js` |
| 18. Durum | **AKTİF KULLANILIYOR** (kod/test seviyesi doğrulandı; gerçek Stage'de denenmedi) |

**Doğrulama:** metin `strip` edilir; boş → `EMPTY_ANSWER`, 10'dan kısa → `ANSWER_TOO_SHORT`, 2000'den uzun → `ANSWER_TOO_LONG`. Üçü de kilit alınmadan ve Trendyol'a istek atılmadan reddedilir (HTTP 400). Sınırlar 10 ve 2000 geçerli, 9 ve 2001 geçersizdir **[TEST]**. Not: TR rehberdeki hata metni "10 karakterden uzun olmalıdır" der, şema ise min 10 der; kod şemayı (10 dahil) izler.

**Dokümante hata mesajları → kod:** "Bu soru daha önce cevaplandı…" → `QUESTION_ALREADY_ANSWERED` (soru `CLOSED`); "Belirtilen süre içinde cevap vermediğiniz için soru kapatılmıştır." → `TIME_LIMIT_EXCEEDED` (`CLOSED`); "…yasaklı kelime kullanma limitini aştığınız için soru kapatılmıştır." → `FORBIDDEN_WORD` (`FAILED`, tekrar denenmez); "Cevabınız 10 karakterden uzun olmalıdır." / "…2000 karakterden uzun olamaz." / "Bir cevap giriniz." → kısa/uzun/boş **[TEST]**. Doküman yasaklı kelime için iki durum anlatır (tek ret: soru `WAITING_FOR_ANSWER`'a döner; limit aşımı: soru kapanır), ancak tek bir mesaj metni verir; kod bunu ayırt edemez.

**`answerId`:** dict gövdedeki `answerId` int'e çevrilir (sayısal string kabul, bool/geçersiz → `None`); `answer_service` ile `questions.answer_id`'ye yazılır ve route yanıtında döner. Düz string başarı gövdesinde `answerId` yoktur → `None`, cevap yine başarılı sayılır (bozuk/boş 2xx gövde de başarıdır; tekrar göndermek yalnız "zaten cevaplanmış" üretirdi) **[TEST]**.

**Yerel durum:** başarı → `ANSWERED` + `answer_text` + `answer_id` + `answered_at`. Hata → `FAILED` (retryable ise tekrar denenebilir) veya `CLOSED`; `answer_text/answer_id/answered_at` hatada **asla** doldurulmaz **[TEST]**. Çift cevap: `NEW→ANSWERING` tek atomik UPDATE; ikinci istek Trendyol'a gitmeden 409 `ALREADY_LOCKED`; arayüzde başarıdan sonra form pasif **[TEST]**.

---

## 4. Stage Test Sorusu Oluşturma

| | |
|---|---|
| 1. Endpoint adı | Creating Customer Questions in Stage Environment — Stage Test Sorusu Oluşturma |
| 2. HTTP Method | POST |
| 3. PROD URL | **Yok.** Servis yalnızca Stage için tanımlıdır; kod PROD'da bu isteği hiç atmaz |
| 4. STAGE URL | `https://stageapigw.trendyol.com/integration/qna/sellers/{sellerId}/questions` **[DOK]** ([sayfa](https://developers.trendyol.com/v2.0/docs/creating-customer-questions-in-stage-environment), changelog 26.06.2026) |
| 5. Amaç | Stage'de soru-cevap akışını denemek için kendi ürünümüze müşteri sorusu üretmek |
| 6. Projedeki rolü | **Yalnızca Stage test aracı.** Üretim (PROD) ana akışının parçası değildir; scheduler ve cevaplama akışı bunu çağırmaz |
| 7. Path parametreleri | `sellerId` (zorunlu) |
| 8. Query parametreleri | Yok |
| 9. Request body | `text` (zorunlu, boş olamaz), `contentId` (zorunlu, ürün content id), `userId` (zorunlu), `showUserName` (zorunlu, boolean), `userFullName` (opsiyonel), `channelId` (opsiyonel; sayfa 1 gönderilebilir der). Örnek: `{"text":"...","contentId":2397814,"userId":4147346,"userFullName":"Test User","showUserName":false,"channelId":1}` |
| 10. Response yapısı | `{"questionId": 42}` **[DOK]** (status kodu belirtilmemiş) |
| 11. Kullandığımız alanlar | `questionId` (yoksa `id`, ya da düz int), `X-Correlation-Id` |
| 12. Authentication | Sayfada belirtilmemiş; client diğer endpointler gibi HTTP Basic gönderir |
| 13. User-Agent | Belirtilmiyor |
| 14. Başarılı status | Dokümanda yok. Kullanıcı bildirimi: Postman'de Stage'e karşı **200 OK + `questionId`** alındı. Bu, bu uygulamadan alınmış bir sonuç **değildir** |
| 15. Önemli hatalar | Dokümante mesajlar: boş metin, eksik `contentId` / `userId` / `showUserName` / `sellerId`. Ayrıca 401/403/429/5xx genel eşleme (Bölüm 7). Oluşan soru yayınlanmadan önce Trendyol'da değerlendirilir, listede görünmesi gecikebilir |
| 16. Retry | **Tekrar denenmez** (`max_attempts=1`): tekrar, mükerrer test sorusu bırakabilir |
| 17. Kod | `app/trendyol_client.py:TrendyolClient.create_stage_question`; `app/stage.py:create_question` (`POST /api/stage/questions`); arayüz `/stage` → "Test Sorusu Oluştur" |
| 18. Durum | **SADECE STAGE TEST** |

Güvenlik: yalnız `TY_BASE_URL` host'u `stageapigw.trendyol.com` ise çalışır; aksi halde HTTP isteği atılmaz **[TEST]**. Kod, dokümandaki zorunlu alanları her zaman gönderir (opsiyonel `userFullName` ve `channelId` dahil).

Geçmiş not (düzeltme): bu servisin resmi dokümanda olmadığı yönündeki önceki not **yanlıştı**; endpoint resmi olarak vardır. Uygulamadan alınan 403 yanıtı yetki (RBAC) sorunu değil, Cloudflare/IP erişim engeli olarak değerlendirilmektedir (Bölüm 6); bu bir kod hatası değildir.

---

## 5. Ürün Sorgulama (barcode → contentId) — Stage test yardımcısı

Bu endpoint QnA değil Product API'dir; yalnızca test sorusu için gereken `contentId`'yi barkoddan bulmak üzere Stage ekranında kullanılır. Üretim akışı çağırmaz.

| | |
|---|---|
| 1. Endpoint adı | Product Filter – Base Information v2 |
| 2. HTTP Method | GET |
| 3. PROD URL | `https://apigw.trendyol.com/integration/product/sellers/{sellerId}/product/{barcode}` **[DOK]** ([sayfa](https://developers.trendyol.com/v3.0/docs/product-filter-base-information-v2)) |
| 4. STAGE URL | `https://stageapigw.trendyol.com/integration/product/sellers/{sellerId}/product/{barcode}` **[DOK]** |
| 5. Amaç | Barkoddan ürünün `contentId` ve onay bilgisini almak |
| 6. Projedeki rolü | Stage test aracı (`POST /api/stage/product`) |
| 7. Path parametreleri | `sellerId`, `barcode` (URL-encode edilir) |
| 8. Query parametreleri | Yok |
| 9. Request body | Yok. Header: `storeFrontCode` **zorunlu** (değeri/formatı sayfada tanımlı değil; kod yalnız `TY_STOREFRONT_CODE` doluysa gönderir) |
| 10. Response yapısı | Örnek: `barcode`, `approved`, `approvedDate`, `archived`, `listingId`, `contentId` |
| 11. Kullandığımız alanlar | `contentId` (liste biçiminde `productContentId`), `barcode`; `title/productName/name` yalnız gelirse |
| 12. Authentication | HTTP Basic |
| 13. User-Agent | Belirtilmiyor |
| 14. Başarılı status | 200 |
| 15. Önemli hatalar | Dokümante değil; genel eşleme (Bölüm 7) |
| 16. Retry | Ortak kural |
| 17. Kod | `app/trendyol_client.py:TrendyolClient.get_product_by_barcode`; `app/stage.py:product` |
| 18. Durum | **SADECE STAGE TEST** |

---

# Özet bölümleri

## 6. Stage / canlı doğrulama durumu

**KOD VE OTOMATİK TEST SEVİYESİ (doğrulandı, sahte HTTP + test veritabanı):**

- Listeleme: parametre iletimi (`status`, `page`, `size`, `barcode`, `startDate`, `endDate`, `orderByField`, `orderByDirection`), geçersiz değerlerin HTTP atmadan reddi.
- Sayfalama: tek sayfa, 3 sayfa, boş son sayfa, `totalPages=0`, ara sayfa hatasında `FAILED` + önceki sayfaların korunması, sonraki poll'da toparlanma.
- Mükerrer önleme, güncelleme, `raw_payload` roundtrip (iç içe ve dokümanda olmayan alanlar dahil).
- Detay: istek biçimi, 401/403/404/429/5xx/timeout, geçersiz 200 gövdesi, birleştirme.
- Cevaplama: sınır uzunlukları, `answerId` varyantları, bozuk 2xx gövde, tüm hata türlerinde yerel durumun yanlış güncellenmemesi, çift cevap engeli.
- Arayüz: kart içeriği, kaçış (XSS), uzun/Türkçe metin, ürün bilgisi yok, güvensiz URL'ler, cevaplananlar bölümü, hata mesajları, ham HTML'in gösterilmemesi.
- Sonuç: **277 Python testi + 16 JS testi geçti, skip/xfail yok** (JS testleri pytest içinde de bir test olarak çalışır).

**GERÇEK STAGE (henüz doğrulanmadı):**

- Bu uygulamadan gerçek Stage'e yapılan hiçbir istek henüz başarıyla tamamlanmadı; **gerçek Stage kabul testi tamamlanmadı.**
- Bildirim (doğrulanmamış, uygulama dışı): Postman'de Stage soru oluşturma 200 OK + `questionId` döndü.
- Sonradan alınan 403 yanıtları Cloudflare/IP erişim engeli olarak değerlendirilmektedir. Bu, kodla çözülecek bir sorun değil, altyapı/erişim sorunudur; çözülmeden Stage'de listeleme, sayfalama, detay, cevaplama ve arayüz akışı canlı olarak kanıtlanamaz.
- Gerçek bir Cloudflare engel sayfası bu projede hiç görülmedi; `ACCESS_BLOCKED` tespiti bilinen belirtilere dayanır (403 + JSON olmayan gövde + "cloudflare" metni veya `Server`/`CF-RAY` başlığı). Gerçek yanıt farklıysa tespit kaçabilir; bu durumda hata `AUTH_ERROR` olarak kalır ama ham HTML yine gösterilmez.
- Doküman çelişkileri gerçek yanıtla netleşecek: başarı gövdesi (`answerId` mi düz string mi), `answer` alt alanları, `size` üst sınırı (50/200), hata yanıtlarının HTTP kodları.
- Bu rapor "canlıda kesin çalışıyor" veya "Stage kabul testi tamamlandı" anlamına gelmez.

## 7. Hata yönetimi

| Durum | Uygulamadaki davranış |
|---|---|
| 401 | `AUTH_ERROR`, tekrar denenmez. Cevaplamada soru `FAILED` (retryable değil); kullanıcıya "kimlik bilgileri reddedildi, yöneticiye haber verin" |
| 403 (JSON gövde, ör. RBAC) | `AUTH_ERROR`, tekrar denenmez. Stage ekranında Trendyol'un kısa hata metni görünür (örn. `RBAC: access denied`) |
| 403 (HTML gövde + Cloudflare belirtisi) | `ACCESS_BLOCKED`, **tekrar denenmez** (blok uzamasın), cevaplamada soru `FAILED` ama **tekrar denenebilir** kalır; kullanıcıya "Trendyol erişimi engellendi (Cloudflare/IP kısıtı), bu bir uygulama hatası değil" gösterilir. Cloudflare/IP erişim sorunu kod hatası değildir |
| 403 (HTML, Cloudflare belirtisi yok) | `AUTH_ERROR`; ham HTML kullanıcıya veya `detail`'e girmez |
| 404 | `UNKNOWN_ERROR`, `status_code=404`, tekrar denenmez. (Özel "bulunamadı" kodu yok; gerçek 404 gövdesi görülmedi) |
| 429 | `RATE_LIMITED`; `Retry-After` kadar (en çok 60 sn) bekleyip 3 denemeye kadar tekrar; sonunda soru `FAILED` + tekrar denenebilir |
| 5xx | `SERVER_ERROR`; 3 deneme (1 sn, 2 sn); sonunda `FAILED` + tekrar denenebilir. HTML hata sayfası ekrana basılmaz |
| Timeout / bağlantı hatası | `NETWORK_ERROR`; 3 deneme; sonunda `FAILED` + tekrar denenebilir (Stage soru oluşturma hariç: tek deneme) |
| Bozuk JSON (2xx) | Cevaplama: başarı sayılır, `answer_id=None`. Detay: `UNKNOWN_ERROR` (geçerli soru nesnesi sayılmaz). Listeleme: poll `FAILED` kaydedilir |
| Bozuk/HTML yanıt (arayüz tarafı) | Arayüz "beklenmeyen yanıt" gösterir, ham gövdeyi göstermez |
| Dokümante cevap hataları | Bölüm 3'teki tablo (zaten cevaplandı / süre aşımı → `CLOSED`; yasaklı kelime → `FAILED`; kısa/uzun/boş → yerelde önceden reddedilir) |

Poll hatası: `PollRun.status=FAILED`, `error_message` (alan kodu + güvenli mesaj) kaydedilir, log yazılır, bir sonraki zamanlanmış poll yine çalışır. Hiçbir hata mesajında API Key/Secret veya ham Trendyol gövdesi bulunmaz **[TEST]**.

## 8. Aktif / destekleyici / test endpoint tabloları

**Aktif endpointler (gerçek iş akışı)**

| Endpoint | Kullanım |
|---|---|
| `GET /integration/qna/sellers/{sellerId}/questions/filter` | Soruları çekme (scheduler, her `POLL_INTERVAL_SECONDS`, varsayılan 300 sn) |
| `POST /integration/qna/sellers/{sellerId}/questions/{id}/answers` | Cevap gönderme (arayüz → `/api/answer`) |

**Destekleyici endpointler**

| Endpoint | Kullanım |
|---|---|
| `GET /integration/qna/sellers/{sellerId}/questions/{id}` | `get_question`: yalnız ihtiyaç halinde; poll çağırmaz |

**Sadece test endpointleri (Stage)**

| Endpoint | Kullanım |
|---|---|
| `POST /integration/qna/sellers/{sellerId}/questions` | Stage'de test sorusu oluşturma |
| `GET /integration/product/sellers/{sellerId}/product/{barcode}` | Test sorusu için `contentId` bulma (Product API) |

**Frontend'in kullandığı backend route'lar**

| Ekran | Aksiyon | Route | Trendyol'a giden istek |
|---|---|---|---|
| `/` | Bekleyen kartlar + "Son cevaplananlar" | `GET /` | Yok (DB'den okur) |
| `/` | Cevapla | `POST /api/answer` | `POST …/questions/{id}/answers` |
| `/stage` | Soruları Yenile | `POST /api/stage/poll` (yalnız Stage host'u) | `GET …/questions/filter` |
| `/stage` | Ürünü Getir / Test Sorusu | `POST /api/stage/product`, `POST /api/stage/questions` | Product GET / `POST …/questions` |
| (arka plan) | Otomatik senkron | `scheduler.poll_job` | `GET …/questions/filter` |

Not: ana ekranda manuel "yenile" düğmesi yoktur; otomatik senkron scheduler'dan gelir.

## 9. Veri akışı

```
Trendyol
  │  GET …/questions/filter  (status=WAITING_FOR_ANSWER, page=0..N, size=50, CreatedDate ASC)
  ▼
Soruları Listeleme ── Pagination: totalPages her sayfada okunur; boş/son sayfada durur;
  │                   ara sayfa hatasında poll FAILED (önceki sayfalar kalır, hata loglanır)
  ▼
Question kayıtlarını upsert ── unique trendyol_question_id → mükerrer yok;
  │                            raw_payload veya status değiştiyse güncelle
  ▼
raw_payload ── Trendyol'un tam soru nesnesi, her poll'da son halıyla (JSONB)
  │
  │   (gerekirse, poll dışında) GET …/questions/{id} → Tek Soru Detayı → merge_question_payload
  ▼
HTML'de gösterme ── GET /  : bekleyen kartlar (soru, durum, tarih, ürün, ürün kodu, link, resim)
  │                          + "Son cevaplananlar"
  ▼
Cevap Gönderme ── POST /api/answer → strip + 10–2000 doğrulama → NEW/REOPENED/FAILED(retryable)→ANSWERING
  │               (atomik kilit) → POST …/questions/{id}/answers
  ▼
answerId ── gövdede varsa int'e çevrilip saklanır; yoksa None
  ▼
Local status güncelleme ── başarı: ANSWERED (+answer_text, answer_id, answered_at)
                           zaten cevaplanmış / süre aşımı: CLOSED
                           diğer hata: FAILED (retryable ise tekrar denenebilir)
                           10 dk'dan uzun ANSWERING: reclaim job ile FAILED (retryable)
```

## 10. "Alınabilen tüm bilgiler" nasıl korunuyor?

- **Temel alanlar modelde ayrı kolonda:** `id`, `text`, `status`, `creationDate`, `userName`, `productName`, `productMainId` (ve gelirse `barcode`). Bunlar filtreleme, gösterme ve iş mantığı için kullanılır.
- **Tüm orijinal soru nesnesi `raw_payload` (JSONB) içinde:** `_map_question`, Trendyol'dan gelen `content[]` öğesini olduğu gibi `raw_payload`'a koyar; upsert her poll'da son tam nesneyle günceller. Dokümandaki tüm alanlar (`customerId`, `showUserName`, `public`, `imageUrl`, `webUrl`, `answeredDateMessage`, `answer`, `rejectedAnswer`, `reason`, `reportReason`, `reportedDate`, `rejectedDate`) ve ileride eklenebilecek, dokümanda olmayan alanlar kaybolmaz. İç içe nesneler dahil birebir geri okunduğu testle doğrulandı **[TEST]**.
- **Neden her alan için kolon açılmadı:** bu alanlar şu an hiçbir sorguda, filtrede veya iş mantığında kullanılmıyor (karar: kolonlaştırma yok; kolonlar geri alındı). Bir alan gerçekten sorgulanacağı gün tek bir migration ile kolonlaştırılabilir; veri `raw_payload`'da zaten mevcuttur.
- **Detay endpointi ek veri getirmez** (aynı şema), bu yüzden tüm bilgi için ikinci bir istek gerekmez.
- **Sınır:** Trendyol'un yanıtı yalnızca soru nesnesi için saklanır. Cevaplama yanıtının ham gövdesi DB'de ayrı saklanmaz (yalnız `answerId`); bu, soru verisinden ayrı bir konudur.
- **Dikkat:** scheduler tarih parametresi vermediği için Trendyol'un varsayılan "son 1 hafta" penceresi geçerlidir. Bu pencerenin dışındaki eski bekleyen sorular çekilmez (cevap süresi dokümanda 3 iş günü olarak geçer; gerekirse `start_date` ile genişletilir).

## 11. Yönetici özeti (son durum)

- **Soruları çekme kod seviyesinde tamamlandı mı?** Evet. Doküman parametreleri destekleniyor, otomatik senkron çalışıyor.
- **Pagination tamamlandı mı?** Evet, kod ve otomatik test seviyesinde: tüm sayfalar dolaşılıyor, ara sayfa hatası sessizce kaybolmuyor. Gerçek veriyle henüz denenmedi.
- **Veri kaybı önlendi mi?** Evet: Trendyol'un döndürdüğü tam soru nesnesi `raw_payload`'da saklanıyor; testle doğrulandı. Not: varsayılan "son 1 hafta" penceresi.
- **Soru cevaplama tamamlandı mı?** Evet, kod ve otomatik test seviyesinde: doğrulama, `answerId`, yerel durum, çift cevap engeli ve hata yönetimi.
- **HTML akışı hazır mı?** Evet: sorular ürün bilgisiyle gösteriliyor, cevap yazılıp gönderiliyor, sonuç ve durum görülüyor, hata mesajları anlaşılır, ham HTML gösterilmiyor. Tarayıcıda ve gerçek Stage'de gözle denenmedi.
- **Kalan tek dış bağımlılık:** Stage'e erişim (Cloudflare/IP engelinin kaldırılması / IP yetkilendirmesi). Çözülünce aşağıdaki kabul testi yapılacak; o zamana kadar gerçek Stage doğrulaması tamamlanmış sayılmaz.

---

# Gerçek Stage son kabul testi — kontrol listesi

Ön koşul: `.env`'de Stage `TY_SUPPLIER_ID`, `TY_API_KEY`, `TY_API_SECRET`, `TY_BASE_URL=https://stageapigw.trendyol.com`; uygulamanın çıkış IP'si Trendyol'a yetkilendirilmiş olmalı. Önce `flask --app app qna config-check` (değerler "set" görünür, secret yazılmaz; host `STAGE`).

Her adımda sonucu not et; **yalnızca gerçek 2xx alınan adım "Stage'de doğrulandı" sayılır.**

- [ ] **1. Listeleme → 2xx.** `flask --app app qna poll` → `PollRun.status=SUCCESS`; log/`flask --app app qna runs` ile `GET …/questions/filter` yanıtı 200.
- [ ] **2. Test sorusu oluştur → `questionId`.** `/stage` → "Test Sorusu Oluştur" (veya Postman). Yanıtta `questionId`; `correlationId` not edilir. Soru yayınlanmadan önce değerlendirilir, görünmesi gecikebilir.
- [ ] **3. Yeniden listele → soru görünür.** `qna poll` sonrası `flask --app app qna question <QID>`: `row_count=1`, `duplicate=false`, `local_status=NEW`, `trendyol_status=WAITING_FOR_ANSWER`.
- [ ] **4. (Gerekirse) `get_question` → aynı soru.** Tek soru detayı 2xx; alanlar listedekiyle aynı; `merge_question_payload` ile veri kaybı yok. Detay gövdesi ve `answer` alt alan adları not edilir (dokümanlar çelişiyor).
- [ ] **5. Cevap endpointi → `answerId`.** `flask --app app qna stage-e2e --question-id <QID> --answer "…" --dry-run`, sonra `--send` (veya adım 7'deki arayüz; ikisi birden değil). Yanıt 2xx; başarı gövdesi biçimi (`answerId` mi, düz string mi) ve HTTP status not edilir.
- [ ] **6. Yeniden listele → durum/cevap güncellenmiş.** `qna poll` sonrası `qna question <QID>`: yerelde `ANSWERED` (`answer_text`, `answer_id` doluysa), Trendyol tarafı `ANSWERED` veya `REOPENED` olabilir; ikinci poll'da tek satır, mükerrer yok.
- [ ] **7. HTML üzerinden aynı akış.** `python run.py` → `/stage` "Soruları Yenile" → soru tabloda; "Cevapla" bağlantısı → `/` kartı; 10 karakter altı cevapta buton kapalı; geçerli cevap → "ANSWERED"; sayfa yenilenince "Son cevaplananlar"da görünür; aynı soruya ikinci cevap gönderilemez.
- [ ] **8. `correlationId` / hata logu kontrolü.** Hata varsa yanıt ve loglarda `correlationId` var mı; loglarda API Key/Secret veya ham HTML yok; Cloudflare/HTML yanıtı gelirse kullanıcıya `ACCESS_BLOCKED` mesajı çıkıyor ve ham gövde görünmüyor.
- [ ] **9. `raw_payload` kontrolü.** DB'de ilgili satırın `raw_payload`'ı Trendyol yanıtındaki tam soru nesnesine eşit; dokümandaki tüm alanlar (özellikle `answer` / `rejectedAnswer` alt alanları) ve beklenmeyen ek alanlar var mı. `answer` alt alan adlarını (reference mı rehber mi doğru) kayda geçir.
- [ ] **10. Pagination'ı gerçek veriyle kontrol.** Birden fazla sayfa üretecek kadar test sorusu oluştur (ör. `size=50`'den fazla veya testte küçük `page_size` ile `fetch_waiting_questions`); tüm sayfaların istendiğini (`page=0,1,2…`), `fetched_count`'un `totalElements` ile tutarlı olduğunu ve mükerrer satır olmadığını doğrula. `size` üst sınırının (50/200) gerçekte hangisi olduğunu not et. Varsayılan "son 1 hafta" penceresini ve `startDate/endDate` filtresini de dene.

Kabul sonucu yazılırken: her madde için tarih, HTTP status, `correlationId` (varsa) ve geçti/kaldı.

---

Kaynaklar (resmi dokümantasyon):
[Müşteri Sorularını Çekme](https://developers.trendyol.com/docs/m%C3%BC%C5%9Fteri-sorular%C4%B1n%C4%B1-%C3%A7ekme) ·
[Müşteri Sorularını Filtreleme (reference)](https://developers.trendyol.com/reference/getquestionfilter) ·
[Soru Detayını Getirme (reference)](https://developers.trendyol.com/reference/getquestion) ·
[Müşteri Sorularını Cevaplama](https://developers.trendyol.com/docs/m%C3%BC%C5%9Fteri-sorular%C4%B1n%C4%B1-cevaplama) ·
[Soruyu Cevaplama (reference)](https://developers.trendyol.com/reference/answerquestion) ·
[Answering Customer Questions](https://developers.trendyol.com/v2.0/docs/answering-customer-questions) ·
[Getting Customer Questions](https://developers.trendyol.com/v2.0/docs/getting-customer-questions) ·
[Creating Customer Questions in Stage Environment](https://developers.trendyol.com/v2.0/docs/creating-customer-questions-in-stage-environment) ·
[Product Filter – Base Information v2](https://developers.trendyol.com/v3.0/docs/product-filter-base-information-v2) ·
[Canlı-Test Ortam Bilgileri](https://developers.trendyol.com/docs/3-canl%C4%B1-test-ortam-bilgileri) ·
[Changelog](https://developers.trendyol.com/changelog/changelog)
