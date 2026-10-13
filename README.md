# Social Crawler

Hệ thống crawl dữ liệu mạng xã hội theo **từ khoá**, cho 4 nền tảng: **Threads, Instagram, Facebook, TikTok**.

Nhập keyword trên UI → hệ thống tự điều hướng tới trang search của từng nền tảng → cuộn thu thập
toàn bộ bài viết → mở từng bài lấy nội dung + comment → lưu vào database → export Excel/CSV/JSON.

---

## 1. Quyết định kỹ thuật cốt lõi

Khi crawl thủ công, cách đọc text hiển thị có 2 nhược điểm chí mạng:

| Vấn đề | Hệ quả |
|---|---|
| Số liệu bị làm tròn (`1.9K`, `2K`) | Không biết chính xác 1.900 hay 1.949 |
| Số 0 **không được render** | Không phân biệt được "0 repost" và "không đọc được repost" |

Vì vậy hệ thống này **không scrape DOM làm nguồn chính**, mà lấy số liệu theo **3 nguồn ưu tiên**:

| # | Nguồn | Khi nào có | Độ chính xác |
|---|---|---|---|
| 1 | Chặn XHR/GraphQL (`harvester.py`) | Đã đăng nhập, SPA tự gọi API | Số nguyên chính xác |
| 2 | **JSON nhúng sẵn trong HTML** (`document.py`) | Chưa đăng nhập, trang render server-side | Số nguyên chính xác |
| 3 | Text đã render (DOM) | Khi cả 2 nguồn trên trống | Làm tròn (`1,9K`) |

Nguồn #2 quan trọng hơn vẻ ngoài của nó. Đo thực tế trên một bài Threads khi **chưa đăng nhập**:
trang **không phát bất kỳ request GraphQL nào** (chỉ có telemetry) — nên nguồn #1 bắt được 0 payload.
Nhưng cũng trang đó nhúng sẵn **~1,3 MB JSON** trong thẻ `<script type="application/json">`, trong đó
có `like_count` chính xác. Thiếu `document.py`, hệ thống sẽ âm thầm tụt xuống nguồn #3 và mất độ
chính xác mà lẽ ra không cần mất:

```
Cùng một bài viết:
  Đọc DOM  → likes "1.9K"  shares "2K"    thời gian "1d"          22 comment
  Hệ thống → likes 1918    shares 2040    2026-09-11T08:24:17Z    29 comment
```

Hệ quả trong data model: mọi metric đều **nullable**.
`NULL` = *không thu được số này*, khác hoàn toàn `0` = *xác nhận bằng không*.
Cột `metrics_source` ghi rõ `api` (chính xác) hay `dom` (làm tròn).

Hai bài học khác được mã hoá thẳng vào `adapters/threads.py`:

1. **Loại bỏ "Related threads"** — trang post của Threads render cả reply thật lẫn khối bài gợi ý
   không liên quan. Hệ thống lọc 2 lớp: (a) loại payload theo tên GraphQL operation chứa
   `related/recommend`, (b) reply thật luôn có `text_post_app_info.reply_to_author`, bài gợi ý thì không.
2. **Phân biệt reply cấp 1 / cấp 2** — `reply_to_author` cho biết reply *cho ai*, từ đó suy ra
   `depth` và `parent_id`.

---

## 2. Kiến trúc

```
app/
├── core/           Config, enums, logging, errors, utils (parse số, retry, jitter)
├── domain/         Entity trung lập nền tảng: RawPost, RawComment, Metrics, AuthorRef
├── db/
│   ├── models.py       SQLAlchemy ORM
│   ├── session.py      Async engine + PRAGMA (WAL, foreign_keys)
│   └── repositories/   crawl_repo (ghi) · job_repo (vòng đời job) · query_repo (đọc)
├── crawler/
│   ├── browser.py      Vòng đời Playwright, context theo platform, lưu session an toàn
│   ├── harvester.py    Chặn & buffer JSON response  ← lõi của chiến lược trích xuất
│   ├── miner.py        Duyệt JSON theo "hình dạng" thay vì theo path cố định
│   ├── scroller.py     Engine infinite-scroll dùng chung
│   ├── ratelimit.py    Token bucket
│   ├── base.py         PlatformAdapter (ABC) + CrawlContext + registry
│   └── adapters/       threads · instagram · facebook · tiktok · meta_common
├── services/
│   ├── crawl_service.py    Orchestration 1 job (search → post → DB)
│   ├── job_manager.py      Hàng đợi, worker, huỷ job, phục hồi sau restart
│   ├── session_service.py  Đăng nhập 1 lần / nền tảng
│   └── export_service.py   XLSX (gộp ô theo bài) / CSV / JSON
├── api/            FastAPI routes + Pydantic schemas + mappers
└── main.py         App factory + lifespan
web/                UI vanilla JS (không cần build step)
```

