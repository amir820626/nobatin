# نوبتین — بستهٔ بردن روی سرور (Linux)

سه چیز لازم است: **یک پروسهٔ WSGI** (gunicorn) که از systemd بالا بیاید، **nginx** که
TLS و فایل‌های ایستا و سقف حجم را جلویش بگیرد، و **چهار timer** که کارهای زمان‌دار
(انقضا، یادآوری، نگهداری، پشتیبان) را بدون cron دستی اجرا کنند.

> روی Windows هیچ‌کدام از این فایل‌ها لازم نیست: همان `python wsgi.py` (waitress) را در
> Task Scheduler به‌عنوان سرویس ثبت کنید. فایل‌های این پوشه برای سرور لینوکسی‌اند.
> توجه: `wsgi.py` پیش‌فرض روی همان قفل‌های `production` است (کلید، دامنه، درگاه)؛ برای
> اجرای محلی `NOBATIN_ENV=development` بگذارید. مسیر دیگر (`python app.py`) فقط برای
> توسعه است و روی `127.0.0.1:5077` می‌نشیند.

## ۰) یک‌بار، روی سرور

```bash
# 1. کد + محیط مجازی
sudo useradd --system --create-home --home-dir /srv/nobatin --shell /usr/sbin/nologin nobatin
sudo mkdir -p /srv/nobatin && sudo chown nobatin:nobatin /srv/nobatin
# فایل‌های پروژه را داخل /srv/nobatin بگذارید (git clone یا rsync)
sudo -u nobatin python3 -m venv /srv/nobatin/.venv
sudo -u nobatin /srv/nobatin/.venv/bin/pip install -r /srv/nobatin/requirements.txt \
    -r /srv/nobatin/deploy/requirements-server.txt

# 2. تنظیمات (کلیدها هیچ‌وقت در git نمی‌روند)
sudo -u nobatin cp /srv/nobatin/.env.example /srv/nobatin/.env
sudo -u nobatin chmod 600 /srv/nobatin/.env
sudo nano /srv/nobatin/.env        # NOBATIN_SECRET + دامنهٔ واقعی + پیامک/درگاه

# 3. پایگاه داده و آمادگی
sudo -u nobatin /srv/nobatin/.venv/bin/python /srv/nobatin/app.py --selfcheck
```

`--selfcheck` تا موارد ❌ دارند نگویید «بریم»؛ همان‌ها همان‌اند که روی سرور
فاجعه می‌سازند (کلید پیش‌فرض، کد مستر روشن، HSTS خاموش، ایندکس جاافتاده).
⚠ها اختیاری‌اند ولی دو تای آن‌ها روی سرور معنا دارند: `hosts` و `hsts` — اگر
در `.env` پرشان نکنید، برنامه در production اصلاً بالا نمی‌آید.

## ۱) فایل تنظیمات

`.env` را از `deploy/nobatin.env.template` کپی کنید و این سه مقدار را حتماً عوض کنید:

| متغیر | چرا مهم است |
|---|---|
| `NOBATIN_SECRET` | رمز کوکی نشست. بلند و تصادفی: `python -c "import secrets;print(secrets.token_urlsafe(48))"` — کوتاه‌تر از ۳۲ نویسه = برنامه بالا نمی‌آید |
| `NOBATIN_TRUSTED_HOSTS` | دامنهٔ واقعی (با www). خالی بگذارید = در production رد می‌شود؛ بی آن هر سرصفحهٔ Host باور می‌شود و پیوندهای پیامک قابل جعل‌اند. تطابق دقیق است؛ «`.example.com`» خودِ دامنه و همهٔ زیردامنه‌ها را می‌پذیرد. `localhost` و `127.0.0.1` خودکار مجازند تا `curl` سلامت‌سنجی کار کند. سرصفحهٔ عبور از پروکسی (`X-Forwarded-Host`) هم با همین فهرست سنجیده می‌شود |
| `NOBATIN_ADMIN_MOBILE` | اولین حساب مدیر؛ بدون آن پنل `/modiriat` هیچ صاحبی ندارد |

دو «ردِ بی‌صدا»ی دیگر که در production جلوی بالا آمدن را می‌گیرند — خسارتشان خاموش است،
آگاهانه نیست):

