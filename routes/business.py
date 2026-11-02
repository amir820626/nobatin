# -*- coding: utf-8 -*-
"""پنل کسب‌وکار: نوبت‌ها، خدمات، ساعات کاری، تعطیلی، قوانین کنسلی و مالی."""
import json
import re
from datetime import date, datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort,
                   session, current_app)

from core import db as D
from core import booking as B
from core.helpers import (current_user, require_login, my_business, business_or_404,
                          can_manage_biz, decorate_biz, appointment_or_404, human_hours, audit,
                          login_user, limit_hit, cfg_limit)
from core.jalali import fa, fa_money, fa_date, parse_policy, rule_label, jweekday

bp = Blueprint("business", __name__)

# نشانی صفحهٔ کسب‌وکار: /k/<slug> — فقط [a-z0-9-]، بدون فاصله و کاراکتر خاص
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")
# ساعت ۲۴ساعتهٔ «HH:MM» که درگاه/ساعات کاری قبول می‌کنیم
HM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def clip(value, limit):
    """برش سمت سرور: `maxlength` قالب فقط در مرورگر اثر دارد و درخواستِ مستقیم
    می‌تواند مگابایت متن در «دربارهٔ ما» بفرستد که به همهٔ بازدیدکننده‌ها نشان
    داده می‌شود (و هر جست‌وجوی LIKE را کند می‌کند)."""
    return (value or "").strip()[:limit]


@bp.route("/kasbkar")
def panel():
    # هر کاربر واردشده می‌تواند پنل کسب‌وکار را باز کند؛ اگر کسب‌وکاری نداشته باشد
    # به فرم ثبت می‌رود (تا لینک «کسب‌وکار خود را ثبت کنید» در پابرگ به بن‌بست نخورد).
    blocked = require_login()
    if blocked:
        return blocked
    biz = my_business(current_user())
    if not biz:
        return redirect(url_for("business.new_biz"))
    return redirect(url_for("business.dashboard", slug=biz["slug"]))