**Vì sao `miner.py` duyệt theo hình dạng, không theo path?** Schema GraphQL của Meta/TikTok đổi
tên wrapper liên tục. Một path cứng như `data.feedback.reaction_count` sẽ vỡ ngay lần deploy
sau. Adapter chỉ mô tả *"node giống post trông thế nào"* rồi để miner đi tìm.

---

## 3. Database schema

| Bảng | Vai trò |
|---|---|
| `crawl_jobs` | 1 job = 1 keyword × nhiều nền tảng |
| `job_targets` | 1 dòng / (job, platform) — mỗi nền tảng thành/bại độc lập |
| `authors` | Định danh qua `identity_key` (id số, fallback username) |
| `posts` | Bài viết + **metric mới nhất** (denormalize để list/sort nhanh) |
| `post_metric_snapshots` | **Lịch sử metric theo thời gian** — 1 snapshot / (post, job) |
| `comments` | Có `parent_id`, `depth`, `is_author_reply` |
| `job_posts` | n-n: 1 bài có thể xuất hiện ở nhiều keyword khác nhau |

Toàn bộ ghi là **idempotent** (`INSERT … ON CONFLICT DO UPDATE`). Crawl lại cùng keyword sẽ
cập nhật chỗ cũ, không nhân bản. Các cột metric dùng `COALESCE(new, old)` nên **lần crawl sau
thu được ít hơn sẽ không xoá mất dữ liệu lần trước**.

---

## 3b. PostgreSQL

Mặc định project chạy SQLite (không cần cài gì). Để dùng PostgreSQL:

```bash
# 1. Tạo role + database + schema + extensions (chạy 1 lan, quyen superuser)
psql -U postgres -h 127.0.0.1 -p 5433 -v app_password=MAT_KHAU_APP -f scripts/bootstrap_postgres.sql

# 2. Đặt SC_DATABASE_URL trong .env
#    postgresql+asyncpg://social_crawler:MAT_KHAU_APP@127.0.0.1:5433/social_crawler

# 3. Tạo bảng + index
python scripts/setup_postgres.py

# 4. (tuỳ chọn) chuyển dữ liệu SQLite cũ sang
python scripts/migrate_sqlite_to_postgres.py
```

**Cùng một code chạy được cả hai backend.** `app/db/dialects.py` chọn constructor
`INSERT ... ON CONFLICT` theo bind thật, nên repository không bị khoá vào SQLite —
`sqlalchemy.dialects.sqlite.insert` tạo câu lệnh mà Postgres từ chối thẳng. `PRAGMA` chỉ chạy khi
dialect là SQLite, và cột `JSON` tự thành `JSONB` trên Postgres.

**Phân quyền**: app **không bao giờ** kết nối bằng superuser. Role `social_crawler` sở hữu đúng
schema `crawler` của nó, `public` bị thu hồi quyền — một lỗi trong crawler không với tới phần còn
lại của cluster.

**7 index Postgres-only** (trong `setup_postgres.py`, mỗi cái kèm lý do). Đáng chú ý:
`ix_posts_content_trgm` dùng GIN + `pg_trgm` vì UI lọc `content LIKE '%tu khoa%'` — không có nó thì
mỗi lần tìm là một lần quét toàn bảng; `ix_authors_username_lower` vì query so sánh `lower(username)`
nên index trên cột gốc vô dụng; `ix_jobs_unfinished` là partial index, giữ nhỏ mãi dù lịch sử lớn dần.

## 4. Cài đặt

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

> Nếu bạn dùng venv của IDE (PyCharm tạo `.venv`), nhớ cài vào **đúng** interpreter đang chạy —
> `ModuleNotFoundError: No module named 'uvicorn'` gần như luôn là dấu hiệu cài nhầm Python.

Hệ thống chạy Chromium đầy đủ ở chế độ headless mới (`chromium_channel = "chromium"`) thay vì binary
`headless-shell` riêng: đỡ một lần tải và render giống trình duyệt thật hơn. Đặt `SC_CHROMIUM_CHANNEL=`
(để trống) nếu muốn quay về mặc định của Playwright.

