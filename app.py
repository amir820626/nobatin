# -*- coding: utf-8 -*-
"""نوبتین — سامانهٔ نوبت‌دهی آنلاین (Flask + SQLite).

اجرای توسعه:  python app.py            →  http://127.0.0.1:5077
اجرای سرور:   gunicorn 'app:create_app()'   (یا python wsgi.py با waitress)

کنسول:
  python app.py --selfcheck        بررسی آمادگی سرور (کلید، کوکی، WAL، ایندکس، …)
  python app.py --prune            کارهای نگهداری: قدیمی‌ها، ماسک OTP، checkpoint
  python app.py --send-reminders   ارسال یادآوری‌های معوق (برای cron/timer)
  python app.py --expiry           آزادسازی سبدها/نوبت‌های بی‌زمان (برای cron/timer)
  python app.py --reset            ساخت دوبارهٔ دادهٔ نمونه (دیتابیس فعلی پاک می‌شود)
"""
import os
import re
import json
import time
import shutil
import secrets
from html import escape as html_escape
from datetime import datetime, date

from flask import (Flask, Response, make_response, render_template, request, session, redirect,
                   url_for, flash, abort, g, current_app, jsonify)

from core import db as D
from core import booking as B
from core import cache as CACHE
from core import config as C
from core import csrf as CSRF
from core.helpers import current_user, my_business, login_user, logout_user, require_login
from core.jalali import (fa, fa_money, fa_date, fa_datetime, minutes_label, month_grid,
                         upcoming_days, jalali_to_date, to_jdate, JALALI_MONTHS,
                         evaluate_cancellation, rule_label, normalize_mobile)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STARTED = time.time()

# جاروی سبدها/نوبت‌های بی‌زمان لازم نیست در «هر» درخواست باشد؛ هر worker حداکثر
# هر N ثانیه یک بار انجامش می‌دهد (کار idempotent است، پس تکرار هم بی‌ضرر است).
_last_hk = [0.0]

_APPS = {}    # نمونهٔ ساخته‌شده به ازای هر حالت (development/production)


def create_app(reset=False, env_name=None):
    """یک نمونهٔ اپ برای این حالت اجرا می‌سازد (و همان را دوباره برمی‌گرداند).

    کشِ نمونه‌ها برای این است که `gunicorn 'app:create_app()'` همان اپی را بگیرد که
    موقع import ماژول ساخته شده — دو بار schema/seed در یک پروسه هم وقت می‌گیرد و هم
    دو کوکی‌کلید/لاگ جدا می‌سازد. با --reset یا حالت تازه، نمونهٔ تازه ساخته می‌شود.
    """
    key = (env_name or os.environ.get("NOBATIN_ENV", "development")).lower()
    if not reset and key in _APPS:
        return _APPS[key]
    app = Flask(__name__, template_folder=os.path.join(APP_ROOT, "templates"),
                static_folder=os.path.join(APP_ROOT, "static"))
    C.apply(app, env_name)
    app.json.ensure_ascii = False
    # ترتیب مهم است: گارد Host داخل ProxyFix می‌نشیند تا هدرِ بازنویسی‌شده هم سنجیده شود
    app.wsgi_app = host_gate(app, app.wsgi_app)
    apply_proxy(app)
    # بیرونی‌ترین لایه: IPِ سوکت را پیش از بازنویسیِ ProxyFix نگه می‌دارد (/healthz)
    app.wsgi_app = peer_stamp(app.wsgi_app)
    setup_logging(app)

    register(app)

    with app.app_context():
        if reset and os.path.exists(D.db_path()):
            os.remove(D.db_path())
        D.init_schema()
        D.seed()
    _APPS[key] = app
    return app


