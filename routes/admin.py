# -*- coding: utf-8 -*-
"""پنل مدیریت: کاربران، کسب‌وکارها، دسته‌بندی‌ها و مکان‌ها، مالی، محتوا و پیامک."""
import json
import re
from datetime import date, datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort,
                   jsonify)

from core import db as D
from core import booking as B
from core.helpers import (current_user, require_login, business_or_404, appointment_or_404,
                          decorate_biz, audit)
from core.jalali import (fa, fa_money, fa_date, normalize_mobile, is_valid_mobile,
                         JALALI_MONTHS, to_jdate, reference, norm_place)

bp = Blueprint("admin", __name__)

TABS = [
    ("overview", "نمای کلی", "chart"),
    ("businesses", "کسب‌وکارها", "shop"),
    ("users", "کاربران", "users"),
    ("cats", "دسته‌ها و مکان‌ها", "grid"),
    ("money", "مالی و تسویه", "wallet"),
    ("appointments", "نوبت‌ها", "calendar"),
    ("sms", "پیامک‌ها", "chat"),
    ("log", "ردپای اقدامات", "shield"),
    ("settings", "تنظیمات", "gear"),
]


@bp.route("/modiriat")
def panel():
    blocked = require_login("admin")
    if blocked:
        return blocked
    tab = request.args.get("tab", "overview")
    ctx = {"tab": tab, "tabs": TABS,
           "stats": {
               "users": D.scalar("SELECT COUNT(*) FROM users", (), 0),
               "biz": D.scalar("SELECT COUNT(*) FROM businesses WHERE status='active'", (), 0),
               "pending": D.scalar("SELECT COUNT(*) FROM businesses WHERE status='pending'", (), 0),
               "appts": D.scalar("SELECT COUNT(*) FROM appointments", (), 0),
               "confirmed": D.scalar("SELECT COUNT(*) FROM appointments WHERE status='confirmed'", (), 0),
               "today": D.scalar("SELECT COUNT(*) FROM appointments WHERE date=?",
                                 (date.today().isoformat(),), 0),
           },
           "finance": finance_totals(),
           "days": daily_series(14)}
    if tab == "businesses":
        ctx["rows"] = [decorate_biz(dict(r)) for r in D.query_d(
            "SELECT b.*, c.title AS cat_title, ct.name AS city_name, p.name AS prov_name, "
            "u.name AS owner_name, u.mobile AS owner_mobile, u.is_active AS owner_active, "
            "(SELECT COUNT(*) FROM appointments a WHERE a.business_id=b.id AND a.status='confirmed') AS sold, "
            "(SELECT COUNT(*) FROM services s WHERE s.business_id=b.id AND s.is_active=1) AS svc_n "
            "FROM businesses b LEFT JOIN categories c ON c.id=b.category_id "
            "LEFT JOIN cities ct ON ct.id=b.city_id LEFT JOIN provinces p ON p.id=b.province_id "
            "LEFT JOIN users u ON u.id=b.owner_id "
            "WHERE (?='' OR b.status=?) ORDER BY "
            "CASE b.status WHEN 'pending' THEN 0 ELSE 1 END, b.id DESC",
            (request.args.get("status", ""), request.args.get("status", "")))]
        for r in ctx["rows"]:
            r["bal"] = B.business_balance(r["id"])
    elif tab == "users":
        ctx["rows"] = D.query_d(
            "SELECT u.*, (SELECT COUNT(*) FROM appointments a WHERE a.user_id=u.id) AS n, "
            "(SELECT COUNT(*) FROM businesses b WHERE b.owner_id=u.id) AS biz_n, "
            "(SELECT MAX(date) FROM appointments a WHERE a.user_id=u.id) AS last "
            "FROM users u WHERE (?='' OR u.role=?) ORDER BY u.id DESC LIMIT 300",
            (request.args.get("role", ""), request.args.get("role", "")))
    elif tab == "cats":
        ctx["cats"] = D.query_d(
            "SELECT c.*, (SELECT COUNT(*) FROM businesses b WHERE b.category_id=c.id) AS n, "
            "(SELECT COUNT(*) FROM services s JOIN businesses b ON b.id=s.business_id "
            " WHERE b.category_id=c.id) AS svc FROM categories c ORDER BY sort")
        ctx["provs"] = D.query_d(
            "SELECT p.*, (SELECT COUNT(*) FROM cities c WHERE c.province_id=p.id) AS nc, "
            "(SELECT COUNT(*) FROM businesses b WHERE b.province_id=p.id) AS nb "
            "FROM provinces p ORDER BY name")
        ctx["cities"] = D.query_d(
            "SELECT c.*, p.name AS prov, "
            "(SELECT COUNT(*) FROM businesses b WHERE b.city_id=c.id) AS nb "
            "FROM cities c JOIN provinces p ON p.id=c.province_id ORDER BY p.name, c.name")
        ctx["districts"] = D.query_d(
            "SELECT d.*, c.name AS city, p.name AS prov, "
            "(SELECT COUNT(*) FROM businesses b WHERE b.district_id=d.id) AS nb "
            "FROM districts d JOIN cities c ON c.id=d.city_id JOIN provinces p ON p.id=c.province_id "
            "ORDER BY p.name, c.name, d.name")
    elif tab == "log":
        ctx["logs"] = D.query_d(
            "SELECT l.*, u.name AS who, u.mobile FROM audit_log l LEFT JOIN users u ON u.id=l.user_id "
            "ORDER BY l.id DESC LIMIT 200")
        ctx["log_kinds"] = D.query_d(
            "SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY n DESC LIMIT 12")
    elif tab == "money":
        ctx["txs"] = D.query_d(
            "SELECT t.*, a.code, b.name AS biz, u.mobile FROM transactions t "
            "LEFT JOIN appointments a ON a.id=t.appointment_id "
            "LEFT JOIN businesses b ON b.id=t.business_id LEFT JOIN users u ON u.id=t.user_id "
            "WHERE (?='' OR t.status=?) AND (?='' OR t.kind=?) ORDER BY t.id DESC LIMIT 120",
            (request.args.get("status", ""), request.args.get("status", ""),
             request.args.get("kind", ""), request.args.get("kind", "")))
        ctx["refunds"] = D.query_d(
            "SELECT r.*, a.code, u.mobile, b.name AS biz FROM refunds r "
            "LEFT JOIN appointments a ON a.id=r.appointment_id "
            "LEFT JOIN users u ON u.id=a.user_id LEFT JOIN businesses b ON b.id=a.business_id "
            "ORDER BY r.id DESC LIMIT 60")
        ctx["settle"] = D.query_d(
            "SELECT s.*, b.name AS biz, b.slug FROM settlements s JOIN businesses b ON b.id=s.business_id "
            "ORDER BY s.id DESC LIMIT 60")
        ctx["balances"] = [{"biz": b, "bal": B.business_balance(b["id"])} for b in
                           [decorate_biz(dict(r)) for r in D.query_d(
                               "SELECT b.*, c.title AS cat_title FROM businesses b "
                               "LEFT JOIN categories c ON c.id=b.category_id ORDER BY b.name")]]
        ctx["by_month"] = D.query_d(
            "SELECT substr(t.done_at,1,7) AS m, SUM(CASE WHEN t.kind='payment' THEN t.amount ELSE 0 END) AS pay, "
            "SUM(CASE WHEN t.kind='refund' AND t.status='success' THEN t.amount ELSE 0 END) AS back "
            "FROM transactions t WHERE t.status='success' GROUP BY m ORDER BY m DESC LIMIT 12")
    elif tab == "appointments":
        frm = request.args.get("from") or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        to = request.args.get("to") or (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        st = request.args.get("status", "")
        ctx["from"], ctx["to"], ctx["status"] = frm, to, st
        ctx["rows"] = D.query_d(
            "SELECT a.*, b.name AS biz, b.slug, s.title AS svc, u.name AS cname, u.mobile "
            "FROM appointments a JOIN businesses b ON b.id=a.business_id "
            "JOIN services s ON s.id=a.service_id JOIN users u ON u.id=a.user_id "
            "WHERE a.date BETWEEN ? AND ? AND (?='' OR a.status=?) ORDER BY a.date DESC, a.start_hm "
            "LIMIT 200", (frm, to, st, st))
    elif tab == "sms":
        ctx["sms"] = D.query_d("SELECT * FROM sms_log ORDER BY id DESC LIMIT 120")
        ctx["by_tpl"] = D.query_d("SELECT template, COUNT(*) AS n FROM sms_log GROUP BY template "
                                  "ORDER BY n DESC")
        # پیامک‌هایی که واقعاً نرفته‌اند (شمارهٔ ثابت/نامعتبر یا خطای سرویس) باید دیده شوند
        ctx["sms_bad"] = D.query_d("SELECT status, COUNT(*) AS n FROM sms_log "
                                   "WHERE status<>'sent' GROUP BY status")
    elif tab == "settings":
        ctx["settings"] = {r["key"]: r["value"] for r in D.query_d("SELECT * FROM settings")}
    ctx["recent"] = D.query_d(
        "SELECT a.*, b.name AS biz, b.slug, s.title AS svc, u.mobile FROM appointments a "
        "JOIN businesses b ON b.id=a.business_id JOIN services s ON s.id=a.service_id "
        "JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 10")
    return render_template("admin/panel.html", **ctx)


def finance_totals():
    gross = D.scalar("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='payment' "
                     "AND status='success'", (), 0)
    refunded = D.scalar("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE status='done'", (), 0)
    pending_ref = D.scalar("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE status='pending'", (), 0)
    settled = D.scalar("SELECT COALESCE(SUM(amount),0) FROM settlements WHERE status='paid'", (), 0)
    commission = D.scalar(
        "SELECT COALESCE(SUM(t.amount * COALESCE(b.commission_percent,0) / 100.0),0) "
        "FROM transactions t JOIN businesses b ON b.id=t.business_id "
        "WHERE t.kind='payment' AND t.status='success'", (), 0)
    return {
        "gross": gross, "refunded": int(refunded), "pending_ref": int(pending_ref),
        "settled": settled, "commission": int(commission),
        "payable_business": max(gross - refunded - int(pending_ref) - int(commission) - settled, 0),
        "failed": D.scalar("SELECT COUNT(*) FROM transactions WHERE status IN ('failed','cancelled')", (), 0),
        "success": D.scalar("SELECT COUNT(*) FROM transactions WHERE status='success'", (), 0),
        "created": D.scalar("SELECT COUNT(*) FROM transactions WHERE status IN ('created','pending')", (), 0),
        "appt_total": D.scalar("SELECT COUNT(*) FROM appointments", (), 0),
        "appt_pending": D.scalar("SELECT COUNT(*) FROM appointments WHERE status='pending_payment'", (), 0),
        "appt_cancel": D.scalar("SELECT COUNT(*) FROM appointments WHERE status='cancelled'", (), 0),
    }


def daily_series(n=14):
    out = []
    today = datetime.now().date()
    for i in range(n):
        d = (today - timedelta(days=n - 1 - i)).isoformat()
        out.append({
            "iso": d,
            "day": fa(to_jdate(d).day),
            "month": JALALI_MONTHS[to_jdate(d).month - 1],
            "n": D.scalar("SELECT COUNT(*) FROM appointments WHERE date=?", (d,), 0),
            "ok": D.scalar("SELECT COUNT(*) FROM appointments WHERE date=? AND status='confirmed'", (d,), 0),
            "rev": D.scalar("SELECT COALESCE(SUM(t.amount),0) FROM transactions t "
                            "JOIN appointments a ON a.id=t.appointment_id "
                            "WHERE a.date=? AND t.kind='payment' AND t.status='success'", (d,), 0),
        })
    return out


@bp.route("/modiriat/settings", methods=["POST"])
def save_settings():
    blocked = require_login("admin")
    if blocked:
        return blocked
    allowed = ("site_name", "platform_fee_percent", "reminder_hours", "support_phone",
               "hold_minutes_default", "min_pay_percent", "tos_text")
    for k in allowed:
        if request.form.get(k) is not None:
            D.set_setting(k, request.form.get(k).strip())
            audit("setting", k, request.form.get(k).strip()[:80])
    flash("تنظیمات ذخیره شد.", "ok")
    return redirect(url_for("admin.panel", tab="settings"))


@bp.route("/modiriat/biz/<int:bid>/<action>", methods=["POST"])
def biz_action(bid, action):
    blocked = require_login("admin")
    if blocked:
        return blocked
    biz = business_or_404(bid)
    if action in ("active", "suspended", "pending"):
        D.execute("UPDATE businesses SET status=? WHERE id=?", (action, bid))
        audit("biz_" + action, "biz:%s" % bid, biz["name"])
        flash(f"وضعیت «{biz['name']}» به {fa_status(action)} تغییر کرد.", "ok")
    elif action == "commission":
        try:
            # عدد نامعتبر یا منفی نباید ۵۰۰ بدهد یا سهم پلتفرم را منفی کند
            v = max(0.0, min(50.0, float(str(request.form.get("value") or "0").replace("٪", "").strip() or 0)))
        except ValueError:
            v = None
        if v is None:
            flash("درصد کارمزد باید عدد باشد.", "error")
        else:
            with D.tx():
                D.execute("UPDATE businesses SET commission_percent=? WHERE id=?", (v, bid))
                audit("biz_commission", "biz:%s" % bid, "%s%% (قبلاً %s%%)" % (v, biz["commission_percent"]))
            flash(f"کمیسیون «{biz['name']}» {fa(v)}٪ شد.", "ok")
    elif action == "slug":
        s = (request.form.get("value") or "").strip().lower()
        # همان قاعدهٔ فرم ثبت کسب‌وکار: این نشانی مستقیم در URL صفحه می‌نشیند
        if not re.match(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$", s or ""):
            flash("نشانی نامعتبر یا تکراری است؛ فقط حروف کوچک انگلیسی، عدد و خط تیره.", "error")
        elif D.scalar("SELECT COUNT(*) FROM businesses WHERE slug=? AND id<>?", (s, bid), 0):
            flash("نشانی نامعتبر یا تکراری است؛ این نشانی برای کسب‌وکار دیگری استفاده شده.", "error")
        else:
            with D.tx():
                D.execute("UPDATE businesses SET slug=? WHERE id=?", (s, bid))
                audit("biz_slug", "biz:%s" % bid, "%s → %s" % (biz["slug"], s))
            flash("نشانی صفحه تغییر کرد.", "ok")
    return redirect(url_for("admin.panel", tab="businesses"))


def fa_status(s):
    return {"active": "تأییدشده", "suspended": "معلق", "pending": "در انتظار بررسی"}[s]


@bp.route("/modiriat/category", methods=["POST"])
def save_category():
    blocked = require_login("admin")
    if blocked:
        return blocked
    cid = request.form.get("id", type=int)
    title = (request.form.get("title") or "").strip()
    if request.form.get("delete") and cid:
        used = D.scalar("SELECT COUNT(*) FROM businesses WHERE category_id=?", (cid,), 0)
        if used:
            flash(f"این دسته {fa(used)} کسب‌وکار دارد؛ ابتدا آن‌ها را منتقل کنید.", "error")
        else:
            D.execute("UPDATE categories SET is_active=0 WHERE id=?", (cid,))
            flash("دسته غیرفعال شد.", "ok")
    elif title:
        if cid:
            D.execute("UPDATE categories SET title=?, icon=?, blurb=?, sort=?, is_active=? WHERE id=?",
                      (title, request.form.get("icon") or "sparkle", request.form.get("blurb") or "",
                       request.form.get("sort", type=int) or 0, 1 if request.form.get("is_active") else 0, cid))
            flash("دسته به‌روزرسانی شد.", "ok")
        else:
            D.execute("INSERT INTO categories(title,icon,blurb,sort) VALUES(?,?,?,?)",
                      (title, request.form.get("icon") or "sparkle", request.form.get("blurb") or "",
                       D.scalar("SELECT COALESCE(MAX(sort),0)+1 FROM categories", (), 1)))
            flash("دستهٔ تازه اضافه شد.", "ok")
    return redirect(url_for("admin.panel", tab="cats"))


@bp.route("/modiriat/location", methods=["POST"])
def save_location():
    blocked = require_login("admin")
    if blocked:
        return blocked
    kind = request.form.get("kind")
    name = re.sub(r"\s+", " ", (request.form.get("name") or "").strip())[:60]
    if not name:
        flash("نام لازم است.", "error")
    elif kind == "province":
        if D.scalar("SELECT COUNT(*) FROM provinces WHERE norm=? AND norm<>''",
                    (norm_place(name),), 0):
            flash("این استان از قبل ثبت شده است.", "error")
        else:
            D.execute("INSERT INTO provinces(name,norm) VALUES(?,?)", (name, norm_place(name)))
            flash("استان اضافه شد.", "ok")
            audit("province_add", "province:%s" % name, name)
    elif kind == "city":
        pid = request.form.get("province", type=int)
        if not pid:
            flash("استان را انتخاب کنید.", "error")
        elif D.scalar("SELECT COUNT(*) FROM cities WHERE province_id=? AND norm=? AND norm<>''",
                      (pid, norm_place(name)), 0):
            flash("این شهر در همان استان از قبل وجود دارد.", "error")
        else:
            D.execute("INSERT INTO cities(province_id,name,norm) VALUES(?,?,?)",
                      (pid, name, norm_place(name)))
            flash("شهر اضافه شد.", "ok")
            audit("city_add", "city:%s" % name, name)
    elif kind == "district":
        cid = request.form.get("city", type=int)
        if not cid:
            flash("شهر را انتخاب کنید.", "error")
        else:
            did = D.get_or_create_district(cid, name)
            flash("محله ثبت شد (نام‌های یک‌شکل ادغام می‌شوند)." if did else "نام محله معتبر نیست.", "ok")
            audit("district_add", "district:%s" % did, "%s / city:%s" % (name, cid))
    return redirect(url_for("admin.panel", tab="cats"))


@bp.route("/modiriat/district/<int:did>/<action>", methods=["POST"])
def district_action(did, action):
    """تغییر نام یا حذف محله؛ در ادغام دستی، کسب‌وکارها به محلهٔ مقصد منتقل می‌شوند."""
    blocked = require_login("admin")
    if blocked:
        return blocked
    d = D.query_d("SELECT * FROM districts WHERE id=?", (did,), one=True)
    if not d:
        abort(404)
    if action == "rename":
        name = re.sub(r"\s+", " ", (request.form.get("name") or "").strip())[:60]
        key = norm_place(name)
        clash = D.scalar("SELECT id FROM districts WHERE city_id=? AND norm=? AND norm<>'' AND id<>?",
                         (d["city_id"], key, did), None)
        if not key:
            flash("نام محله معتبر نیست.", "error")
        elif clash:
            flash("محله‌ای با این نام در همین شهر هست؛ کسب‌وکارها را به آن منتقل کنید.", "error")
        else:
            D.execute("UPDATE districts SET name=?, norm=? WHERE id=?", (name, key, did))
            flash("نام محله تغییر کرد.", "ok")
            audit("district_rename", "district:%s" % did, "%s ← %s" % (name, d["name"]))
    elif action == "merge":
        into = request.form.get("into", type=int)
        target = D.query_d("SELECT * FROM districts WHERE id=? AND city_id=?", (into or 0, d["city_id"]),
                           one=True)
        if not target or target["id"] == did:
            flash("محلهٔ مقصد باید در همان شهر باشد.", "error")
        else:
            moved = D.scalar("SELECT COUNT(*) FROM businesses WHERE district_id=?", (did,), 0)
            # انتقال و حذف یک تراکنش‌اند: نیمه‌کاره بماند یعنی کسب‌وکارهایی که به
            # محلهٔ حذف‌شده اشاره می‌کنند (یا محله‌ای که هنوز کسب‌وکار دارد).
            with D.tx():
                D.execute("UPDATE businesses SET district_id=? WHERE district_id=?", (target["id"], did))
                D.execute("DELETE FROM districts WHERE id=?", (did,))
                audit("district_merge", "district:%s→%s" % (did, target["id"]),
                      f"{d['name']} → {target['name']} ({moved} business)")
            flash(f"{fa(moved)} کسب‌وکار به «{target['name']}» منتقل شد و «{d['name']}» حذف گردید.", "ok")
    elif action == "delete":
        used = D.scalar("SELECT COUNT(*) FROM businesses WHERE district_id=?", (did,), 0)
        if used:
            flash(f"این محله {fa(used)} کسب‌وکار دارد؛ اول آن‌ها را منتقل کنید.", "error")
        else:
            D.execute("DELETE FROM districts WHERE id=?", (did,))
            flash("محله حذف شد.", "ok")
            audit("district_delete", "district:%s" % did, d["name"])
    return redirect(url_for("admin.panel", tab="cats"))


@bp.route("/modiriat/user/<int:uid>/<action>", methods=["POST"])
def user_action(uid, action):
    blocked = require_login("admin")
    if blocked:
        return blocked
    u = D.query_d("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        abort(404)
    if action == "toggle":
        if u["id"] == current_user()["id"]:
            flash("حساب خودتان را غیرفعال نمی‌کنید.", "error")
        else:
            D.execute("UPDATE users SET is_active=1-is_active WHERE id=?", (uid,))
            audit("user_toggle", "user:%s" % uid,
                  "%s → %s" % (u["mobile"], "فعال" if not u["is_active"] else "غیرفعال"))
            flash("وضعیت فعال/غیرفعال تغییر کرد.", "ok")
    elif action in ("customer", "business", "admin"):
        # در فرم پنل، نقش انتخابی در `name="role"` می‌آید؛ اگر خوانده نشود،
        # «تغییر نقش» همان نقشِ پیش‌فرضِ لینک را اعمال می‌کند و گزینهٔ «مدیر»
        # بی‌سروصدا بی‌اثر می‌ماند (مدیر فکر می‌کند نقش عوض شده).
        want = (request.form.get("role") or "").strip()
        role = want if want in ("customer", "business", "admin") else action
        if role == "admin" and u["id"] == current_user()["id"]:
            flash("نقش خودتان را از همین صفحه تغییر نمی‌دهید.", "error")
            return redirect(url_for("admin.panel", tab="users"))
        D.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        audit("user_role", "user:%s" % uid, "%s: %s → %s" % (u["mobile"], u["role"], role))
        flash("نقش کاربر تغییر کرد.", "ok")
    elif action == "name":
        D.execute("UPDATE users SET name=? WHERE id=?", ((request.form.get("name") or "").strip(), uid))
        audit("user_name", "user:%s" % uid, "%s ← %s" % ((request.form.get("name") or "").strip(),
                                                       u["name"]))
        flash("نام ذخیره شد.", "ok")
    return redirect(url_for("admin.panel", tab="users"))


@bp.route("/modiriat/settle/<int:bid>", methods=["POST"])
def create_settlement(bid):
    blocked = require_login("admin")
    if blocked:
        return blocked
    biz = business_or_404(bid)
    bal = B.business_balance(biz["id"])
    amount = request.form.get("amount", type=int)
    amount = bal["receivable"] if not amount else amount
    if amount <= 0:
        flash("مبلغ تسویه باید بزرگ‌تر از صفر باشد.", "error")
        return redirect(url_for("admin.panel", tab="money"))
    if amount > bal["receivable"]:
        # بیشتر از ماندهٔ واقعی اجازهٔ تسویه نیست، وگرنه موجودی منفی و دادهٔ مالی
        # ناسازگار می‌شود (کسب‌وکارها از هم جدا نگه داشته می‌شوند).
        flash(f"مبلغ درخواستی از ماندهٔ قابل تسویه ({fa_money(bal['receivable'])} تومان) بیشتر است.",
              "error")
        return redirect(url_for("admin.panel", tab="money"))
    with D.tx():
        sid = D.execute(
            "INSERT INTO settlements(business_id,amount,period_from,period_to,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (biz["id"], amount,
             D.scalar("SELECT MIN(date) FROM appointments WHERE business_id=?", (biz["id"],),
                      date.today().isoformat()),
             date.today().isoformat(), "pending", D.now_str()))
        audit("settlement_create", "settlement:%s" % sid,
              "%s / %s تومان" % (biz["name"], fa_money(amount)))
    flash(f"درخواست تسویهٔ {fa_money(amount)} تومان برای «{biz['name']}» ثبت شد (کد {fa(sid)}).", "ok")
    return redirect(url_for("admin.panel", tab="money"))


@bp.route("/modiriat/settlement/<int:sid>/<action>", methods=["POST"])
def settlement_action(sid, action):
    blocked = require_login("admin")
    if blocked:
        return blocked
    s = D.query_d("SELECT * FROM settlements WHERE id=?", (sid,), one=True)
    if not s:
        abort(404)
    if action == "pay":
        if s["status"] == "paid":
            flash("این تسویه پیش‌تر پرداخت شده است؛ دوباره پرداخت نمی‌شود.", "error")
            return redirect(url_for("admin.panel", tab="money"))
        ref = reference("STL")
        owner = D.scalar("SELECT u.mobile FROM businesses b JOIN users u ON u.id=b.owner_id "
                        "WHERE b.id=?", (s["business_id"],), "")
        biz_name = D.scalar("SELECT name FROM businesses WHERE id=?", (s["business_id"],), "")
        owner_id = D.scalar("SELECT owner_id FROM businesses WHERE id=?", (s["business_id"],), None)
        try:
            with D.tx():
                # «پرداخت‌شدن» با شرط وضعیتی نوشته می‌شود: اگر دو مدیر (یا دو کلیک
                # هم‌زمان) بزنند، فقط یکی سطری عوض می‌کند و دیگری رد می‌شود؛
                # پس هیچ‌وقت دو رکورد برداشت برای یک تسویه نداریم.
                cur = D.get_db().execute("UPDATE settlements SET status='paid', paid_at=? "
                                         "WHERE id=? AND status<>'paid'", (D.now_str(), sid))
                if not cur.rowcount:
                    raise D.Abort("already")
                D.execute("INSERT INTO transactions(reference,appointment_id,user_id,business_id,kind,"
                          "amount,status,gateway,trace,message,created_at,done_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (ref, None, owner_id, s["business_id"], "settlement",
                           s["amount"], "success", "manual", f"settlement:{sid}",
                           f"تسویهٔ دورهٔ {s['period_from']} تا {s['period_to']}",
                           D.now_str(), D.now_str()))
                D.send_sms(owner, "settlement", _appt_id=None, amount=fa_money(s["amount"]),
                           biz=biz_name, ref=ref)
                audit("settlement_paid", "settlement:%s" % sid,
                      "%s / ref:%s" % (fa_money(s["amount"]), ref))
        except D.Abort:
            flash("هم‌زمان همین تسویه پرداخت شد؛ رکورد دوباره ثبت نشد.", "error")
            return redirect(url_for("admin.panel", tab="money"))
        flash(f"تسویهٔ {fa_money(s['amount'])} تومان پرداخت و در دفتر کل ثبت شد (کد {ref}).", "ok")
    elif action == "delete" and s["status"] == "pending":
        with D.tx():
            D.execute("DELETE FROM settlements WHERE id=?", (sid,))
            audit("settlement_delete", "settlement:%s" % sid, fa_money(s["amount"]))
        flash("درخواست تسویه حذف شد.", "ok")
    return redirect(url_for("admin.panel", tab="money"))


@bp.route("/modiriat/refund/<int:rid>/<action>", methods=["POST"])
def refund_action(rid, action):
    blocked = require_login("admin")
    if blocked:
        return blocked
    r = D.query_d("SELECT * FROM refunds WHERE id=?", (rid,), one=True)
    if not r:
        abort(404)
    if action == "done":
        mob = D.scalar("SELECT u.mobile FROM appointments a JOIN users u ON u.id=a.user_id WHERE a.id=?",
                       (r["appointment_id"],), "")
        ref = D.scalar("SELECT reference FROM transactions WHERE id=?", (r["transaction_id"],), "")
        with D.tx():
            # سه جدول (بازپرداخت، تراکنش، نوبت) با هم عوض می‌شوند، وگرنه
            # «بازپرداخت انجام شد» بدون رکورد مالیِ موفق می‌ماند.
            # شرط «هنوز pending» + بررسی تعداد ردیف: دو بار کلیک (یا دو تب مرورگر)
            # وگرنه دو تراکنش موفق و دو پیامک برای یک بازپرداخت می‌ساخت.
            if not D.changed("UPDATE refunds SET status='done', done_at=? WHERE id=? "
                             "AND status='pending'", (D.now_str(), rid)):
                raise D.Abort("این بازپرداخت پیش‌تر انجام شده است.")
            D.execute("UPDATE transactions SET status='success', done_at=? WHERE id=?",
                      (D.now_str(), r["transaction_id"]))
            D.execute("UPDATE appointments SET pay_status='refunded' WHERE id=?", (r["appointment_id"],))
            if mob:
                D.send_sms(mob, "refund", _appt_id=r["appointment_id"],
                           amount=fa_money(r["amount"]), ref=ref)
            audit("refund_done", "refund:%s" % rid,
                  "%s / appt:%s" % (fa_money(r["amount"]), r["appointment_id"]))
        flash("بازپرداخت انجام و پیامک ارسال شد.", "ok")
    elif action == "reject":
        with D.tx():
            D.execute("UPDATE refunds SET status='rejected' WHERE id=?", (rid,))
            D.execute("UPDATE transactions SET status='cancelled' WHERE id=?", (r["transaction_id"],))
            audit("refund_reject", "refund:%s" % rid, fa_money(r["amount"]))
        flash("درخواست بازپرداخت رد شد.", "error")
    return redirect(url_for("admin.panel", tab="money"))


@bp.route("/modiriat/sms", methods=["POST"])
def send_test_sms():
    blocked = require_login("admin")
    if blocked:
        return blocked
    mob = normalize_mobile(request.form.get("mobile"))
    tpl = request.form.get("template") or "reminder"
    if not is_valid_mobile(mob):
        flash("شماره معتبر نیست.", "error")
    else:
        D.send_sms(mob, tpl, biz="نوبتین", date=fa_date(date.today()), time=fa("11:00"),
                   code="NB-TEST", amount=fa_money(0), refund="—" if "refund" not in tpl else "")
        flash("پیامک آزمایشی در لیست ثبت شد.", "ok")
        audit("sms_test", mob, tpl)
    return redirect(url_for("admin.panel", tab="sms"))


@bp.route("/modiriat/api/stats")
def api_stats():
    blocked = require_login("admin")
    if blocked:
        return abort(403)
    return jsonify({"stats": D.query_d("SELECT * FROM settings"),
                    "finance": finance_totals(), "days": daily_series(7)})