| تنظیم | چه کسی رد می‌کند | چرا |
|---|---|---|
| `NOBATIN_OTP_MASTER=1` | همیشه | کد ثابت «۱۲۳۴۵» = ورود هر کسی به هر حسابی |
| `NOBATIN_PAYMENT_PROVIDER=sandbox` | بی `NOBATIN_ALLOW_SANDBOX=1` | درگاه آزمایشی یعنی هر کاربر واردشده می‌تواند «پرداخت موفق» بزند و نوبت رایگان قطعی کند |
| `NOBATIN_SEED=1` با دادهٔ نمونه | فقط هشدار (`--selfcheck`) | حساب‌ها و کسب‌وکارهای نمونه روی دادهٔ واقعی |

پیش‌فرض‌های امنِ خودکار در production: `SESSION_COOKIE_SECURE=1`، `HSTS_SECONDS=۳۱۵۳۶۰۰۰`،
و **`NOBATIN_SMS_MASK_OTP=1`** — یعنی رقم‌های کد ورود در جدول `sms_log` و در پنل
«پیامک‌ها» ذخیره/نمایش نمی‌شوند (به اپراتور همان متن کامل می‌رود). بی این خط، هر کسی
که دسترسی به پنل پیامک‌ها یا یک نسخهٔ پشتیبان پیدا کند، کدِ زندهٔ ورود دارد. اگر
عمداً می‌خواهید خاموشش کنید `NOBATIN_SMS_MASK_OTP=0`.

## ۲) سرویس و timerها

```bash
sudo cp deploy/nobatin.service      /etc/systemd/system/
sudo cp deploy/timers/*.service     /etc/systemd/system/
sudo cp deploy/timers/*.timer       /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nobatin nobatin-expiry.timer nobatin-reminders.timer \
                            nobatin-prune.timer nobatin-backup.timer
systemctl status nobatin --no-pager
journalctl -u nobatin -f
```

`nobatin.service` با `EnvironmentFile=/srv/nobatin/.env` کار می‌کند، پس اگر مسیر
پروژه‌تان چیز دیگری است، واحدها را ویرایش کنید (grep `/srv/nobatin`).

واحد سرویس علاوه بر کاربرِ بدونِ root و `PrivateTmp`، سقف‌های هسته‌ای هم دارد
(`ProtectKernelModules`، `ProtectKernelTunables`، `RestrictAddressFamilies=AF_INET
AF_INET6 AF_UNIX`، `RestrictSUIDSGID`، `LockPersonality`)؛ یعنی حتی اگر روزی کد
اجرای دلخواه بگیرد، نتواند مدول بارگذاری کند، سوکت خام بسازد یا به `/proc` بنویسد.
`LimitNOFILE=8192` هم برای سه worker × چهار ترد لازم است.

## ۳) nginx

```bash
sudo cp deploy/nginx-nobatin.conf /etc/nginx/sites-available/nobatin
sudo ln -s /etc/nginx/sites-available/nobatin /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d example.com -d www.example.com
```

کنارِ پروکسی کردن، gzip، کش هفت‌روزهٔ فایل‌های ایستا، سقف `client_max_body_size 1m`
و بستن `/healthz` رو به بیرون هم همان‌جا تنظیم شده است.

دو نکتهٔ عمدی در این فایل:

* `set_real_ip_from` / `real_ip_header` **نوشته نشده است**. آن تنظیم مقدار
  `X-Forwarded-For` را جای `remote_addr` می‌نشاند و گاردِ `/healthz` (که فقط
  `127.0.0.1` را می‌پذیرد) دور زده می‌شود. برنامه خودش با `ProxyFix` فقط
  *آخرین* مقدار همان سرصفحه را — یعنی همان IPِ واقعی که nginx افزوده است — می‌خواند.
* `proxy_set_header X-Forwarded-Prefix "";` لازم است: بی آن، سرصفحهٔ ارسالیِ خودِ
  مرورگر در `url_for` اثر می‌گذارد و پیوندها (و لینکِ پیامکی) به مسیر جعلی می‌روند.
* `/healthz` با `allow 127.0.0.1; deny all;` و `proxy_set_header Host localhost`
  بسته می‌شود تا حتی با دامنهٔ جعلی هم از بیرون باز نشود.

## ۴) پشتیبان و بازگردانی