@bp.route("/kasbkar/new", methods=["GET", "POST"])
def new_biz():
    blocked = require_login()
    if blocked:
        return blocked
    if my_business(current_user()):
        return redirect(url_for("business.panel"))
    if request.method == "POST":
        # سقف ثبت کسب‌وکار از هر IP در روز: جلوی ربات‌هایی که صدها صفحهٔ تبلیغاتی
        # می‌سازند. کاربر عادی هرگز به این عدد نمی‌رسد.
        if limit_hit("bizip:" + (request.remote_addr or "?"), cfg_limit("LIMIT_BIZ_IP"), 86400,
                     "تعداد ثبت کسب‌وکار از این رایانه برای امروز کامل شده است؛ "
                     "فردا دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."):
            return new_biz_form()
        name = (request.form.get("name") or "").strip()
        slug = (request.form.get("slug") or "").strip().lower()
        mode = request.form.get("payment_mode") or "full"
        deposit = max(0, request.form.get("deposit", type=int) or 0)
        hold = min(30, max(3, request.form.get("hold_minutes", type=int) or 10))
        if not name:
            flash("نام کسب‌وکار را وارد کنید.", "error")
        elif len(name) > 80:
            flash("نام کسب‌وکار خیلی بلند است (حداکثر ۸۰ نویسه).", "error")
        # slug بخشی از نشانی اینترنتی صفحه است؛ فقط حروف کوچک، عدد و خط تیره و
        # حداکثر شصت نویسه. اعتبارسنجی سمت سرور لازم است، چون `pattern` قالب
        # فقط در مرورگر اثر می‌کند و درخواستِ مستقیم از بیرون را نمی‌گیرد.
        elif not slug:
            flash("نشانی اینترنتی صفحه را وارد کنید (مثلاً arasteh-beauty).", "error")
        elif not SLUG_RE.match(slug):
            flash("نشانی اینترنتی صفحه باید فقط حروف انگلیسی کوچک، عدد و خط تیره باشد "
                  "(مثلاً arasteh-beauty).", "error")
        elif mode not in ("full", "deposit", "free"):
            flash("نوع پرداخت نامعتبر است.", "error")
        elif D.scalar("SELECT COUNT(*) FROM businesses WHERE slug=?", (slug,), 0):
            flash("این نشانی قبلاً استفاده شده است.", "error")
        elif mode == "deposit" and not deposit:
            flash("در حالت «پیش‌پرداخت» مبلغ بیعانه را هم وارد کنید.", "error")
        else:
            policy = build_policy(request)
            dist_name = clip(request.form.get("district"), 60)
            dist_id = (D.get_or_create_district(request.form.get("city", type=int), dist_name)
                       if dist_name and request.form.get("city") else None)
            # کمیسیون اولیه از تنظیمات پلتفرم گرفته می‌شود؛ فرم ثبت روی همین عدد را
            # به صاحب کسب‌وکار «دورهٔ معرفی» قول می‌دهد، پس نباید صفر ساخته شود.
            # تغییرش بعداً فقط از پنل مدیریت مجاز است (admin.biz_action).
            # `or "5"` (نه `or 0`): اگر مقدارِ ذخیره‌شده در تنظیمات خالی باشد، platform
            # بی‌سروصدا صفر کمیسیون نمی‌گیرد؛ همان پیش‌فرضِ فرم به کار می‌رود.
            fee = max(0.0, min(50.0, float(D.setting("platform_fee_percent", "5") or "5")))
            u = current_user()
            try:
                # کسب‌وکار + ساعات کاری + ارتقای نقش: یا هر سه، یا هیچ‌کدام.
                with D.tx():
                    bid = D.execute(
                        "INSERT INTO businesses(owner_id,name,slug,category_id,province_id,city_id,district_id,"
                        "address,phone,about,hue,status,payment_mode,deposit_amount,cancel_policy,hold_minutes,"
                        "commission_percent,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (u["id"], name, slug, request.form.get("category", type=int),
                         request.form.get("province", type=int), request.form.get("city", type=int),
                         dist_id, clip(request.form.get("address"), 200),
                         clip(request.form.get("phone"), 30), clip(request.form.get("about"), 1500),
                         min(359, max(0, int(request.form.get("hue") or 172))), "pending",
                         mode, deposit, json.dumps(policy, ensure_ascii=False),
                         hold, fee, D.now_str()))
                    for wd in range(7):    # شنبه تا جمعه — روز تعطیل را خود کسب‌وکار بستن می‌گیرد
                        D.execute("INSERT INTO working_hours(business_id,weekday,start_hm,end_hm,slot_min) "
                                  "VALUES(?,?,?,?,?)", (bid, wd, "09:00", "19:00", 30))
                    # اولین کسب‌وکار، کاربرِ مشتری را به نقش کسب‌وکار تبدیل می‌کند تا پنلش باز بماند.
                    # (قبل از flash انجام می‌شود چون login_user نشست را پاک می‌کند.)
                    if u["role"] == "customer":
                        D.execute("UPDATE users SET role='business' WHERE id=?", (u["id"],))
                        audit("user_become_business", f"user:{u['id']}",
                              f"با ثبت اولین کسب‌وکار ({name})")
            except Exception as exc:
                current_app.logger.exception("business create failed: %s", exc)
                flash("ثبت کسب‌وکار ممکن نشد؛ لطفاً دوباره تلاش کنید.", "error")
            else:
                if u["role"] == "customer":
                    login_user(D.query_d("SELECT * FROM users WHERE id=?", (u["id"],), one=True))
                flash("کسب‌وکار ثبت شد. پس از تأیید مدیر، نوبت‌دهی آنلاین فعال می‌شود.", "ok")
                return redirect(url_for("business.dashboard", slug=slug))
    return new_biz_form()