Chạy:

```bash
python run.py
```

Mở http://127.0.0.1:8000

Tuỳ chỉnh: copy `.env.example` → `.env`.

---

## 5. Cách dùng

1. **Đăng nhập 1 lần** cho Threads / Instagram / Facebook ở panel *Phiên đăng nhập*.
   Hệ thống mở cửa sổ Chromium thật, bạn tự đăng nhập — **không có mật khẩu nào đi qua service này**,
   chỉ cookie phiên được lưu vào `sessions/<platform>.json`.
2. Nhập **từ khoá**, chọn nền tảng, đặt số bài tối đa → **Bắt đầu crawl**.
3. Theo dõi tiến độ realtime, bấm vào 1 dòng kết quả để xem toàn bộ comment.
4. Export **Excel / CSV / JSON**.

### Self-test

```bash
python scripts/selftest.py
```

Kiểm tra parse số, ngữ nghĩa merge metric, tính idempotent, resolve `parent_id` của comment và
cả 3 exporter — **không cần browser**.

---

## 6. Mức độ tin cậy theo nền tảng

| Nền tảng | Trạng thái | Ghi chú |
|---|---|---|
| **Threads** | **Đã chạy thật, đối chứng số liệu** | Lấy đúng post + 29 comment + metric chính xác khi *chưa* đăng nhập. Phần `search` vẫn cần session. |
| TikTok | **Đã chạy thật, crawl được KHÔNG cần đăng nhập** | Nhưng **bắt buộc cửa sổ hiển thị**: headless bị trả trang "Đã xảy ra lỗi" (UA chứa `HeadlessChrome`), headed trả 11-12 kết quả bình thường. Adapter đặt `prefers_headed=True` nên tự bật cửa sổ kể cả khi `SC_HEADLESS=true` |
| Instagram | Chưa đối chứng tài khoản thật | Dùng chung shape với Threads (`meta_common`) |
| Facebook | **Dễ phải chỉnh nhất** | GraphQL nhiều lớp wrapper đổi liên tục |

Kết quả đo TikTok (job thật, ẩn danh, qua toàn bộ pipeline):

```
@dinhvakyuchanoi    likes=220  cmt=4   share=11   views=3.219   saves=4    src=api
@nguyentranphongvu  likes=502  cmt=47  share=380  views=35.500  saves=51   src=api
```

**"Bị từ chối" ≠ "cần đăng nhập".** Một bản trước đặt `requires_login=True` cho TikTok sau khi chỉ
thấy lần từ chối ở chế độ headless — suy luận sai từ một quan sát đúng. Vì vậy hai khái niệm này
được tách hẳn trong code:

- `AuthenticationRequiredError` → `needs_auth`: đăng nhập sẽ giải quyết được
- `PlatformBlockedError` → `blocked`: đăng nhập **không** giải quyết được, kèm `remedy` chỉ rõ cách sửa

### Comment TikTok: vì sao từng về 0, và cách đã sửa

Trang video TikTok mở sẵn ở tab **"Bạn có thể thích"**, không phải tab **"Bình luận"** — nên
`/api/comment/list/` **không bao giờ được gọi** và DOM không có dòng comment nào. Không liên quan
đến đăng nhập. Ba lớp phải xử lý, mỗi lớp đều được xác minh bằng đo đạc:

1. **Bấm tab "Bình luận"** — span này *không có* `data-e2e` lẫn `role`, phải tìm theo nhãn (và nhãn
   đổi theo ngôn ngữ). Phải **chờ tab render** rồi mới bấm: kiểm tra `count()` một lần thì chạy tay
   được nhưng trong crawl thì trượt, đúng kiểu tạo ra "0 comment" trên video có hàng chục comment.
2. **Cuộn đúng container.** Danh sách nằm trong một div `overflow-y: scroll` lồng bên trong; cuộn
   window hay `scrollIntoView` để `scrollTop` nguyên bằng 0 → handler phân trang không chạy, dù API
   đã báo `has_more: 1, cursor: 20, total: 47`. Phải tìm ancestor cuộn được rồi đẩy `scrollTop` xuống đáy.
3. **Mở các cụm reply bị thu gọn** ("Xem N câu trả lời") — reply không được tải cho đến khi bấm,
   nhưng vẫn được tính vào tổng số comment của video.