```bash
bash deploy/backup.sh            # یک نسخهٔ سالم + متن WAL در /var/backups/nobatin
bash deploy/backup.sh --keep 60  # نگه‌داشتن ۶۰ روز (پیش‌فرض ۱۴)
bash deploy/restore.sh /var/backups/nobatin/nobatin-2026-10-16-0310.db --dry-run
```

`backup.sh` از API پایگاه دادهٔ SQLite برای «آنلاین بکاپ» استفاده می‌کند (نه کپی فایل)،
پس دیتابیس در میانهٔ نوشتن تکه‌تکه نمی‌شود؛ سپس با `PRAGMA integrity_check` سلامت
نسخه را تأیید می‌کند و در صورت خرابی با کد خروجی ۱ رد می‌شود.

## ۵) راستی‌آزمایی پس از بالا آمدن

```bash
curl -s http://127.0.0.1:8000/healthz                        # از داخل سرور
curl -s -H "X-Health-Token: ..." http://127.0.0.1:8000/healthz   # از بیرون، با توکن
curl -Is https://example.com/ | grep -i 'content-security-policy\|strict-transport'
python tools/bench.py --base http://127.0.0.1:8000 --threads 16 --seconds 30
```

توکن سلامت را در **سرصفحه** بفرستید (`X-Health-Token`)، نه در `?token=`؛ مسیرِ کوئری
در لاگِ nginx و در تاریخچهٔ مرورگر می‌نشیند. (پشتیبانی از `?token=` همچنان هست، فقط
پیشنهاد نمی‌کنیم.)

همین فهرست روی این ماشین با اجرای واقعیِ production (`wsgi.py` + دیتابیسِ جدا) آزموده
شد و این‌ها دیده شد؛ روی سرور خودتان هم باید همین باشد:

* `Set-Cookie: session=…; Secure; HttpOnly; SameSite=Lax` — هر سه صفت روی کوکی نشست.
* `Strict-Transport-Security: max-age=31536000; includeSubDomains` و سرصفحهٔ CSP.
* `Host: evil.com` → پاسخ ۴۰۰ (نه ریدیرکت، نه صفحهٔ کاربری).
* `/healthz` از loopback پاسخ JSON می‌دهد؛ با `X-Forwarded-For: 127.0.0.1` جعلی از
  بیرون ۴۰۴ می‌ماند، چون معیار، نشانیِ سوکت است نه سرصفحه.
* `/sms-box` و پنل‌ها برای کاربرِ ناشناس به `/vorood?next=…` می‌روند (پیوندِ داخلی).
* بدون `NOBATIN_SECRET` یا با فهرستِ هاستِ خالی، برنامه بالا نمی‌آید و دلیل را
  فارسی روی stderr می‌نویسد.

عددِ مرجع روی همین ماشین (سرور تک‌پروسهٔ توسعه، پایگاه دادهٔ نمونه، بدون خطا و بدون
پاسخ ۵xx). هر سنجه دو بار اجرا شده و بازهٔ دیده‌شده نوشته می‌شود — روی ماشین شلوغ
چند درصد پایین‌تر هم طبیعی است:

| سنجه | ترد | نرخ | p95 |
|---|---|---|---|
| صفحه‌های عمومی (`/`، جست‌وجو، درباره، قوانین) | ۸ | ۱۳۹–۱۶۴ RPS | ۱۳۶–۱۶۹ ms |
| پنل واردشده (`/pankack`، `/profile`، `/`) | ۸ | ۸۹–۹۸ RPS | ۱۳۷–۱۶۵ ms |
| پنل کسب‌وکار با ۱۶ ترد (`/kasbkar`، `/profile`، `/`) | ۱۶ | ۱۰۸–۱۱۶ RPS | ۲۹۷–۳۱۲ ms |
| صفحهٔ خانه + جست‌وجو (۳۰ مهر ۱۴۰۵، ۱۶ ترد / ۲۰ ثانیه) | ۱۶ | ۱۱۵ RPS | ۲۱۴ ms (p99 = ۲۵۲) |
| همان‌ها با ۳۲ ترد / ۱۵ ثانیه | ۳۲ | ۱۰۳ RPS | ۴۶۷ ms (p99 = ۵۰۲) |
| اجرای production با waitress (۴ تردِ سرویس‌دهنده، دیتابیسِ خالی) | ۱۶ | ۳۲۹ RPS | ۷۴ ms (p99 = ۸۷) |