def setup_logging(app):
    """لاگ روی فایل چرخشی (۵×۴ مگابایت) + stdout، تا journal/systemd هم ببیندش."""
    import logging
    from logging.handlers import RotatingFileHandler

    level = getattr(logging, str(app.config.get("LOG_LEVEL") or "WARNING").upper(), logging.WARNING)
    app.logger.setLevel(level)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
    path = app.config.get("LOG_FILE")
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            h = RotatingFileHandler(path, maxBytes=4 * 1024 * 1024, backupCount=5,
                                    encoding="utf-8")
            h.setFormatter(fmt)
            app.logger.addHandler(h)
        except OSError as exc:
            app.logger.warning("logfile unusable (%s); falling back to stderr", exc)
    if not any(isinstance(h, logging.StreamHandler) and getattr(h, "_nobatin", False)
               for h in app.logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh._nobatin = True
        app.logger.addHandler(sh)
    # خطاهای werkzeug هم در همان فایل بنشیند
    logging.getLogger("werkzeug").setLevel(max(level, logging.WARNING))
    return app



def apply_proxy(app):
    """پشت nginx/Proxy: IP و http/https واقعی از سرصفحه‌های X-Forwarded-* خوانده شود.

    محدودیت‌های نرخ ورود بر پایهٔ IP و کوکی Secure بر پایهٔ پروتکل‌اند، پس بدون این
    لایه همهٔ درخواست‌ها از IP خودِ پروکسی می‌آیند و سقف‌ها بی‌معنی می‌شوند.
    با NOBATIN_BEHIND_PROXY=1 روشن می‌شود و باید دقیقاً یک پروکسی جلوی اپ باشد.
    """
    if app.config.get("BEHIND_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    return app


def peer_stamp(inner):
    """IP واقعیِ همتای TCP را پیش از دست‌کاری ProxyFix در environ می‌نویسد.

    چرا لازم است: `ProxyFix(x_for=1)` مقدار `REMOTE_ADDR` را از نخستین عضو
    X-Forwarded-For می‌سازد، و آن سرصفحه را فرستنده می‌نویسد. اگر `/healthz` و
    سقف‌های نرخِ ورودی همان را باور کنند، هر کسی با `X-Forwarded-For: 127.0.0.1`
    هم سلامت سایت را می‌خواند و هم شمارنده‌های OTP/رزرو را دور می‌زند. این لایه
    بیرونی‌ترین است، پس عددِ درستِ سوکت را قبل از بازنویسی برمی‌دارد.
    """
    def wsgi_app(environ, start_response):
        environ["nobatin.peer_addr"] = environ.get("REMOTE_ADDR") or ""
        return inner(environ, start_response)
    return wsgi_app


def auto_reminders():
    """یادآوری خودکار، حداکثر هر N دقیقه — بدون نیاز به cron بیرونی."""
    mins = int(current_app.config.get("REMINDER_INTERVAL", 10) or 0)
    if mins <= 0:
        return
    key = "last_reminder_run"
    try:
        prev = datetime.strptime(D.setting(key, "") or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        prev = None
    if prev and (datetime.now() - prev).total_seconds() < mins * 60:
        return
    D.set_setting(key, D.now_str())          # اول مُهر می‌زنیم تا دو بار اجرا نشود
    try:
        n = B.send_reminders()
        if n:
            current_app.logger.info("یادآوری خودکار: %s پیامک ارسال شد", n)
    except Exception as exc:
        current_app.logger.warning("reminder job failed: %s", exc)


def janitor(force=False):
    """کارهای پس‌زمینه: آزادکردن سبدها و نوبت‌های بی‌زمان، یادآوری‌های معوق.

    در حالت عادی حداکثر هر HOUSEKEEPING_SECONDS اجرا می‌شود؛ با timer/cron می‌توان
    `python app.py --expiry` را جدا صدا زد و این را فقط برای کاربران فعال نگه داشت.
    """
    gap = int(current_app.config.get("HOUSEKEEPING_SECONDS", 20) or 0)
    now = time.monotonic()
    if not force and gap and (now - _last_hk[0]) < gap:
        return False
    _last_hk[0] = now
    try:
        B.expire_locks()
        B.expire_pending_appointments()
        D.reap_stuck_sms()
        auto_reminders()
    except Exception as exc:                  # جاروی شکسته‌خورده نباید صفحه را خراب کند
        current_app.logger.warning("janitor failed: %s", exc)
    return True



_ASSETS = {}


def asset_url(filename):
    """پیوند فایل ایستا با اثر انگشت محتوا (?v=…).

    در سرور کش مرورگر یک‌هفته‌ای است؛ بدون این اثر انگشت، کاربر بعد از هر به‌روزرسانی
    CSS/JS همان نسخهٔ کهنه را نگه می‌داشت. تغییر محتوا → نشانی تازه، بدون تغییر نام فایل.
    """
    from flask import url_for
    path = os.path.join(APP_ROOT, "static", filename)
    try:
        st = os.stat(path)
        tag = "%x-%x" % (int(st.st_mtime), st.st_size)
    except OSError:
        tag = "0"
    if _ASSETS.get(filename) != tag:
        _ASSETS[filename] = tag
    return url_for("static", filename=filename, v=tag)


HOST_VALUE_RE = re.compile(r"[a-z0-9._-]*[a-z0-9-]\Z")


def trusted_host(raw, allowed):
    """مقدار Host را بررسی و نرمال می‌کند؛ مقدارِ درست یا None برمی‌گرداند.

    قاعدهٔ تطابق عمداً همان قاعدهٔ werkzeug است، چون Flask Config بی‌حساس به
    بزرگی/کوچکی، کلید TRUSTED_HOSTS را به `trusted_hosts` آن هم می‌دهد: اگر ما
    آسان‌تر بگیریم، werkzeug بعداً در `url_for` / `request.host` با SecurityError
    برخورد می‌کند و خودِ صفحهٔ خطا هم می‌ترکد. پس:
      • «example.com»  فقط همان نام؛
      • «.example.com» آن نام و همهٔ زیردامنه‌ها؛
      • نقطهٔ انتهایی و حروف بزرگ پاک می‌شوند (DNS بی‌تفاوت است، werkzeug نیست)؛
      • پورت باید عددِ ۱ تا ۶۵۵۳۵ باشد؛
      • IPv6 داخل [] را werkzeug نمی‌تواند بپذیرد، پس ما هم رد می‌کنیم.
    """
    s = (raw or "").strip().lower().rstrip(".")
    if ":" in s:
        name, _, port = s.rpartition(":")
    else:
        name, port = s, ""
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return None
    name = name.rstrip(".")
    if not HOST_VALUE_RE.match(name or ""):
        return None
    for a in allowed:
        a = (a or "").strip().lower()
        if a.startswith("."):
            if name == a[1:] or name.endswith(a):
                return name + ((":" + port) if port else "")
        elif name == a:
            return name + ((":" + port) if port else "")
    return None


def plain_error(code, title, text):
    """پاسخ خطای بدون قالب.

    تنها راهِ بی‌خطر وقتی است که خودِ هدر Host جعلی/بدشکل باشد: در آن وضعیت
    `url_for` (که base.html برای لوگو و CSS صدا می‌زند) می‌تواند بترکد و صفحهٔ خطا
    خودش ۵۰۰ شود. این نسخهٔ کوچک هیچ پیوندی نمی‌سازد.

    متن‌ها escape می‌شوند: امروز عنوان/توضیح از خودِ werkzeug می‌آید، ولی یک
    `str(exception)` با نام فیلدِ درخواست در میانش، مستقیم در HTML می‌نشست.
    """
    esc = html_escape
    body = ("<!doctype html><html dir=rtl lang=fa><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><body style=\"font:16px/1.8 system-ui;padding:2rem;color:#1b2330\">"
            "<h2 style='font-size:1.2rem'>%s — %s</h2><p style='max-width:44rem'>%s</p>"
            "</body></html>" % (esc(str(title)), code, esc(str(title)), esc(str(text))))
    # سرصفحه‌های امنیتی خودکار در لایهٔ request اضافه می‌شوند؛ این پاسخ از آن لایه
    # می‌گذرد (گارد Host پیش از Flask پاسخ می‌دهد)، پس همین سرصفحه‌ها را دستی می‌گذاریم.
    return Response(body, code, {"Content-Type": "text/html; charset=utf-8",
                                 "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": C.DEFAULT_CSP,
                                 "Server": "nobatin"})


def error_page(code, title, text):
    """صفحهٔ خطای قالب‌دار؛ اگر رندر نشد (هاست بد/نشست شکسته) همان پیام ساده.

    همیشه یک Response برمی‌گرداند تا فراخوان بتواند سرصفحهٔ خودش را اضافه کند
    (مثل Retry-After برای ۴۲۹).
    """
    try:
        return make_response((render_template("error.html", code=code, title=title, text=text), code))
    except Exception:
        current_app.logger.exception("خطای %s نتوانست قالب را رندر کند", code)
        return plain_error(code, title, text)


def host_gate(app, inner):
    """لایهٔ WSGI: هدر Host را بررسی و نرمال می‌کند — پیش از ساخته‌شدن زمینهٔ درخواست.

    چرا اینجا و نه `before_request`؟ Flask همان‌جا که Request را می‌سازد،
    `trusted_hosts` خودِ werkzeug را هم اعمال می‌کند. اگر مقدار خام بدشکل باشد
    (حروف بزرگ، نقطهٔ انتهایی، پورت نامعتبر) زودتر از هر قلاب ما SecurityError
    بلند می‌شود و صفحهٔ خطا هم به‌خاطر همان هدر می‌ترکد. پس در پایین‌ترین لایه،
    مقدار را به شکلِ درستِ یکسان درمی‌آوریم؛ هر دو لایه یکی را می‌بینند.

    چون این لایه *داخل* ProxyFix می‌نشیند، مقدارِ X-Forwarded-Host هم (که پروکسی
    به Host تبدیل می‌کند) پیش از پذیرش با فهرست مورد اعتماد سنجیده می‌شود —
    یعنی جعل هدر از راه هدرِ_forward هم ۴۰۰ می‌گیرد.
    """
    def wsgi_app(environ, start_response):
        allowed = app.config.get("TRUSTED_HOSTS") or ()
        # فهرست خالی یعنی «بی‌گارد»؛ از این به بعد فقط در توسعه رخ می‌دهد، چون
        # core/config.py در production با فهرست خالی اجازهٔ بالا آمدن نمی‌دهد.
        if allowed:
            raw = environ.get("HTTP_HOST") or ""
            good = trusted_host(raw, allowed)
            if good is None:
                app.logger.warning("rejected Host header: %r", raw)
                return plain_error(
                    400, "درخواست نامعتبر",
                    "میزبان این درخواست شناخته‌شده نیست. اگر با نشانی مستقیم یا IP "
                    "وارد شده‌اید، از دامنهٔ اصلی استفاده کنید.")(environ, start_response)
            if good != raw:
                environ["HTTP_HOST"] = good
        return inner(environ, start_response)

    return wsgi_app


def register(app):
    from routes import public, auth, booking, gateway, customer, business, admin
    app.register_blueprint(public.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(booking.bp)
    app.register_blueprint(gateway.bp)
    app.register_blueprint(customer.bp)
    app.register_blueprint(business.bp)
    app.register_blueprint(admin.bp)

    # ---------------- فیلترها و متغیرهای سراسری قالب
    f = app.jinja_env.filters
    f["fa"] = fa
    f["money"] = fa_money
    f["jdate"] = fa_date
    f["jdatetime"] = fa_datetime
    f["dur"] = minutes_label
    f["jmonth"] = lambda m: JALALI_MONTHS[int(m) - 1]
    f["jd"] = to_jdate
    f["policy_label"] = rule_label
    f["min_hr_label"] = rule_label
    f["human_hours_label"] = human_hours
    f["safe_json"] = lambda s: (json.loads(s or "[]") if isinstance(s, str) else (s or []))
    app.jinja_env.globals.update(grid=month_grid, strip=upcoming_days, jd=to_jdate, fa=fa,
                                 money=fa_money, dur=minutes_label,
                                 jtoday=lambda: to_jdate(date.today()),
                                 now=datetime.now, appt_status_label=STATUS_LABELS,
                                 pay_status_label=PAY_LABELS, tx_label=TX_LABELS,
                                 tx_kind=TX_KINDS,
                                 csrf=CSRF.field,
                                 hours_label=human_hours,
                                 asset=asset_url)

    @app.context_processor
    def inject():
        u = current_user()
        # فهرست دسته/استان/شهر در هر درخواست لازم است (نوار بالا و فوتر). کش کوتاه
        # جلوی چند کوئری تکراری به ازای هر بازدید را می‌گیرد؛ نوشتن روی همان جدول‌ها
        # کش را بی‌اعتبار می‌کند، پس مدیر تغییر را بلافاصله می‌بیند.
        ttl = int(app.config.get("CACHE_TTL", 15) or 0)

        def cached(key, sql, args=()):
            if ttl <= 0:
                return D.query_d(sql, args)
            return CACHE.remember(key, lambda: D.query_d(sql, args),
                                  tables=("categories", "provinces", "cities", "districts"))

        biz = my_business(u) if u and u["role"] in ("business", "admin") else None
        return {
            "user": u, "my_biz": biz,
            "site_name": D.setting_cached("site_name", "نوبتین"),
            "support_phone": D.setting_cached("support_phone", ""),
            "fee_percent": D.setting_cached("platform_fee_percent", "5"),
            "today_iso": date.today().isoformat(),
            "jtoday": to_jdate(date.today()),
            "nav_cats": cached("nav_cats", "SELECT * FROM categories WHERE is_active=1 ORDER BY sort"),
            "all_provinces": cached("all_provinces", "SELECT * FROM provinces ORDER BY name"),
            "all_cities": cached("all_cities",
                                 "SELECT c.*, p.name AS prov FROM cities c "
                                 "JOIN provinces p ON p.id=c.province_id ORDER BY p.name, c.name"),
            "open_locks": (D.query_d("SELECT * FROM slot_locks WHERE holder_user_id=? AND status='hold' "
                                      "AND expires_at>?", (u["id"], D.now_str())) if u else []),
        }

    @app.before_request
    def guard_csrf():
        """هر نوشتن باید توکن نشست را بیاورد؛ پیش از هر جاروب پایگاه داده بررسی می‌شود.

        endpointهای ثبت‌شده در config['CSRF_EXEMPT_ENDPOINTS'] استثنایند — تنها جای
        که باید استفاده شود: بازگشت سرور‌به‌سرورِ درگاه پرداخت واقعی (که فرم ما نیست).
        """
        if request.method in CSRF.WRITE_METHODS:
            if request.endpoint not in current_app.config.get("CSRF_EXEMPT_ENDPOINTS", ()):
                CSRF.validate()

    @app.before_request
    def guard_rate():
        """سقف درخواست‌های نوشتن از هر IP — جلوی ربات و هجوم ساده."""
        if request.method not in CSRF.WRITE_METHODS or request.path.startswith("/static"):
            return
        limit = int(current_app.config.get("LIMIT_WRITES_IP", 0) or 0)
        if limit <= 0:
            return
        ok, retry = D.throttle("w:" + (request.remote_addr or "?"), limit, 60)
        if not ok:
            resp = error_page(429, "درخواست‌ها بیش از حد است",
                              "درخواست‌های شما زیاد است؛ %s ثانیه دیگر ادامه دهید." % fa(retry))
            resp.headers["Retry-After"] = str(max(1, int(retry or 1)))
            return resp

    @app.before_request
    def housekeeping():
        if request.path.startswith("/static"):
            return
        janitor()

    @app.after_request
    def security_headers(response):
        """هدرهای امنیتی: CSP بدون اسکریپت درون‌خطی، HSTS روی HTTPS، و بسته‌کردن
        چارچوب/پیش‌بینی MIME/ارجاع نشانی. صفحه‌های پرداخت و ورود no-store می‌شوند."""
        cfg = current_app.config
        response.headers.setdefault("Content-Security-Policy", cfg.get("CONTENT_SECURITY_POLICY") or "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=()"
        response.headers["X-XSS-Protection"] = "0"   # مرورگرهای تازه خودشان مدیریت می‌کنند
        response.headers["Server"] = "nobatin"       # نسخهٔ werkzeug فاش نمی‌شود
        if request.is_secure or cfg.get("IS_PRODUCTION"):
            secs = int(cfg.get("HSTS_SECONDS") or 0)
            if secs:
                response.headers["Strict-Transport-Security"] = (
                    "max-age=%d; includeSubDomains" % secs)
        # کوکی نشست دارد یا دادهٔ شخصی نشان می‌دهد → کش مشترک ممنوع
        if session.get("uid") or request.path.startswith(
                ("/nobat", "/kasbkar", "/modiriat", "/pankack", "/profile", "/gateway")):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Vary"] = "Cookie"
        elif request.method == "GET" and request.path.startswith("/k/"):
            # صفحهٔ عمومی کسب‌وکار: ثانیه‌ها کوتاه، چون زمان‌های آزاد زنده عوض می‌شوند
            response.headers["Cache-Control"] = "public, max-age=10"
        return response

    @app.teardown_appcontext
    def close_db(_):
        d = g.pop("db", None)
        if d is not None:
            d.close()

    @app.route("/healthz")
    def healthz():
        """بازدید سلامت برای load balancer / uptime. پیش‌فرض فقط از loopback،
        یا با ?token= از بیرون. جزئیات داخلی نشان داده نمی‌شود."""
        cfg = current_app.config
        if not cfg.get("HEALTHZ_ENABLED"):
            abort(404)
        allow = cfg.get("HEALTHZ_ALLOW") or ()
        token = cfg.get("HEALTHZ_TOKEN")
        # IPِ سوکت، نه remote_addrِ بازنویسی‌شده توسط ProxyFix: سرصفحهٔ
        # X-Forwarded-For را فرستنده می‌نویسد، پس با «127.0.0.1» جعلی در آن سرصفحه
        # نمی‌توان ورودی این صفحه شد.
        ip = request.environ.get("nobatin.peer_addr") or request.remote_addr or ""
        ok_ip = ip in allow
        given = (request.headers.get("X-Health-Token") or
                 request.args.get("token") or "")
        ok_token = bool(token) and secrets.compare_digest(str(given), str(token))
        if not (ok_ip or ok_token):
            abort(404)
        try:
            n = D.scalar("SELECT COUNT(*) FROM businesses", (), None)
            db_ok = n is not None
        except Exception:
            db_ok = False
        body = {"status": "ok" if db_ok else "degraded", "db": "up" if db_ok else "down",
                "uptime": int(time.time() - STARTED), "env": cfg.get("ENV"),
                "cache": CACHE.stats()}
        return (jsonify(body), 200) if db_ok else (jsonify(body), 503)

    @app.errorhandler(400)
    def e400(_e):
        return error_page(400, "درخواست نامعتبر",
                          "نشست شما تمام شده یا فرم از بیرون این سایت ارسال شده است. "
                          "به صفحهٔ قبل برگردید و دوباره تلاش کنید.")

    @app.errorhandler(403)
    def e403(_e):
        return error_page(403, "دسترسی غیرمجاز",
                          "نقش کاربری شما اجازهٔ مشاهدهٔ این بخش را ندارد.")

    @app.errorhandler(404)
    def e404(_e):
        return error_page(404, "پیدا نشد",
                          "صفحه‌ای که دنبال آن بودید وجود ندارد یا حذف شده است.")

    @app.errorhandler(413)
    def e413(_e):
        return error_page(413, "درخواست خیلی بزرگ است",
                          "چیزی که فرستادید از حد مجاز بیشتر است.")

    @app.errorhandler(429)
    def e429(e):
        wait = getattr(e, "description", "") or ""
        return error_page(429, "درخواست‌ها بیش از حد است",
                          "برای محافظت از سرویس، موقتاً سرعت درخواست‌هایتان "
                          "محدود شد. کمی بعد دوباره تلاش کنید. " + str(wait))

    @app.errorhandler(500)
    def e500(e):
        app.logger.exception("server error")
        return error_page(500, "خطای سرور",
                          "مشکلی پیش آمد. اگر تکرار شد با پشتیبانی تماس بگیرید.")

    @app.errorhandler(Exception)
    def e_any(e):
        """هر خطای پیش‌بینی‌نشده: لاگ کامل، پاسخ عمومی. جزئیات فنی به کاربر نشان
        داده نمی‌شود (پشتهٔ خطا مسیر، ستون و گاهی دادهٔ داخلی را فاش می‌کند)."""
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            # ۴۰۰/۴۰۳/۴۰۴/۴۱۳/۴۲۹/۵۰۰ هندلر اختصاصی دارند و به اینجا نمی‌رسند؛
            # بقیه (مثل ۴۰۵) همین‌جا صفحهٔ فارسی می‌گیرند نه متن خام werkzeug.
            return error_page(e.code or 500, e.name or "خطا", str(e.description or ""))
        app.logger.exception("unhandled error on %s %s", request.method, request.path)
        return error_page(500, "خطای سرور",
                          "مشکلی پیش آمد و ثبت شد. لطفاً چند لحظه دیگر دوباره "
                          "تلاش کنید یا با پشتیبانی تماس بگیرید.")


STATUS_LABELS = {
    "pending_payment": "در انتظار پرداخت",
    "confirmed": "تأییدشده",
    "cancelled": "لغوشده",
    "completed": "انجام شد",
    "no_show": "حضور نیافت",
    "expired": "منقضی‌شده",
}
PAY_LABELS = {
    "unpaid": "پرداخت‌نشده",
    "paid": "پرداخت‌شده",
    "partially_paid": "پرداخت جزئی",
    "refund_pending": "در انتظار بازپرداخت",
    "refunded": "بازپرداخت شد",
}
TX_LABELS = {"created": "در جریان", "pending": "در جریان", "success": "موفق",
             "failed": "ناموفق", "cancelled": "لغوشده"}
TX_KINDS = {"payment": "پرداخت", "refund": "بازپرداخت", "settlement": "تسویه"}


def human_hours(h):
    if h is None:
        return ""
    h = float(h)
    if h <= 0:
        return "کمتر از یک دقیقه"
    if h < 1:
        return f"{fa(int(h * 60))} دقیقه"
    if h < 48:
        return f"{fa(round(h))} ساعت"
    return f"{fa(round(h / 24))} روز"


def usage():
    print("""
  نوبتین — راه‌انداز و ابزار سرور

    python app.py                    اجرای وب‌سرور (روی سرور: gunicorn/waitress را انتخاب کنید)
    python app.py --selfcheck       بررسی آمادگی استقرار (کلید، کوکی، WAL، ایندکس، ساعت، …)
    python app.py --prune           کارهای نگهداری: کهنه‌ها، ماسک کدها، checkpoint
    python app.py --send-reminders  ارسال یادآوری‌های معوق و خروج (برای cron/timer)
    python app.py --expiry          آزادسازی سبدها/نوبت‌های بی‌زمان (برای cron/timer)
    python app.py --reset           ساخت دوبارهٔ دادهٔ نمونه (دیتابیس فعلی پاک می‌شود)

  اجرای جدی (production و fail-closed):
    export NOBATIN_ENV=production NOBATIN_SECRET=... NOBATIN_TRUSTED_HOSTS=example.com
    gunicorn -w 3 -b 127.0.0.1:8000 'app:create_app()'
""")


def selfcheck(app):
    """فهرست (severity, code, پیام) برای تصمیم «آمادهٔ سرور هستم یا نه».

    severity: FAIL (باید برطرف شود) / WARN (رسک قابل‌پذیرش) / OK.
    بیرون از درخواست هم صدا زده می‌شود (--selfcheck و آزمون‌ها)، پس خودمان
    زمینهٔ برنامه را باز می‌کنیم تا کوئری‌ها «Working outside of application
    context» نگیرند.
    """
    with app.app_context():
        return _selfcheck_rows(app)


def _selfcheck_rows(app):
    out = []
    cfg = app.config
    prod = bool(cfg.get("IS_PRODUCTION"))

    def add(level, code, msg):
        out.append((level, code, msg))

    add("OK" if prod else "WARN", "env",
        "حالت: %s%s" % (cfg.get("ENV"), "" if prod else " — برای سرور NOBATIN_ENV=production بگذارید"))

    secret = app.secret_key or ""
    if prod:
        if secret == C.DEV_SECRET:
            add("FAIL", "secret", "کلید نشست هنوز مقدار پیش‌فرض توسعه است")
        elif len(secret) < 32:
            add("FAIL", "secret", "کلید نشست کوتاه است (≥۳۲ نویسه)")
        else:
            add("OK", "secret", "کلید نشست بلند و اختصاصی است")
    else:
        add("WARN", "secret", "در حالت توسعه از کلید پیش‌فرض استفاده می‌شود")

    if cfg.get("OTP_MASTER"):
        add("FAIL" if prod else "WARN", "otp_master", "کد مستر «۱۲۳۴۵» روشن است — هر شماره وارد می‌شود")
    else:
        add("OK", "otp_master", "کد مستر خاموش است; فقط کد پیامکی واقعی کار می‌کند")

    add("OK" if cfg.get("SESSION_COOKIE_HTTPONLY") else "FAIL", "cookie_httponly", "کوکی HttpOnly")
    if cfg.get("SESSION_COOKIE_SAMESITE") in ("Lax", "Strict"):
        add("OK", "cookie_samesite", "SameSite=%s" % cfg["SESSION_COOKIE_SAMESITE"])
    else:
        add("FAIL", "cookie_samesite", "SameSite تنظیم نشده")
    if cfg.get("SESSION_COOKIE_SECURE"):
        add("OK", "cookie_secure", "کوکی Secure (فقط HTTPS)")
    else:
        add("FAIL" if prod else "OK", "cookie_secure",
            ("کوکی Secure نیست و روی سرور HTTPS است؛ NOBATIN_COOKIE_SECURE=1 لازم است"
             if prod else
             "کوکی Secure نیست — در توسعه با http طبیعی است؛ در production پیش‌فرض روشن است"))

    hosts = cfg.get("TRUSTED_HOSTS") or ()
    if hosts:
        add("OK", "hosts", "هاست‌های مجاز: " + ", ".join(hosts))
    else:
        add("FAIL" if prod else "WARN", "hosts",
            "فهرست هاست مجاز خالی است؛ روی سرور NOBATIN_TRUSTED_HOSTS را پر کنید "
            "(در production بی آن اصلاً بالا نمی‌آید)")


    csp = cfg.get("CONTENT_SECURITY_POLICY") or ""
    if "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]:
        add("OK", "csp", "CSP اسکریپت درون‌خطی را ممنوع می‌کند")
    else:
        add("FAIL" if prod else "WARN", "csp", "CSP اسکریپت را محدود نمی‌کند")
    if cfg.get("HSTS_SECONDS"):
        add("OK", "hsts", "HSTS %s ثانیه" % fa(cfg["HSTS_SECONDS"]))
    else:
        add("FAIL" if prod else "WARN", "hsts",
            "HSTS خاموش است؛ روی HTTPS باید NOBATIN_HSTS_SECONDS مقدار بگیرد "
            "(در production بی آن اصلاً بالا نمی‌آید)")


    # پایگاه داده
    try:
        mode = D.query("PRAGMA journal_mode", (), one=True)[0]
        add("OK" if str(mode).lower() == "wal" else "WARN", "wal", "journal_mode=%s" % mode)
        busy = D.query("PRAGMA busy_timeout", (), one=True)[0]
        add("OK" if int(busy) >= 2000 else "WARN", "busy_timeout", "busy_timeout=%s ms" % busy)
        have = {r["name"] for r in D.query_d(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
        # نام ایندکس‌ها از همان جمله‌های CREATE خوانده می‌شود، پس فهرست و بررسی
        # یک‌جا نگه داشته می‌شوند و از هم دور نمی‌افتند. (با regex: بریدن رشته به
        # «idx_» نام را بی‌پیشوند می‌داد و این بررسی همیشه FAIL می‌شد.)
        want = set()
        for n in D.HOT_INDEXES:
            m = re.search(r"INDEX(?: IF NOT EXISTS)?\s+([A-Za-z0-9_]+)", n)
            if m:
                want.add(m.group(1))
        absent = sorted(want - have)
        add("OK" if not absent else "FAIL", "indexes",
            "همهٔ %s ایندکس گرم ساخته شده" % fa(len(want)) if not absent
            else "ایندکس‌های جاافتاده: " + ", ".join(absent))
        n_rate = D.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='rate_events'", (), 0)
        add("OK" if n_rate else "FAIL", "rate_table", "جدول سقف نرخ موجود است")
    except Exception as exc:
        add("FAIL", "db", "دیتابیس پاسخ نمی‌دهد: %s" % exc)

    try:
        inst = D.INSTANCE_DIR
        probe = os.path.join(inst, ".write_test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        free_gb = shutil.disk_usage(inst).free / 1024 ** 3
        add("OK" if free_gb > 0.5 else "WARN", "disk",
            "پوشهٔ instance نوشتنی است؛ %s گیگابایت آزاد" % fa(round(free_gb, 1)))
    except OSError as exc:
        add("FAIL", "disk", "پوشهٔ instance نوشتنی نیست: %s" % exc)

    tz = time.strftime("%z")
    add("OK" if tz in ("+0330", "+0300") else "WARN", "timezone",
        "منطقهٔ ساعت سرور %s — تقویم و مهلت‌های نوبت باید با تهران (+۰۳:۳۰) بخواند" % tz)

    if (cfg.get("SMS_PROVIDER") or "log") == "log":
        add("WARN", "sms", "پیامک در حالت log (فقط در فایل/پنل ثبت می‌شود؛ سرویس واقعی "
                           "از NOBATIN_SMS_PROVIDER=kavenegar)")
    else:
        add("OK", "sms", "ارکان پیامک: %s" % cfg.get("SMS_PROVIDER"))
    if (cfg.get("PAYMENT_PROVIDER") or "sandbox") == "sandbox":
        add("WARN", "pay", "درگاه پرداخت شبیه‌سازی‌شده است؛ برای پول واقعی اتصال درگاه لازم است")
    else:
        add("OK", "pay", "ارکان درگاه: %s" % cfg.get("PAYMENT_PROVIDER"))

    last = D.setting("last_prune", "")
    try:
        age_days = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).days if last else None
    except ValueError:
        age_days = None
    if age_days is None:
        add("WARN", "retention", "کارهای نگهداری (--prune) هرگز اجرا نشده؛ کدها و پیامک‌ها انبار می‌شوند")
    elif age_days > 7:
        add("WARN", "retention", "آخرین --prune %s روز پیش بوده (هفتگی توصیه می‌شود)" % fa(age_days))
    else:
        add("OK", "retention", "آخرین --prune: %s روز پیش" % fa(age_days))

    if not prod and D.setting("site_name", "نوبتین") == "نوبتین":
        add("OK", "seed", "دادهٔ نمونه در حالت توسعه فعال است")
    elif prod:
        demo = D.scalar("SELECT COUNT(*) FROM users WHERE mobile IN "
                        "('09120000000','09121112233','09301234567')", (), 0)
        add("FAIL" if demo else "OK", "seed",
            "%s حساب نمونه در production مانده (کد مستر حتی خاموش، ورود با پیامکِ لاگ‌شده ممکن است)"
            % fa(demo) if demo else "حساب‌های نمونه در production نیستند")
        admin = D.scalar("SELECT COUNT(*) FROM users WHERE role='admin'", (), 0)
        add("OK" if admin else "WARN", "admin",
            "%s مدیر ساخته‌شده" % fa(admin) if admin else
            "هیچ مدیری نیست؛ با NOBATIN_ADMIN_MOBILE اولین مدیر را بسازید")
    return out


def print_selfcheck(app):
    rows = selfcheck(app)
    fails = [r for r in rows if r[0] == "FAIL"]
    warns = [r for r in rows if r[0] == "WARN"]
    order = {"FAIL": 0, "WARN": 1, "OK": 2}
    print("\n  نوبتین — بررسی آمادگی سرور (حالت: %s)\n" % app.config.get("ENV"))
    for level, code, msg in sorted(rows, key=lambda r: (order[r[0]], r[1])):
        mark = {"FAIL": "✖", "WARN": "⚠", "OK": "✔"}[level]
        print("   %s %-13s %s" % (mark, code, msg))
    print("\n  نتیجه: %s" % ("آمادهٔ سرور ✅" if not fails else
                             "%s مورد باید پیش از انتشار برطرف شود ❌" % fa(len(fails))))
    if warns:
        print("  %s هشدار قابل‌پذیرش (با تصمیم خودتان)." % fa(len(warns)))
    return 0 if not fails else 1


app = create_app("--reset" in os.sys.argv)

if __name__ == "__main__":
    argv = os.sys.argv
    if "--help" in argv or "-h" in argv:
        usage()
    elif "--selfcheck" in argv:
        with app.app_context():
            os.sys.exit(print_selfcheck(app))
    elif "--prune" in argv:
        with app.app_context():
            res = D.prune(vacuum="--vacuum" in argv)
            D.set_setting("last_prune", D.now_str())
            print("کارهای نگهداری: " + "، ".join(
                "%s %s" % (fa(v), k) for k, v in res.items()))
    elif "--send-reminders" in argv:
        with app.app_context():
            print("یادآوری ارسال‌شده: %s" % fa(B.send_reminders()))
    elif "--expiry" in argv:
        with app.app_context():
            janitor(force=True)
            print("سبدها و نوبت‌های بی‌زمان آزاد شدند.")
    else:
        # همان دو نامی که .env.example و wsgi.py نشان می‌دهند؛ NOBATIN_BIND فقط
        # نامِ قدیمیِ همان تنظیم است و برای فایل‌های .env موجود همچنان کار می‌کند.
        port = int(os.environ.get("NOBATIN_PORT") or os.environ.get("PORT") or 5077)
        host = os.environ.get("NOBATIN_HOST") or os.environ.get("NOBATIN_BIND") or "127.0.0.1"
        master = app.config.get("OTP_MASTER")
        print("\n  نوبتین روشن است (%s) →  http://%s:%s\n" % (app.config["ENV"], host, port))
        print("  حساب‌های نمونه: مدیر 09120000000 | کسب‌وکار 09121112233 | مشتری 09301234567")
        print("  کد ورود: " + ("حالت توسعه — کد ثابت ۱۲۳۴۵" if master
                               else "واقعی؛ پیامک را از پنل مدیریت › پیامک‌ها بخوانید") + "\n")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