Tiến triển đo được trên cùng một video (47 comment): `0 → 30 → 39 → 46`. Một comment lệch gần như
chắc chắn là comment bị TikTok lọc (payload có cờ `has_filtered_comments`).

**Thứ tự xử lý được đảm bảo bằng cấu trúc code**: `fetch_post()` chỉ trả về sau khi đã mở tab, cuộn
hết danh sách và bung mọi reply; `_crawl_refs()` chỉ sang bài kế sau khi bài hiện tại đã lưu xong.
Không có xử lý song song giữa các bài.

Phần chưa kiểm chứng: luồng **đã đăng nhập** của Threads/Instagram/Facebook (cần tài khoản thật).

Khi một nền tảng đổi cấu trúc, chỗ cần sửa gần như luôn nằm gọn trong 1 file adapter — phần lõi
(harvester, miner, scroller, DB) không phải đụng tới.

### Thêm nền tảng mới

Tạo `app/crawler/adapters/<tên>.py`, kế thừa `PlatformAdapter`, hiện thực 4 method
(`build_search_url`, `is_authenticated`, `collect_post_refs`, `fetch_post`), gắn `@register_adapter`,
thêm vào `Platform` enum và import trong `adapters/__init__.py`. UI tự động hiện nền tảng mới.

---

## 7. Xử lý sự cố đăng nhập (CAPTCHA lặp vô tận)

Triệu chứng: trả lời captcha đúng nhưng Facebook/Instagram hỏi lại mãi không thoát.

Đây gần như luôn là **lỗi phía công cụ**, không phải do bạn trả lời sai. Hai nguyên nhân đã đo được
và đã sửa trong bản này:

**1. User-Agent nói dối.** Playwright chỉ ghi đè *chuỗi* User-Agent, còn `Sec-CH-UA` client hints và
`navigator.userAgentData` vẫn khai báo build thật. Khi config ép `Chrome/131` trong khi binary là
Chromium 149, mỗi request tự mâu thuẫn — đúng thứ mà hệ thống chống bot tìm kiếm:

```
Trước:  UA = Chrome/131.0.0.0   |  client hints = Chromium 149    ✗ mâu thuẫn
Sau:    UA = Chrome/149.0.0.0   |  client hints = Chromium 149    ✓ khớp
```

`SC_USER_AGENT` giờ mặc định rỗng = để trình duyệt tự khai đúng. **Đừng đặt lại giá trị này.**

**2. Danh tính trình duyệt bị vứt sau mỗi lần thử.** Trước đây mỗi lần bấm "Đăng nhập" tạo một cookie
jar trắng, nên cookie định danh `datr` mới toanh mỗi vòng. CAPTCHA tồn tại để **gắn trust token vào
một trình duyệt ổn định** — không có danh tính ổn định thì không bao giờ "qua" được.

Bản này dùng **persistent profile** cho từng nền tảng (`profiles/<platform>/`):

```
run 1 → datr = hFinaovFnj-e
run 2 → datr = hFinaovFnj-e     ← cùng một trình duyệt, qua nhiều lần mở
```

**3. Kết luận "đã đăng nhập" quá sớm → bị đá về login rồi phải verify lại.** `has_live_session` trước
đây dùng `any()`: chỉ cần **một** cookie trong danh sách là coi như xong. Facebook khai
`("c_user",)` — nhưng `c_user` chỉ là **user ID**, được set *sớm ngay trong lúc checkpoint*; token
phiên thật là `xs`. Hệ quả: vừa giải xong captcha là hệ thống lưu một phiên **chưa hoàn tất** rồi
**đóng luôn cửa sổ giữa chừng**, nên lần sau Facebook đá thẳng về login.

Đã sửa thành:

- `has_live_session` yêu cầu **tất cả** cookie trong danh sách (`c_user` **và** `xs`)
- Thêm `blocking_cookie_names` — có cookie `checkpoint` nghĩa là **chưa** đăng nhập
- Thêm `confirm_session()`: tải trang thật bằng một tab riêng và kiểm tra không bị chuyển hướng về
  `/login`, `/checkpoint`, `/two_step`… Cookie có thể nói dối trong lúc challenge; chỉ lần tải trang
  thành công mới là bằng chứng
- Phải **xác nhận 2 lần liên tiếp** cách nhau vài giây mới lưu phiên và đóng cửa sổ

### TikTok trả `ERR_HTTP_RESPONSE_CODE_FAILURE` / HTTP 403

