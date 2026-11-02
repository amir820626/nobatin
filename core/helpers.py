# -*- coding: utf-8 -*-
"""کمک‌های مشترک مسیرها: کاربر جاری، دسترسی‌ها، قالب‌بندی دادهٔ نمایشی."""
from functools import wraps
from datetime import datetime, date
from urllib.parse import urlsplit

from flask import session, redirect, url_for, request, abort, g, flash, current_app

from core import db as D
from core.jalali import normalize_mobile, fa_date, fa, to_jdate


def limit_hit(bucket, limit, window, message="درخواست‌هایتان زیاد شده است؛ %s ثانیه دیگر دوباره تلاش کنید."):
    """سقف نرخ نوشتن؛ اگر شکسته شود پیام را flash و True برمی‌گرداند.

    شمارنده روی خودِ دیتابیس است، پس اگر سه process (gunicorn worker) در کار باشد،
    هر سه یک سقف را می‌بینند — برخلاف شمارندهٔ در حافظهٔ هر worker.
    """
    limit = int(limit or 0)
    if limit <= 0:
        return False
    ok, retry = D.throttle(str(bucket), limit, int(window))
    if ok:
        return False
    flash(message % fa(retry) if "%s" in message else message, "error")
    current_app.logger.info("rate limit hit: %s (%s/%ss)", bucket, limit, window)
    return True


def cfg_limit(name):
    return int(current_app.config.get(name, 0) or 0)


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if getattr(g, "_uid", None) != uid:
        g._uid = uid
        g._user = D.query_d("SELECT * FROM users WHERE id=?", (uid,), one=True)
    # حسابی که مدیر غیرفعالش کرده، با کوکیِ قدیمی هم باید دیگر «وارد» نباشد.
    # بی این خط، هر مسیری که فقط current_user() را می‌پرسد (نه require_login) — مثل
    # تأیید درگاه، «رزرو سریع» یا ویرایش پروفایل — برای کاربر تعلیق‌شده باز می‌ماند.
    if g._user and not g._user["is_active"]:
        return None
    return g._user


def safe_next(target, fallback=""):
    """مقصدِ بیرون از سایت را رد می‌کند؛ مقدار بی‌خطر (یا fallback) می‌دهد.

    قاعده: فقط مسیر نسبیِ `/…`. `//host` و `/\\host` (که مرورگرها همان را //host
    می‌خوانند) و هر چیزِ دارای scheme/host رد می‌شوند، و نویسه‌های کنترل/خط‌شکن
    که در سرصفحهٔ Location تزریق می‌شوند هم همین‌طور.
    """
    t = (target or "").strip()
    if not t or t[0] != "/" or "\r" in t or "\n" in t or "\x00" in t:
        return fallback or url_for("public.home")
    if t.startswith("//") or t.startswith("/\\") or "\\" in t or "@" in t:
        return fallback or url_for("public.home")
    if urlsplit(t).netloc or urlsplit(t).scheme:
        return fallback or url_for("public.home")
    return t


def login_user(user):
    session.clear()
    session["uid"] = user["id"]
    # نشست «دائمی» تا PERMANENT_SESSION_LIFETIME ادامه می‌یابد (۱۲ ساعت در سرور،
    # ۳۰ روز در توسعه). بی این خط کوکی با بستن مرورگر می‌مرد و کاربرِ در میانهٔ
    # پرداخت مجبور به ورود دوباره می‌شد.
    session.permanent = True
    g._uid = user["id"]
    g._user = user


def logout_user():
    session.clear()
    g._uid = None
    g._user = None


def audit(action, target="", detail=""):
    """ثبت یک اقدام در ردپای مدیریتی؛ کاربر جاری را خودش از نشست می‌خواند."""
    u = current_user()
    return D.log_audit(u["id"] if u else None, u["role"] if u else "guest",
                       action, target, detail)


