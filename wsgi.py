# -*- coding: utf-8 -*-
"""نقطهٔ ورود اجرای جدی نوبتین (waitress).

    Windows:   set NOBATIN_SECRET=یک-کلید-بلند-تصادفی   &  python wsgi.py
    Linux/mac: export NOBATIN_SECRET=...  ;  python wsgi.py

یا از سرویس‌دهندهٔ فرمانی:
    waitress-serve --host 127.0.0.1 --port 8080 wsgi:app

حالت پیش‌فرض «production» است و عمداً سخت‌گیرانه: بدون NOBATIN_SECRET، با کد مسترِ
ورود روشن (NOBATIN_OTP_MASTER=1)، با فهرست هاست خالی، یا با درگاه آزمایشیِ بی‌اجازه
برنامه بالا نمی‌آید. برای تست محلی همین فایل را با NOBATIN_ENV=development اجرا کنید.

نشانی پیش‌فرض 127.0.0.1 است: این برنامه پشت پروکسی (nginx/caddy) طراحی شده، نه
روستِ اینترنت. روشن‌کردن آن روی 0.0.0.0 یعنی پذیرشِ مستقیمِ ترافیک بدون TLS —
کوکی امن، HSTS و IPِ واقعیِ سقف‌های نرخ همه به پروکسی جلویی وابسته‌اند. اگر واقعاً
بی‌پروکسی اجرا می‌کنید، صریحاً NOBATIN_HOST=0.0.0.0 بگذارید.
"""
import os
import sys

# باید پیش از importِ app تنظیم شود. ماژول app در سطح خودش یک نمونه می‌سازد
# (`app = create_app(...)`)، و اگر NOBATIN_ENV تنظیم نباشد آن نمونه «توسعه» است —
# یعنی seed شدن دادهٔ نمونه در همان دیتابیسِ واقعی و ساختنِ یک اپِ بی‌استفاده.
# با این خط، نمونهٔ سطح-ماژول همان نمونهٔ production می‌شود و create_app آن را
# از کش برمی‌گرداند (یک schema/seed، یک secret، یک لاگ‌فایل).
os.environ.setdefault("NOBATIN_ENV", "production")

try:
    from app import create_app              # noqa: E402

    ENV_NAME = os.environ.get("NOBATIN_ENV", "production")
    app = create_app(env_name=ENV_NAME)
except RuntimeError as exc:
    print(f"\n  نوبتین بالا نیامد: {exc}\n", file=sys.stderr)
    raise SystemExit(2)

application = app          # برای سازگاری با برخی سرورهای WSGI

if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("NOBATIN_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("NOBATIN_PORT") or 8080)
    print(f"\n  نوبتین ({app.config['ENV']}) در حال سرو‌شدن → http://{host}:{port}")
    if host in ("0.0.0.0", "::"):
        print("  هشدار: روی همهٔ نشانی‌ها باز است. بدون پروکسی TLS، کوکی امن و "
              "سقف‌های نرخِ IP و HSTS معنایشان را از دست می‌دهند.")
    print(f"  پیامک: {app.config['SMS_PROVIDER']} | درگاه: {app.config['PAYMENT_PROVIDER']}\n")
    serve(app, host=host, port=port, threads=int(os.environ.get("NOBATIN_THREADS", "8")))