Đây là **rate-limit tạm thời**, không phải URL hỏng hay account bị khoá. Đã kiểm chứng: đúng video
vừa lỗi, sau khi nghỉ 60 giây thì trả `200` bình thường.

Cách hệ thống xử lý:

- `rate_per_minute = 10` riêng cho TikTok (chậm hơn mức chung 20) — trang video rất nặng
- Gửi `Referer` của trang search khi mở video, vì ta **thực sự** đến từ đó
- Backoff dài hơn khi nhận mã lỗi HTTP (20s, 40s) thay vì 2s/4s như lỗi mạng thường
- **Adaptive cooldown**: khi một bài bị chặn, cả nền tảng bị *tạm nghỉ* trước khi sang bài kế
  (45s, tăng dần nếu tái diễn). Đây mới là thứ chặn hiệu ứng dây chuyền — thử lại bài đang lỗi
  mạnh hơn thì không giải quyết được gì
- **`rate_burst = 1` cho TikTok.** `RateLimiter` khởi tạo bucket **đầy**, nên burst mặc định 5 nghĩa
  là **5 request đầu bắn liên tiếp không bị chặn gì cả** — `rate_per_minute` chỉ có hiệu lực từ
  request thứ 6. Đây là thứ vô hiệu hoá chính cái giới hạn tốc độ vừa đặt
- **Vòng thử lại thứ hai**: bài lỗi vì bị chặn tốc độ được *để dành* rồi chạy lại sau khi hết
  cooldown, thay vì bỏ luôn. Chỉ bài lỗi ở lần hai mới tính là `failed`

Kết quả đo trên cùng bộ URL:

| | trước | sau |
|---|---|---|
| job 4 bài | `crawled=2 failed=2` | `crawled=4 failed=0` |
| job 8 bài | — | **`crawled=8 failed=0`** (74s, không cảnh báo throttle nào) |

> **Không có gì đảm bảo 403 "không bao giờ" xảy ra** — đó là quyết định của hệ thống chống bot phía
> nền tảng. Mục tiêu của các thay đổi trên là làm nó *hiếm* và *không gây mất dữ liệu khi xảy ra*.

Nếu vẫn gặp 403 liên tục: giảm `SC_RATE_PER_MINUTE`, giảm `max_posts`, và nghỉ vài phút. Đừng tăng
tốc để "vượt" — đó là cách chắc chắn bị chặn lâu hơn.

Nếu vẫn bị hỏi lại:
- Đăng nhập **một lần trong cửa sổ hiện ra**, làm chậm rãi như người thật, đừng đóng giữa chừng.
- Nếu crawl (headless) bị challenge, đặt `SC_HEADLESS=false` để chạy bằng cửa sổ thật cùng profile đó.
- Nút **Xoá** phiên sẽ xoá cả `sessions/*.json` lẫn `profiles/<platform>/` để bắt đầu lại sạch.

> Công cụ này **không** giải captcha tự động, **không** spoof fingerprint, **không** xoay proxy. Các
> sửa đổi trên chỉ làm trình duyệt khai báo trung thực và nhất quán về chính nó. Nếu nền tảng vẫn từ
> chối, hãy tôn trọng quyết định đó thay vì tìm cách vượt.

## 8. Giới hạn & lưu ý

- **Tôn trọng ToS**: crawl tự động có thể vi phạm điều khoản dịch vụ của các nền tảng. Công cụ này
  dùng chính tài khoản của bạn, chạy với tốc độ người thật (token bucket + delay ngẫu nhiên),
  và **không** cài bất kỳ cơ chế né phát hiện nào (không giải captcha, không xoay proxy, không giả
  mạo fingerprint). Nếu bị chặn: giảm `SC_RATE_PER_MINUTE`, đừng cố vượt.
- Mặc định chạy **1 job tại một thời điểm** — mỗi job điều khiển một browser thật.
- Số comment lấy được vẫn phụ thuộc nền tảng cho hiển thị bao nhiêu; giới hạn bằng
  `max_comments_per_post`, cờ `comments_truncated` cho biết đã bị cắt.
- **Threads hạ cấp phiên ẩn danh theo thời gian.** Quan sát trực tiếp: cùng một bài, lần đầu trả
  đủ 29 reply trong payload SSR; sau vài chục lượt truy cập không đăng nhập thì chỉ còn bài gốc,
  phần reply bị thay bằng feed "Thịnh hành". Khi đó hệ thống ghi chú rõ
  *"platform reports N replies but none were served"* thay vì lưu 0 một cách im lặng — so sánh
  `comments_count` (nền tảng công bố) với `comments_collected` (thực thu) để biết độ đầy đủ.
  Đăng nhập là cách khắc phục.