def require_login(role=None):
    """None اگر اجازه دارد، در غیر این صورت redirect/abort."""
    u = current_user()
    if not u:
        # کوکیِ حسابی که مدیر غیرفعال کرده باید بمیرد، نه فقط بی‌اثر بماند:
        # current_user() جلوی دسترسی را می‌گیرد، ولی تا کوکی هست هر مسیری که
        # فقط «نشست دارد؟» را می‌پرسد (نه require_login) می‌تواند گمراه شود.
        if session.get("uid") and D.scalar("SELECT is_active FROM users WHERE id=?",
                                           (session["uid"],), 1) == 0:
            logout_user()
        session["next_url"] = (request.full_path if request.method == "GET"
                               else request.path)
        return redirect(url_for("auth.login", next=request.path if request.method == "GET"
                                else request.referrer or "/"))
    if role and u["role"] != role and u["role"] != "admin":
        abort(403)
    if not u["is_active"]:
        logout_user()
        abort(403)
    return None


def my_business(user):
    return D.query_d("SELECT * FROM businesses WHERE owner_id=?", (user["id"],), one=True)


def business_or_404(id_or_slug):
    """slug اولویت دارد: اگر کسب‌وکاری slugِ عددی داشته باشد، با شناسهٔ عددیِ
    کسب‌وکار دیگر اشتباه گرفته نشود. فقط int (مثل a["business_id"]) با شناسه خوانده
    می‌شود؛ رشته‌ها نخست slug هستند و اگر پیدا نشد و عدد بودند، آنگاه شناسه."""
    if isinstance(id_or_slug, int):
        b = D.query_d("SELECT * FROM businesses WHERE id=?", (id_or_slug,), one=True)
    else:
        b = D.query_d("SELECT * FROM businesses WHERE slug=?", (id_or_slug,), one=True)
        if not b and str(id_or_slug).isdigit():
            b = D.query_d("SELECT * FROM businesses WHERE id=?", (id_or_slug,), one=True)
    if not b:
        abort(404)
    # برچسب‌های «دسته / شهر / استان / محله» در تیتر و breadcrumb صفحهٔ کسب‌وکار،
    # داشبورد مالک، رزرو و جزئیات نوبت لازم‌اند. business_or_404 تنها نقطهٔ خواندن
    # کسب‌وکار است، پس همین‌جا افزوده می‌شوند؛ بی این‌ها تیتر «—  در  |» می‌شود و
    # breadcrumb دو پیوندِ بی‌متن می‌داد.
    b["cat_title"] = D.scalar("SELECT title FROM categories WHERE id=?", (b["category_id"],), "")
    b["city_name"] = D.scalar("SELECT name FROM cities WHERE id=?", (b["city_id"],), "")
    b["prov_name"] = D.scalar(
        "SELECT p.name FROM provinces p JOIN cities c ON c.province_id=p.id WHERE c.id=?",
        (b["city_id"],), "")
    b["district"] = D.scalar("SELECT name FROM districts WHERE id=?", (b["district_id"],), "")
    return b


def appointment_or_404(aid):
    a = D.query_d("SELECT * FROM appointments WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    return a


def can_manage_biz(biz):
    u = current_user()
    return bool(u and (u["role"] == "admin" or u["id"] == biz["owner_id"]))


def decorate_biz(biz):
    """برچسب‌های رنگی/متنی برای کارت‌ها."""
    hue = biz.get("hue") or 172
    biz["style"] = f"--biz-hue:{hue}"
    biz["initials"] = "".join(w[0] for w in (biz.get("name") or "")[:14].split()[:2])
    biz["mode_label"] = {"full": "پرداخت کامل آنلاین", "deposit": "پیش‌پرداخت آنلاین",
                         "free": "پرداخت در محل"}[biz.get("payment_mode") or "full"]
    return biz


def enrich_biz_rows(rows):
    for r in rows:
        decorate_biz(r)
    return rows


def j_range(start, stop):
    return list(range(start, stop))


def human_hours(h):
    if h is None:
        return ""
    h = float(h)
    if h <= 0:
        return "کمتر از یک دقیقه"
    if h < 1:
        return f"{fa(int(h * 60))} دقیقه"
    if h < 24:
        return f"{fa(round(h, 1))} ساعت"
    return f"{fa(round(h / 24, 1))} روز"
