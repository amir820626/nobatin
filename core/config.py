# -*- coding: utf-8 -*-
"""تنظیمات نوبتین از متغیرهای محیطی.

دو حالت دارد:
  development  → پیش‌فرضِ اجرای `python app.py`؛ کد مستر و درگاه آزمایشی فعال‌اند.
  production   → با `wsgi.py` یا NOBATIN_ENV=production؛ عمداً سخت‌گیرانه (fail-closed)
                 است: بدون کلید رمز بلند، با کد مستر ورود روشن، بی‌فهرست هاست مجاز، یا با
                 درگاه آزمایشیِ بی‌تأیید صریح بالا نمی‌آید (`python app.py --selfcheck`).

همهٔ مقادیر از متغیرهای محیط (یا فایل .env که خودتان export می‌کنید) خوانده می‌شود.
"""
import os

from core.jalali import normalize_mobile

DEV_SECRET = "nobatin-dev-secret-change-me"

# CSP: اسکریپت فقط از خودِ سایت (هیچ <script> درون‌خطی در قالب‌ها نمانده)، هیچ iframe،
# هیچ object/eval، و فرم‌ها فقط به همین مبنا. 'unsafe-inline' فقط برای style لازم است،
# چون رنگ اختصاصی هر کسب‌وکار و پهنای نوارهای مالی به‌صورت متغیر درون‌خطی نوشته می‌شود.
DEFAULT_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
               "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; "
               "object-src 'none'; frame-src 'none'")


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _int(name, default):
    try:
        return int(env(name, str(default)) or default)
    except ValueError:
        return default