- Dữ liệu cá nhân thu thập được cần được xử lý theo quy định bảo vệ dữ liệu hiện hành.

## 9. Deploy bằng Docker

### 9.1. Vì sao kiến trúc lại thế này

Hai ràng buộc thật của chính dự án này quyết định toàn bộ thiết kế container, không phải sở thích:

1. **TikTok đo được là từ chối trình duyệt headless** (xem mục 6) — mọi crawl TikTok đều bật
   `headless=False`. Container không có màn hình thật, nên cần **Xvfb** (X server ảo) để Chromium
   "headed" có nơi để vẽ, dù không ai đang nhìn.
2. **Luồng đăng nhập tương tác** (Threads/Instagram/Facebook) cố ý mở cửa sổ trình duyệt thật để
   bạn tự nhập tài khoản. Trong container không có cách nào *nhìn thấy* cửa sổ đó — nên container
   đóng gói thêm **x11vnc + noVNC**, để bạn mở `http://<host>:6080/vnc.html` bằng trình duyệt bất kỳ
   là thấy và điều khiển được cửa sổ đăng nhập bên trong container.

**Base image** là `mcr.microsoft.com/playwright/python` (không phải `python:slim` tự cài
`playwright install`), vì chính dự án này từng dính lỗi thật do lệch version giữa gói pip
Playwright và bản Chromium trên đĩa (mục 6, phần "chromium_headless_shell"). Image chính thức của
Microsoft đóng gói sẵn Chromium đúng revision khớp với gói pip cùng version — loại bỏ hẳn lớp lỗi
đó. Đổi lại image nặng hơn (có cả Firefox/WebKit không dùng tới) — đánh đổi có chủ đích, ưu tiên
đúng hơn nhẹ.

### 9.2. Cài đặt lần đầu

```bash
cp .env.docker.example .env.docker
```

Mở `.env.docker`, đổi `POSTGRES_APP_PASSWORD` và `VNC_PASSWORD` sang giá trị thật (không để nguyên
`change-me...`). File này **khác** `.env` dùng khi chạy `python run.py` trực tiếp trên host —
`.env` trỏ vào Postgres host cổng 5433, còn `.env.docker` trỏ vào Postgres container. Cố tình tách
riêng để tránh nhầm `127.0.0.1` của host với `127.0.0.1` *bên trong container* (hai địa chỉ khác
nhau hoàn toàn).

```bash
docker compose --env-file .env.docker up -d --build
```

Lần đầu chạy, `entrypoint.sh` tự động gọi `scripts/setup_postgres.py` để tạo bảng + 7 index
Postgres-only — không cần bước thủ công nào thêm.

- App: `http://localhost:8088` (đổi qua `APP_HOST_PORT` trong `.env.docker`)
- Đăng nhập tương tác: `http://localhost:6080/vnc.html` (đổi qua `NOVNC_HOST_PORT`), nhập
  `VNC_PASSWORD` bạn đặt ở trên khi được hỏi
- Postgres (cho pgAdmin/psql từ host): `localhost:5434` (đổi qua `POSTGRES_HOST_PORT`) — mặc định
  **5434** vì cổng 5432/5433 trên máy này đã có sẵn 2 bản Postgres khác đang chạy

### 9.3. Chuyển dữ liệu đã crawl từ Postgres trên host vào container

Nếu bạn đã có dữ liệu trong Postgres cài trên host (như README mục 3b mô tả), container khởi động
lên sẽ **trống** — cần chuyển thủ công một lần:

```bash
docker compose --env-file .env.docker up -d db   # chỉ bật Postgres trước
scripts/migrate_host_postgres_to_docker.sh
```

Script dùng `pg_dump`/`pg_restore` sẵn có ở `C:\Program Files\PostgreSQL\18\bin`, chỉ chuyển dữ
liệu (schema đã được `docker/postgres-init.sql` + `setup_postgres.py` tạo sẵn phía container).

### 9.4. Bảo mật khi đưa lên server thật