def new_biz_form():
    """فرم ثبت کسب‌وکار با فهرست‌های دسته/استان/شهر (کش‌شده، چون به‌ندرت عوض می‌شوند)."""
    cats = D.query_d("SELECT * FROM categories WHERE is_active=1 ORDER BY sort")
    provinces = D.query_d("SELECT * FROM provinces ORDER BY name")
    cities = D.query_d("SELECT c.*, p.name AS prov FROM cities c "
                       "JOIN provinces p ON p.id=c.province_id ORDER BY p.name, c.name")
    return render_template("business/new.html", cats=cats, provinces=provinces, cities=cities)


def build_policy(req):
    """قانون‌های کنسلیِ فرم را به JSONِ قابل‌اعتماد تبدیل می‌کند.

    هر فیلدِ این فرم می‌تواند از یک POSTِ دستی هم بیاید، پس اینجا چیزی فرض
    نمی‌شود:
      * ساعتِ غیرعددی رد می‌شود (پیش‌تر `int(...)` روی refund_ می‌ترکد و ذخیرهٔ
        قوانین ۵۰۰ می‌شد).
      * ساعت فقط ۰ تا یک سال می‌ماند. قانونِ «-۵ ساعت» همیشه برقرار است، پس یک
        بدفرمی = بازپرداخت کاملِ تضمینی برای هر لغوی دیررس.
      * درصد فقط ۰..۱۰۰ و فقط عددِ درست‌خوانده‌شده؛ خرابیِ آن صفر است، نه صدمه به
        کل فرم.
      * ساعتِ تکراری یکی می‌ماند (اولین ردیف فرم)، وگرنه دو قانون هم‌زمان در
        فهرست می‌نشست و ترتیب چیدنِ «سخاوتمندانه‌ترینِ برقرار» مبهم می‌شد.
    """
    rules = {}
    for raw in req.form.getlist("rule_hours"):
        try:
            hours = int(raw)
        except (TypeError, ValueError):
            continue
        if not 0 <= hours <= 24 * 365 or hours in rules:
            continue
        try:
            percent = int(req.form.get("refund_%d" % hours) or 0)
        except (TypeError, ValueError):
            percent = 0
        rules[hours] = max(0, min(100, percent))
    return [{"min_hours": h, "refund_percent": rules[h]} for h in sorted(rules, reverse=True)]