def _bool(name, default):
    v = env(name, "").lower()
    if v in ("1", "true", "yes", "on", "ok"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def apply(app, env_name=None):
    """پیکربندی را روی اپلیکیشن Flask می‌نشاند و همان app را برمی‌گرداند."""
    name = (env_name or env("NOBATIN_ENV", "development")).lower()
    prod = name in ("production", "prod")

    secret = env("NOBATIN_SECRET")
    if prod:
        if not secret or secret == DEV_SECRET:
            raise RuntimeError("برای اجرای production متغیر NOBATIN_SECRET (کلید رمز جلسات) را "
                               "مقداری کنید؛ مقدار پیش‌فرض توسعه مجاز نیست.")
        if len(secret) < 32:
            raise RuntimeError(
                "NOBATIN_SECRET کوتاه است (حداقل ۳۲ نویسه لازم است). یک مقدار تصادفی بسازید: "
                'python -c "import secrets;print(secrets.token_urlsafe(48))"')
        if env("NOBATIN_OTP_MASTER", "0") == "1":
            raise RuntimeError("کد مسترِ ورود («۱۲۳۴۵» برای همهٔ حساب‌ها) در production مجاز نیست؛ "
                               "NOBATIN_OTP_MASTER را حذف یا 0 کنید.")
    else:
        secret = secret or DEV_SECRET

    behind_proxy = _bool("NOBATIN_BEHIND_PROXY", False)
    secure_cookie = _bool("NOBATIN_COOKIE_SECURE", prod)     # روی HTTPS پیش‌فرض روشن
    session_hours = _int("NOBATIN_SESSION_HOURS", 12 if prod else 24 * 30)
    # هاست‌های مجاز: جلوی Host-header poisoning (لینک و پیامک با دامنهٔ مهاجم).
    # آدرس‌های لوکال همیشه اضافه می‌شوند تا بررسی سلامت با curl 127.0.0.1 و تست‌های
    # محلی ۴۰۰ نگیرند. چون Flask Config بی‌حساس به بزرگی/کوچکی است، همین کلید
    # `trusted_hosts` خودِ werkzeug هم هست؛ پس دو لایه (guard ما و werkzeug) یک
    # فهرست را می‌بینند و هیچ‌گاه یکی می‌پذیرد و دیگری نه.
    # قاعدهٔ تطابق همان قاعدهٔ werkzeug است: نام کامل، یا اگر با «.» شروع شود همهٔ
    # زیردامنه‌ها («.example.com» یعنی example.com و shop.example.com).
    # IPv6 داخل [] را werkzeug نمی‌پذیرد؛ برای سرور داخلی از localhost/IP استفاده کنید.
    hosts = tuple(x.strip().lower().rstrip(".")
                  for x in env("NOBATIN_TRUSTED_HOSTS", "").split(",") if x.strip())
    if hosts:
        hosts = tuple(dict.fromkeys(hosts + ("localhost", "127.0.0.1")))

    # دو «باخ‌باز» (fail-open) که در تولید بی‌صدا رخ می‌دهند و کاربر متوجه نمی‌شود:
    # ۱) فهرست هاست خالی → گارد Host کلاً خاموش می‌ماند و هر Hostی باور می‌شود
    #    (لینک‌های پیامکی با دامنهٔ مهاجم). ۲) درگاه آزمایشی در تولید → هر کاربر
    #    واردشده می‌تواند «پرداخت موفق» بزند و نوبت رایگان قطعی کند. هر دو باید
    #    جلوی بالا آمدن را بگیرند، نه فقط هشدارِ خودcheck؛ پس صریح رد می‌شوند.
    # برای محیط نمایشی/دموی تولیدی: NOBATIN_ALLOW_SANDBOX=1 (آگاهانه).
    if prod:
        if not hosts:
            raise RuntimeError("در production فهرست هاست‌های مجاز خالی است؛ NOBATIN_TRUSTED_HOSTS "
                               "را به دامنهٔ خود تنظیم کنید (مثلاً example.com,.example.com). "
                               "بدون آن سرور هر سرصفحهٔ Host را باور می‌کند.")
        if env("NOBATIN_PAYMENT_PROVIDER", "sandbox") == "sandbox" and not _bool("NOBATIN_ALLOW_SANDBOX", False):
            raise RuntimeError("درگاه پرداخت روی حالت آزمایشی (sandbox) است: هر کاربر واردشده می‌تواند "
                               "«پرداخت موفق» بزند و نوبت قطعی کند، بدون پول. درگاه واقعی را در "
                               "NOBATIN_PAYMENT_PROVIDER بشناسانید، یا اگر این سرور فقط نمایشی است "
                               "NOBATIN_ALLOW_SANDBOX=1 بگذارید.")

    app.secret_key = secret
    app.config.update(
        ENV=name,
        IS_PRODUCTION=prod,
        # کوکی ورود: HttpOnly (JS نخواند) + Lax (فقط ناوبری هم‌مبنا) + Secure روی HTTPS.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        PERMANENT_SESSION_LIFETIME=3600 * session_hours,
        SESSION_REFRESH_EACH_REQUEST=True,
        # دادهٔ نمونه فقط در توسعه/تست؛ در production با NOBATIN_SEED=1 صریح روشن می‌شود
        SEED_DEMO=_bool("NOBATIN_SEED", not prod),
        ADMIN_MOBILE=normalize_mobile(env("NOBATIN_ADMIN_MOBILE")) if env("NOBATIN_ADMIN_MOBILE") else "",
        ADMIN_NAME=env("NOBATIN_ADMIN_NAME", "مدیر نوبتین"),
        # ورود و پیامک
        OTP_MASTER=(env("NOBATIN_OTP_MASTER", "1" if not prod else "0") == "1"),
        OTP_COOLDOWN=_int("NOBATIN_OTP_COOLDOWN", 60),          # ثانیه فاصلهٔ دو درخواست کد
        OTP_MAX_MOBILE=_int("NOBATIN_OTP_MAX_MOBILE", 5),        # سقف کد هر موبایل در ساعت
        OTP_MAX_IP=_int("NOBATIN_OTP_MAX_IP", 12),               # سقف کد هر IP در ساعت
        OTP_MAX_ATTEMPTS=_int("NOBATIN_OTP_MAX_ATTEMPTS", 12),   # سقف کدهای اشتباه در ۳۰ دقیقه
        SMS_PROVIDER=env("NOBATIN_SMS_PROVIDER", "log"),         # log | kavenegar
        # در production متن پیامکِ کدِ ورود در sms_log ماسکه می‌شود؛ وگرنه هر کسی که
        # به پنل پیامک‌ها یا به دیتابیس دسترسی داشته باشد، کد زنده دارد = حساب شما.
        SMS_MASK_OTP=_bool("NOBATIN_SMS_MASK_OTP", prod),
        # CSRF: هر نوشتن توکن می‌خواهد. استثنا فقط برای بازگشتِ سرور‌به‌سرورِ
        # درگاه واقعی است (فرمِ آن‌سایتی که توکن ما را ندارد). پیش‌فرض: هیچ.
        CSRF_EXEMPT_ENDPOINTS=tuple(x.strip() for x in env("NOBATIN_CSRF_EXEMPT", "").split(",") if x.strip()),
        # اگر پشت nginx/Proxy اجرا می‌کنید: 1 → X-Forwarded-* باور می‌شود
        BEHIND_PROXY=behind_proxy,
        # هاست‌های مجاز: جلوی Host-header poisoning (لینک و پیامک با دامنهٔ مهاجم)
        TRUSTED_HOSTS=hosts,
        # سقف حجم بدنهٔ درخواست (فرم‌های ما چند کیلوبایت‌اند؛ ۱ مگابایت خیلی بیشتر از نیاز است)
        MAX_CONTENT_LENGTH=_int("NOBATIN_MAX_BODY_KB", 1024) * 1024,
        # سقف‌های نرخِ ضد هجوم؛ شمارنده روی دیتابیس است، پس بین workerها هم مشترک می‌ماند
        LIMIT_HOLDS_USER=_int("NOBATIN_LIMIT_HOLDS_PER_HOUR", 120),    # قفل ساعت/کاربر
        LIMIT_BOOKS_USER=_int("NOBATIN_LIMIT_BOOKINGS_PER_HOUR", 30),  # ساخت نوبت/کاربر/ساعت
        LIMIT_BIZ_IP=_int("NOBATIN_LIMIT_BIZ_PER_DAY", 10),            # ثبت کسب‌وکار/IP/روز
        LIMIT_WRITES_IP=_int("NOBATIN_LIMIT_WRITES_PER_MIN", 90),      # نوشتن/IP/دقیقه
        # پرداخت
        PAYMENT_PROVIDER=env("NOBATIN_PAYMENT_PROVIDER", "sandbox"),   # sandbox | idpay|zarinpal
        # کارهای زمان‌بندی‌شده
        REMINDER_INTERVAL=_int("NOBATIN_REMINDER_MINUTES", 10),
        # جاروی داخل هر درخواست حداکثر هر N ثانیه (وگرنه هر بازدید یک نوشتن است)
        HOUSEKEEPING_SECONDS=_int("NOBATIN_HOUSEKEEPING_SECONDS", 20),
        # کش دادهٔ تقریباً ثابت (دسته/استان/شهر/تنظیمات نمایشی)؛ ۰ = خاموش
        CACHE_TTL=_int("NOBATIN_CACHE_TTL", 15),
        # هدرد امنیتی
        HSTS_SECONDS=_int("NOBATIN_HSTS_SECONDS", 31536000 if prod else 0),
        CONTENT_SECURITY_POLICY=env("NOBATIN_CSP") or DEFAULT_CSP,
        # سلامت: پیش‌فرض فقط از loopback؛ با توکن از بیرون هم بررسی‌پذیر است
        HEALTHZ_ENABLED=_bool("NOBATIN_HEALTHZ", True),
        HEALTHZ_TOKEN=env("NOBATIN_HEALTHZ_TOKEN"),
        HEALTHZ_ALLOW=tuple(x.strip() for x in
                            env("NOBATIN_HEALTHZ_ALLOW", "127.0.0.1,::1").split(",") if x.strip()),
        # لاگ: در production روی فایل چرخشی (در کنار systemd/journal هم بی‌خطر)
        LOG_FILE=env("NOBATIN_LOG_FILE"),
        LOG_LEVEL=env("NOBATIN_LOG_LEVEL", "INFO" if prod else "WARNING").upper(),
        # استاتیک: کش طولانی‌مدت در production (آدرس فایل نسخه‌دار می‌شود، پس بی‌خطر)
        SEND_FILE_MAX_AGE_DEFAULT=_int("NOBATIN_STATIC_MAX_AGE", 60 if not prod else 604800),
    )
    app.json.ensure_ascii = False
    return app