`docker-compose.yml` đã tự bind mọi cổng publish (`app`: 8088 + 6080, `db`: 5434) vào `127.0.0.1`
thay vì `0.0.0.0` — dùng `localhost` trên máy đang chạy Docker vẫn vào được y như trước, nhưng
máy khác trên mạng (hoặc internet) không thể chạm thẳng vào các cổng này nữa dù có biết địa chỉ IP.
Đây là lý do: app hiện **không có hệ thống đăng nhập riêng**, và VNC hiển thị nguyên màn hình đăng
nhập thật của Threads/Instagram/Facebook — mở thẳng các cổng này ra ngoài coi như để ngỏ toàn bộ dữ
liệu đã crawl lẫn nguy cơ lộ phiên đăng nhập cho bất kỳ ai quét được cổng.

- **VNC không nên lộ ra internet trần trụi.** Cơ chế xác thực của giao thức VNC (mà `x11vnc` dùng)
  là DES cũ, yếu theo chuẩn hiện đại — đủ để chặn tiến trình khác trên cùng máy hoặc quét mạng
  ngẫu nhiên, không đủ để chống một kẻ tấn công chủ đích. Việc bind `127.0.0.1` ở trên đã lo phần
  này; khi cần đăng nhập platform trên server thật, dùng SSH tunnel (mục 9.6) chứ đừng đổi cổng này
  sang `0.0.0.0`.
- Đặt `.env.docker` với mật khẩu mạnh, không commit (đã có trong `.gitignore`/`.dockerignore`).
- Muốn mở app ra cho nhiều người dùng chung qua internet: xem mục 9.6 — dùng `docker-compose.prod.yml`
  để thêm Caddy làm reverse proxy, HTTPS tự động, và một lớp xác thực chung cho cả team.

### 9.5. Sự cố Docker Desktop gặp khi soạn phần này (không liên quan code của dự án)

Lúc build thử trên máy phát triển, Docker Desktop báo lỗi khởi động:

```
starting services: initializing Inference manager: listening on unix://...\dockerInference:
remove ...\dockerInference: The file cannot be accessed by the system.
```

Đây là lỗi ở **chính Docker Desktop trên Windows**, không phải ở Dockerfile/compose của dự án:
các file Unix-domain-socket nội bộ của Docker (`AppData\Local\Docker\run\dockerInference`,
`AppData\Local\docker-secrets-engine\engine.sock`) rơi vào trạng thái hỏng, không xoá/đọc được
qua API thường (kể cả sau khi tắt hẳn tiến trình Docker và `wsl --shutdown`). Nghi vấn hợp lý nhất:
**Windows Defender real-time protection** (xác nhận đang bật trên máy này) chặn/khoá việc tạo các
file socket đặc biệt đó — đây là lớp lỗi đã được cộng đồng Docker Desktop trên Windows ghi nhận.

Việc thêm ngoại lệ Defender cho thư mục dữ liệu Docker là **thay đổi liên quan bảo mật hệ thống**,
nên không tự ý thực hiện — cần bạn quyết định. Nếu gặp lại lỗi này:

1. Thử **Docker Desktop → Troubleshoot → Clean / Purge data**, hoặc gỡ cài đặt lại Docker Desktop.
2. Nếu vẫn lỗi, cân nhắc thêm ngoại lệ Windows Defender cho `C:\Users\<user>\AppData\Local\Docker\`
   và `C:\ProgramData\DockerDesktop\` (tự quyết định, đây là việc hạ thấp một lớp bảo vệ).
3. `docker compose --env-file .env.docker config` (không cần daemon chạy) đã được xác minh **chạy
   sạch, exit 0** trong phiên làm việc này — nghĩa là toàn bộ `docker-compose.yml` hợp lệ; phần
   còn thiếu duy nhất là `docker build` thật, chưa chạy được vì daemon chưa lên.

### 9.6. Đưa app ra cho team dùng chung qua internet

Áp dụng khi: vài người trong team, dùng chung 1 bộ session đăng nhập platform và 1 database là
chấp nhận được (không cần tách dữ liệu theo từng người) — nhưng muốn truy cập từ ngoài mạng nội bộ,
qua một domain thật. `docker-compose.prod.yml` thêm 1 service `caddy` làm reverse proxy: tự xin
chứng chỉ HTTPS (Let's Encrypt) cho domain, và chặn mọi request bằng HTTP Basic Auth chung cho cả
team trước khi cho chạm tới app — vì bản thân app không có hệ thống đăng nhập riêng.

**Yêu cầu trước khi bắt đầu** (phần này người dùng tự làm, không thể làm thay từ máy dev):

- Một VPS Linux (Ubuntu 22.04/24.04 khuyến nghị) có IP công khai — DigitalOcean, Vultr, Linode,
  AWS Lightsail... đều được. Cấu hình tối thiểu: 2 vCPU / 4GB RAM (Chromium + Postgres cần đủ RAM,
  8GB sẽ thoải mái hơn nếu chạy nhiều job song song).
- Một domain (hoặc subdomain) đã trỏ **A record** về IP của VPS đó. Caddy cần DNS trỏ đúng *trước*
  khi khởi động lần đầu, vì nó xin chứng chỉ HTTPS ngay lúc start.
- Cài Docker + Docker Compose plugin trên VPS (Ubuntu: xem hướng dẫn chính thức tại
  `docs.docker.com/engine/install/ubuntu/` — không cần Docker Desktop trên Linux server).

**Các bước:**

1. Copy toàn bộ project lên VPS (`git clone` nếu đã push lên remote, hoặc `scp -r`/`rsync`).

2. Tạo `.env.docker` trên VPS từ `.env.docker.example`, điền:
   ```
   POSTGRES_APP_PASSWORD=<mật khẩu mạnh>
   VNC_PASSWORD=<mật khẩu mạnh>
   DOMAIN=crawler.congty-cua-ban.com
   BASIC_AUTH_USER=team
   BASIC_AUTH_HASH=<xem bước 3>
   ```

3. Sinh hash mật khẩu Basic Auth (chạy trên VPS hoặc máy dev, không cần daemon compose):
   ```bash
   docker run --rm caddy:2-alpine caddy hash-password --plaintext 'mật-khẩu-that-manh'
   ```
   Dán kết quả vào `BASIC_AUTH_HASH` ở bước 2. Đây là mật khẩu **chung cho cả team** — chia sẻ nó
   (cùng `BASIC_AUTH_USER`) cho từng người qua kênh riêng tư, không qua trang web/chat công khai.

4. Build và chạy — dùng `-f` gộp cả 2 file compose (base **và** overlay, thiếu 1 trong 2 là sai):
   ```bash
   docker compose --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml up -d --build
   ```

5. Vào `https://crawler.congty-cua-ban.com` — trình duyệt sẽ hỏi user/password (Basic Auth) trước
   khi thấy giao diện app. Chứng chỉ HTTPS do Caddy tự cấp, không cần làm gì thêm.

**Đăng nhập Threads/Instagram/Facebook trên server thật (không mở port 6080 ra ngoài):**

`docker-compose.yml` (base) đã bind noVNC vào `127.0.0.1:6080` của chính VPS — không cổng nào cho
noVNC bị publish ra internet cả, kể cả khi chạy overlay prod. Để mở noVNC từ máy cá nhân của bạn:

```bash
ssh -L 6080:127.0.0.1:6080 user@<ip-vps>
```

Giữ session SSH đó mở, rồi vào `http://localhost:6080/vnc.html` **trên chính máy bạn** — traffic đi
qua đường hầm SSH mã hoá, không lộ ra ngoài. Đăng nhập xong đóng SSH tunnel lại là được.

**Cập nhật code trên server** (giống hệt máy dev — xem mục 9.4 phần trên): sửa code, rồi
```bash
docker compose --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml up -d --build app
```
(không cần `--build` cho `db`/`caddy` trừ khi bạn sửa `docker/postgres-init.sql` hoặc `docker/Caddyfile`.)

**Những gì overlay này KHÔNG lo tới** (biết trước để không bất ngờ):

- **Không tách dữ liệu/đăng nhập theo người dùng.** Mọi thành viên team nhìn thấy toàn bộ dữ liệu
  đã crawl, và crawl Threads/Instagram/Facebook dùng chung 1 tài khoản đã đăng nhập trong container.
  Muốn tách riêng cho "nhiều người không liên quan nhau" là một hướng kiến trúc khác hẳn (cần thêm
  đăng nhập người dùng + cô lập session/database theo user) — không nằm trong phạm vi overlay này.
- **Không tự sao lưu database.** `pg_data` là volume Docker bình thường trên VPS; nên có lịch
  `pg_dump` định kỳ ra ngoài container nếu dữ liệu quan trọng — chưa có sẵn trong dự án.
- **Basic Auth là 1 lớp chặn thô**, không phải hệ thống người dùng thật (không phân quyền, không
  audit log ai làm gì). Đủ để ngăn người lạ trên internet, không đủ nếu cần kiểm soát chi tiết trong
  nội bộ team.