سطرِ آخر با `python wsgi.py` در حالت production گرفته شده و دیتابیسش خالی بوده، پس
صفحه‌ها ارزان‌تر از واقعیت‌اند؛ برای عددِ محافظه‌کارانه همان ردیف‌های بالاتر (با دادهٔ
نمونه) را ملاک قرار دهید. نکتهٔ عملیِ همان سنجه: `wsgi.py` سرویس‌دهندهٔ waitress را با
`NOBATIN_THREADS` (پیش‌فرض ۸) اجرا می‌کند، و ۱۶ مشتریِ هم‌زمان روی ۴ ترد، هشدارِ
`waitress.queue: Task queue depth is N` در لاگ می‌سازد — یعنی درخواست‌ها در صف می‌مانند.
همین سنجه روی این ماشین با سه عددِ ترد اجرا شد: ۴ ترد → ۳۲۹ RPS و p95 = ۷۴ms،
۸ ترد → ۳۷۲ RPS و p95 = ۸۴ms، ۱۶ ترد → ۳۶۶ RPS و p95 = ۱۲۳ms (۱۶ مشتریِ هم‌زمان در
هر سه حالت؛ صفر خطا). یعنی تردِ بیشتر صف را کوتاه می‌کند و هشدارِ لاگ را کم می‌کند،
ولی بعد از یک نقطه فقط تأخیر را بالا می‌برد. پس پیش‌فرضِ ۸ را نگه دارید و برای ظرفیتِ
بیشتر در لینوکس worker اضافه کنید (gunicorn)، نه ترد.

یعنی حتی محافظه‌کارانه (نصفِ عدد، چون TLS، nginx و دادهٔ واقعیِ بیشتر همه را کند
می‌کنند) چند ده‌هزار بازدید صفحه در ساعت روی یک پروسه جا می‌شود — و این عدد بدون
gunicorn و بدون TLS گرفته شده (nginx جلوی آن را می‌گیرد). با `--workers 3` تعداد
صفحه‌های خواندنی تقریباً سه برابر می‌شود؛ مگر در نوشتن‌ها، چون SQLite یک‌نویسنده
هم‌زمان دارد — همان‌جا `busy_timeout`، retries و `BEGIN IMMEDIATE` وارد می‌شوند و
درستیِ «یک ساعت، یک برنده» تضمین می‌ماند (بخش ۳۳ و ۳۵ آزمون).

## ۶) سطح‌بندی: چند کاربر هم‌زمان؟

| چه چیزی | چه زمانی bottleneck می‌شود | چه کار کنیم |
|---|---|---|
| دیدن صفحه‌ها | تقریباً دیر (کش ۱۵ ثانیهٔ دسته/شهر + ایندکس‌ها) | workerها را زیاد کنید: `--workers 3` |
| رزرو/لغو/پرداخت | نوشتن‌ها روی یک فایل SQLite قفل می‌شوند | `--expiry` را به timer بسپارید تا جاروب از مسیر درخواست برود؛ سقف‌های `LIMIT_*` را نگه دارید |
| پیامک | ارسال بیرونی | در نوبتین پس از commit می‌رود (`after_commit`)، پس قفل دیتابیس هنگام HTTP در دست نیست |
| دادهٔ بیشتر از ~۱۰ برابر | فایل SQLite و کش درون‌روشی | کش را به Redis و دیتابیس را به PostgreSQL ببرید (لایهٔ db در `core/db.py` جداست) |

تا هزاران نوبت در روز، همین SQLite با WAL و ایندکس‌های `core/db.py` کافی است؛
نسخهٔ اول عمداً همین را انتخاب کرده تا روی یک سرور ارزان هم بی‌موقعیت اجرا شود.

## ۷) به‌روزرسانی نسخه

```bash
cd /srv/nobatin && git pull
sudo systemctl restart nobatin         # template/socket دوباره بارگذاری می‌شوند
sudo -u nobatin /srv/nobatin/.venv/bin/python app.py --selfcheck
python smoke_test.py                   # ۵۱۷ بررسی، ۳۵ بخش؛ باید «پاس شد» بدهد
```

schema خودکار در اولین اجرا به‌روز می‌شود (`D.init_schema`)؛ ولی پیش از هر کاری
یک نسخه بگیرید (`bash deploy/backup.sh`).