@bp.route("/kasbkar/<slug>")
def dashboard(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    decorate_biz(biz)
    biz["district"] = D.scalar("SELECT name FROM districts WHERE id=?", (biz["district_id"],), "")
    iso = request.args.get("date") or date.today().isoformat()
    tab = request.args.get("tab", "today")
    services = D.query_d("SELECT * FROM services WHERE business_id=? ORDER BY sort", (biz["id"],))
    hours = D.query_d("SELECT * FROM working_hours WHERE business_id=? ORDER BY weekday", (biz["id"],))
    hour_map = {h["weekday"]: h for h in hours}
    blocks = D.query_d("SELECT * FROM day_blocks WHERE business_id=? AND date>=? ORDER BY date",
                       (biz["id"], date.today().isoformat()))
    todays = [decorate_appt_biz(a) for a in D.query_d(
        "SELECT a.*, s.title AS svc, s.duration_min, u.name AS cname, u.mobile, "
        "(SELECT COALESCE(SUM(t.amount),0) FROM transactions t WHERE t.appointment_id=a.id "
        " AND t.kind='payment' AND t.status='success') AS paid_ok "
        "FROM appointments a JOIN services s ON s.id=a.service_id JOIN users u ON u.id=a.user_id "
        "WHERE a.business_id=? AND a.date=? ORDER BY a.start_hm", (biz["id"], iso))]
    taken_map = {a["start_hm"]: a for a in todays}
    slots = B.slot_times(biz["id"], iso, 30)
    balance = B.business_balance(biz["id"])
    ctx = dict(biz=biz, iso=iso, tab=tab, services=services, hours=hours, hour_map=hour_map,
               blocks=blocks, todays=todays, taken=taken_map, slots=slots, bal=balance,
               appts=[decorate_appt_biz(a) for a in D.query_d(
                   "SELECT a.*, s.title AS svc, u.name AS cname, u.mobile, "
                   "(SELECT COALESCE(SUM(t.amount),0) FROM transactions t WHERE t.appointment_id=a.id "
                   " AND t.kind='payment' AND t.status='success') AS paid_ok "
                   "FROM appointments a JOIN services s ON s.id=a.service_id JOIN users u ON u.id=a.user_id "
                   "WHERE a.business_id=? ORDER BY a.date DESC, a.start_hm DESC LIMIT 150", (biz["id"],))],
               policy=parse_policy(biz["cancel_policy"]),
               upcoming=D.query_d(
                   "SELECT a.*, s.title AS svc, u.name AS cname, u.mobile FROM appointments a "
                   "JOIN services s ON s.id=a.service_id JOIN users u ON u.id=a.user_id "
                   "WHERE a.business_id=? AND a.date>=? AND a.status IN ('confirmed','pending_payment') "
                   "ORDER BY a.date, a.start_hm LIMIT 25", (biz["id"], iso)),
               months=D.query_d(
                   "SELECT substr(a.date,1,7) AS m, COUNT(*) AS n, "
                   " SUM(CASE WHEN a.status='confirmed' THEN 1 ELSE 0 END) AS ok, "
                   " SUM(CASE WHEN a.status='cancelled' THEN 1 ELSE 0 END) AS cancel "
                   "FROM appointments a WHERE a.business_id=? GROUP BY m ORDER BY m DESC LIMIT 6",
                   (biz["id"],)),
               top=D.query_d(
                   "SELECT s.title, s.price, COUNT(a.id) AS n, "
                   " COALESCE(SUM(CASE WHEN a.status='confirmed' THEN t.amount ELSE 0 END),0) AS rev "
                   "FROM services s LEFT JOIN appointments a ON a.service_id=s.id "
                   " LEFT JOIN transactions t ON t.appointment_id=a.id AND t.kind='payment' "
                   "  AND t.status='success' "
                   "WHERE s.business_id=? GROUP BY s.id ORDER BY rev DESC, n DESC LIMIT 6", (biz["id"],)),
               recent_tx=D.query_d(
                   "SELECT t.*, a.code FROM transactions t LEFT JOIN appointments a ON a.id=t.appointment_id "
                   "WHERE t.business_id=? ORDER BY t.id DESC LIMIT 20", (biz["id"],)),
               refunds=D.query_d("SELECT r.*, a.code FROM refunds r "
                                 "JOIN appointments a ON a.id=r.appointment_id "
                                 "WHERE a.business_id=? ORDER BY r.id DESC LIMIT 12", (biz["id"],)),
               settlements=D.query_d("SELECT * FROM settlements WHERE business_id=? ORDER BY id DESC LIMIT 12",
                                     (biz["id"],)),
               cats=D.query_d("SELECT * FROM categories WHERE is_active=1 ORDER BY sort"),
               districts=D.query_d("SELECT * FROM districts WHERE city_id=? ORDER BY name", (biz["city_id"],)),
               stats={"today": len([t for t in todays if t["status"] == "confirmed"]),
                      "pending_pay": len([t for t in todays if t["status"] == "pending_payment"]),
                      "week": D.scalar("SELECT COUNT(*) FROM appointments WHERE business_id=? "
                                       "AND date>=? AND status='confirmed'",
                                       (biz["id"], (date.today() - timedelta(days=jweekday(date.today()))
                                                    ).isoformat()), 0),
                      "total": D.scalar("SELECT COUNT(*) FROM appointments WHERE business_id=? "
                                        "AND status='confirmed'", (biz["id"],), 0),
                      "cancel_rate": cancel_rate(biz["id"])})
    return render_template("business/dashboard.html", **ctx)


def cancel_rate(bid):
    tot = D.scalar("SELECT COUNT(*) FROM appointments WHERE business_id=?", (bid,), 0)
    c = D.scalar("SELECT COUNT(*) FROM appointments WHERE business_id=? AND status='cancelled'", (bid,), 0)
    return int(round(c / tot * 100)) if tot else 0


def decorate_appt_biz(a):
    a["cancel"] = B.cancellation_preview(a)
    a["is_past"] = f"{a['date']} {a['start_hm']}" < datetime.now().strftime("%Y-%m-%d %H:%M")
    return a


@bp.route("/kasbkar/<slug>/service", methods=["POST"])
def save_service(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    sid = request.form.get("id", type=int)
    title = clip(request.form.get("title"), 80)
    if not title:
        flash("عنوان سرویس لازم است.", "error")
        return redirect(url_for("business.dashboard", slug=slug, tab="services"))
    price = max(0, request.form.get("price", type=int) or 0)
    dur = max(10, request.form.get("duration", type=int) or 30)
    desc = clip(request.form.get("description"), 500)
    if sid:
        # «مال من باشد وگرنه هیچ» — و اگر هیچ ردیفی عوض نشد، به صاحب مجموعه دروغ
        # نمی‌گوییم که «سرویس به‌روزرسانی شد» (شناسهٔ سرویسِ مجموعهٔ دیگر).
        if not D.changed("UPDATE services SET title=?, price=?, duration_min=?, description=?, "
                         "buffer_min=?, is_active=? WHERE id=? AND business_id=?",
                         (title, price, dur, desc, max(0, request.form.get("buffer", type=int) or 0),
                          1 if request.form.get("is_active") else 0, sid, biz["id"])):
            flash("آن سرویس در این کسب‌وکار پیدا نشد؛ چیزی تغییر نکرد.", "error")
            return redirect(url_for("business.dashboard", slug=slug, tab="services"))
        flash("سرویس به‌روزرسانی شد.", "ok")
    else:
        D.execute("INSERT INTO services(business_id,title,price,duration_min,description,buffer_min,sort) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (biz["id"], title, price, dur, desc,
                   max(0, request.form.get("buffer", type=int) or 0), price))
        flash("سرویس جدید اضافه شد.", "ok")
    return redirect(url_for("business.dashboard", slug=slug, tab="services"))


@bp.route("/kasbkar/<slug>/hours", methods=["POST"])
def save_hours(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    # اول می‌خوانیم و اعتبارسنجی می‌کنیم، بعد می‌نویسیم: اگر همهٔ بازه‌ها بی‌اعتبار
    # بودند، ساعت‌های کاری موجود پاک نمی‌شوند.
    parsed, bad = [], []
    for wd in range(7):
        if not request.form.get(f"on{wd}"):
            continue
        start = request.form.get(f"start{wd}") or "09:00"
        end = request.form.get(f"end{wd}") or "18:00"
        if not HM_RE.match(start) or not HM_RE.match(end):
            bad.append(f"ساعت {fa_date_weekday(wd)} معتبر نیست؛ باید به شکل ۰۹:۳۰ باشد")
            continue
        if B.hm_to_min(end) - B.hm_to_min(start) < 30:
            bad.append(f"بازهٔ {fa_date_weekday(wd)} معتبر نیست (پایان باید حداقل ۳۰ دقیقه "
                       "بعد از شروع باشد)")
            continue
        parsed.append((wd, start, end, min(240, max(10, request.form.get(f"slot{wd}", type=int) or 30))))
    if bad and not parsed:
        for msg in bad:
            flash(msg + ".", "error")
        return redirect(url_for("business.dashboard", slug=slug, tab="hours"))
    # هفت روز = حذف و درج در یک تراکنش؛ اگر میانهٔ کار خطایی رخ دهد، کسب‌وکار بدون
    # ساعت کاری یا با نیمی از بازه‌ها نمی‌ماند.
    with D.tx():
        for wd in range(7):
            D.execute("DELETE FROM working_hours WHERE business_id=? AND weekday=?", (biz["id"], wd))
        for wd, start, end, slot in parsed:
            D.execute("INSERT INTO working_hours(business_id,weekday,start_hm,end_hm,slot_min) "
                      "VALUES(?,?,?,?,?)", (biz["id"], wd, start, end, slot))
    for msg in bad:
        flash(msg + " و ذخیره نشد.", "error")
    flash("ساعات کاری ذخیره شد و تقویم زمان‌های آزاد بلافاصله به‌روز می‌شود.", "ok")
    return redirect(url_for("business.dashboard", slug=slug, tab="hours"))


def fa_date_weekday(wd):
    return "شنبه یکشنبه دوشنبه سه‌شنبه چهارشنبه پنج‌شنبه جمعه".split()[wd]


@bp.route("/kasbkar/<slug>/block", methods=["POST"])
def block_day(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    d = request.form.get("date")
    if request.form.get("_remove"):
        D.execute("DELETE FROM day_blocks WHERE business_id=? AND date=?", (biz["id"], d))
        flash("تعطیلی آن روز حذف شد.", "ok")
    elif d:
        D.execute("INSERT OR IGNORE INTO day_blocks(business_id,date,reason) VALUES(?,?,?)",
                  (biz["id"], d, request.form.get("reason") or "تعطیل"))
        n = D.scalar("SELECT COUNT(*) FROM appointments WHERE business_id=? AND date=? "
                     "AND status IN ('confirmed','pending_payment')", (biz["id"], d), 0)
        flash(f"روز {fa_date(d)} تعطیل شد." + (f" {fa(n)} نوبت فعال در آن روز وجود دارد." if n else ""),
              "ok")
    return redirect(url_for("business.dashboard", slug=slug, tab="hours"))


@bp.route("/kasbkar/<slug>/rules", methods=["POST"])
def save_rules(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    rules = build_policy(request)
    if not rules:
        flash("حداقل یک قانون کنسلی تعریف کنید.", "error")
        return redirect(url_for("business.dashboard", slug=slug, tab="rules"))
    mode = request.form.get("payment_mode") or "full"
    deposit = max(0, request.form.get("deposit", type=int) or 0)
    if mode == "deposit" and deposit <= 0:
        # بیعانهٔ صفر یعنی «پرداختی در کار نیست» و نوبت بی‌پرداخت قطعی می‌شود؛
        # این با الزام «پرداخت ناموفق = نوبت قطعی نه» در تضاد است، پس رد می‌کنیم.
        flash("در حالت «پیش‌پرداخت» باید مبلغ بیعانه را بیشتر از صفر وارد کنید.", "error")
        return redirect(url_for("business.dashboard", slug=slug, tab="rules"))
    # کارمزد پلتفرم از این فرم خوانده نمی‌شود: فیلدی در پنل کسب‌وکار ندارد و
    # تغییرش فقط از تب کسب‌وکارهای پنل مدیریت (admin.biz_action) مجاز است.
    # وگرنه با یک POST دستی می‌شد کارمزد خود را صفر کرد.
    D.execute("UPDATE businesses SET cancel_policy=?, payment_mode=?, deposit_amount=?, "
              "hold_minutes=? WHERE id=?",
              (json.dumps(rules, ensure_ascii=False), mode,
               deposit,
               # همان سقفِ فرم ثبت (3..30): بدون بالا، یک POSTِ دستی می‌توانست
               # hold_minutes را میلیونی کند و ساعت‌های آن کسب‌وکار عملاً قفل بماند.
               min(30, max(3, request.form.get("hold_minutes", type=int) or 10)),
               biz["id"]))
    flash("قوانین ذخیره شد. نوبت‌هایی که قبلاً رزرو شده‌اند، با قوانین لحظهٔ رزرو خود باقی می‌مانند.", "ok")
    return redirect(url_for("business.dashboard", slug=slug, tab="rules"))


@bp.route("/kasbkar/<slug>/profile", methods=["POST"])
def save_profile(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    city = request.form.get("city", type=int) or biz["city_id"]
    dist_name = clip(request.form.get("district"), 60)
    dist_id = D.get_or_create_district(city, dist_name) if dist_name else biz["district_id"]
    D.execute("UPDATE businesses SET name=?, about=?, address=?, phone=?, district_id=?, city_id=?, "
              "province_id=?, hue=?, category_id=? WHERE id=?",
              ((request.form.get("name") or biz["name"]).strip()[:80], clip(request.form.get("about"), 1500),
               clip(request.form.get("address"), 200), clip(request.form.get("phone"), 30),
               dist_id, city,
               request.form.get("province", type=int) or biz["province_id"],
               int(request.form.get("hue") or biz["hue"]),
               request.form.get("category", type=int) or biz["category_id"], biz["id"]))
    flash("اطلاعات صفحهٔ کسب‌وکار به‌روز شد.", "ok")
    return redirect(url_for("business.dashboard", slug=slug, tab="profile"))


@bp.route("/nobat/<int:aid>/<action>", methods=["POST"])
def appt_action(aid, action):
    blocked = require_login("business")
    if blocked:
        return blocked
    a = appointment_or_404(aid)
    biz = business_or_404(a["business_id"])
    if not can_manage_biz(biz):
        abort(403)
    if action in ("completed", "no_show", "confirmed"):
        # تغییر وضعیت + ردپا + پیامک در یک تراکنش، تا نوبتی بدون ردپا یا
        # ردپایی دربارهٔ نوبتِ تغییرناکرده باقی نماند.
        with D.tx():
            D.execute("UPDATE appointments SET status=?, confirmed_at=COALESCE(confirmed_at,?) WHERE id=?",
                      (action, D.now_str(), aid))
            audit("appt_" + action, "appt:%s" % a["code"], "%s: %s → %s" % (biz["name"],
                                                                           a["status"], action))
            if action in ("completed", "no_show"):
                B.notify_status(aid, action)
        flash({"completed": "نوبت انجام‌شده ثبت شد و به مشتری پیامک رفت.",
               "no_show": "حضور نیافت ثبت شد و به مشتری پیامک رفت.",
               "confirmed": "نوبت تأیید شد."}[action], "ok")
    elif action == "cancel":
        ok, info = B.cancel_appointment(aid, request.form.get("reason") or "لغو توسط کسب‌وکار", "business")
        audit("appt_cancel" if ok else "appt_cancel_blocked", "appt:%s" % a["code"],
              request.form.get("reason") or "لغو توسط کسب‌وکار")
        if not ok:
            flash(info, "error")
        elif isinstance(info, dict):
            flash(f"نوبت لغو و {fa_money(info['amount'])} تومان بازپرداخت شد ({info['label']}).", "ok")
        else:
            flash(info, "ok")
    else:
        abort(404)
    return redirect(url_for("business.dashboard", slug=biz["slug"], date=a["date"]))


@bp.route("/kasbkar/<slug>/reminders", methods=["POST"])
def reminders(slug):
    blocked = require_login("business")
    if blocked:
        return blocked
    biz = business_or_404(slug)
    if not can_manage_biz(biz):
        abort(403)
    n = B.send_reminders(biz["id"])
    flash(("هیچ یادآوری تازه‌ای لازم نبود (هر نوبت یک بار یادآوری می‌گیرد)." if n == 0
           else f"{fa(n)} یادآوری پیامکی برای نزدیک‌ترین نوبت‌های همین مجموعه ارسال شد."), "ok")
    return redirect(url_for("business.dashboard", slug=slug, date=biz and date.today().isoformat()))
