# -*- coding: utf-8 -*-
"""صفحات عمومی: خانه، جست‌وجو، دسته‌بندی، صفحهٔ کسب‌وکار."""
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, abort

from core import db as D
from core import booking as B
from core.jalali import like_pattern, parse_policy
from core.helpers import business_or_404, enrich_biz_rows, decorate_biz, current_user, require_login

bp = Blueprint("public", __name__)

SEARCH_SQL = """
SELECT b.*, c.title AS cat_title, ct.name AS city_name, p.name AS prov_name,
       COUNT(DISTINCT s.id) AS svc_count, MIN(s.price) AS min_price,
       (SELECT COUNT(*) FROM appointments a WHERE a.business_id=b.id AND a.status='confirmed') AS sold
FROM businesses b
LEFT JOIN services s ON s.business_id=b.id AND s.is_active=1
LEFT JOIN categories c ON c.id=b.category_id
LEFT JOIN cities ct ON ct.id=b.city_id
LEFT JOIN provinces p ON p.id=b.province_id
WHERE {where}
GROUP BY b.id ORDER BY {order} LIMIT 40
"""

# زیرشرطِ شمارش نوبت‌های قطعی؛ در SQLهای «ویژهٔ امروز» و «مشابه‌ها» هم به کار می‌رود.
SOLD = ("(SELECT COUNT(*) FROM appointments a WHERE a.business_id=b.id "
        "AND a.status='confirmed')")

# «پرطرفدارترین» تنها بر پایهٔ نوبت‌های قطعی (sold) است؛ امتیاز و نظر در نسخهٔ اول
# پیاده نشده‌اند، پس هیچ مرتب‌سازی یا بنری نباید به b.rating تکیه کند.
ORDERS = {
    "best": "sold DESC, b.id ASC",
    "cheap": "min_price ASC",
    "new": "b.id DESC",
    "busy": "sold DESC, b.id ASC",
}


def do_search(q=None, cat=None, city=None, prov=None, dist=None, order="best", limit=40,
              with_slots=False):
    where, args = ["b.status='active'"], []
    q = (q or "").strip()
    pat = like_pattern(q) if q else None
    if pat:
        where.append("(b.name LIKE ? ESCAPE '!' OR b.about LIKE ? ESCAPE '!' "
                     "OR s.title LIKE ? ESCAPE '!' OR c.title LIKE ? ESCAPE '!')")
        args += [pat] * 4
    if cat:
        where.append("b.category_id=?")
        args.append(cat)
    if city:
        where.append("b.city_id=?")
        args.append(city)
    if prov:
        where.append("b.province_id=?")
        args.append(prov)
    if dist:
        where.append("b.district_id=?")
        args.append(dist)
    sql = SEARCH_SQL.format(where=" AND ".join(where), order=ORDERS.get(order, ORDERS["best"]))
    rows = D.query_d(sql, args)
    if with_slots:
        for r in rows:
            sum_ = B.slot_summary(r["id"], request_today(), r.get("dur") or 30)
            r["free_today"] = sum_["free"]
            r["first_today"] = sum_["first"]
    return rows[:limit]


def request_today():
    return date.today().isoformat()


@bp.route("/")
def home():
    q = (request.args.get("q") or "").strip()
    if q:
        return redirect(url_for("public.search", q=q))
    cat = request.args.get("cat", type=int)
    city = request.args.get("city", type=int)
    order = request.args.get("order", "best")
    results = do_search(cat=cat, city=city, order=order) if (cat or city) else []
    featured = D.query_d(
        "SELECT b.*, c.title AS cat_title, ct.name AS city_name FROM businesses b "
        "LEFT JOIN categories c ON c.id=b.category_id LEFT JOIN cities ct ON ct.id=b.city_id "
        "WHERE b.status='active' ORDER BY " + SOLD + " DESC, b.id ASC LIMIT 6")
    deals = D.query_d(
        "SELECT b.*, c.title AS cat_title, MIN(s.price) AS min_price FROM businesses b "
        "LEFT JOIN categories c ON c.id=b.category_id "
        "LEFT JOIN services s ON s.business_id=b.id AND s.is_active=1 "
        "WHERE b.status='active' GROUP BY b.id ORDER BY min_price ASC LIMIT 4")
    stats = {
        "businesses": D.scalar("SELECT COUNT(*) FROM businesses WHERE status='active'", (), 0),
        "services": D.scalar("SELECT COUNT(*) FROM services WHERE is_active=1", (), 0),
        "cities": D.scalar("SELECT COUNT(*) FROM cities", (), 0),
        "bookings": D.scalar("SELECT COUNT(*) FROM appointments WHERE status='confirmed'", (), 0),
        "today": D.scalar("SELECT COUNT(*) FROM appointments WHERE date=? AND status='confirmed'",
                          (date.today().isoformat(),), 0),
    }
    return render_template("home.html", featured=enrich_biz_rows(featured),
                           deals=enrich_biz_rows(deals), stats=stats,
                           results=enrich_biz_rows(results) if results else None,
                           cities=D.query_d("SELECT c.*, p.name AS prov FROM cities c "
                                            "JOIN provinces p ON p.id=c.province_id ORDER BY p.name, c.name"),
                           cities_with_biz=D.scalar(
                               "SELECT COUNT(DISTINCT city_id) FROM businesses WHERE status='active'", (), 0))


@bp.route("/jostojoo")
def search():
    q = (request.args.get("q") or "").strip()
    cat = request.args.get("cat", type=int)
    city = request.args.get("city", type=int)
    prov = request.args.get("prov", type=int)
    dist = request.args.get("district", type=int)
    order = request.args.get("order", "best")
    results = do_search(q=q, cat=cat, city=city, prov=prov, dist=dist, order=order)
    districts = D.query_d(
        "SELECT d.id, d.name, d.city_id, c.province_id FROM districts d "
        "JOIN cities c ON c.id=d.city_id WHERE EXISTS(SELECT 1 FROM businesses b "
        " WHERE b.district_id=d.id AND b.status='active') "
        "ORDER BY c.name, d.name")
    params = {"q": q, "cat": cat, "city": city, "prov": prov, "district": dist, "order": order}
    labels = {
        "q": q,
        "prov": D.scalar("SELECT name FROM provinces WHERE id=?", (prov,), "") if prov else "",
        "city": D.scalar("SELECT name FROM cities WHERE id=?", (city,), "") if city else "",
        "district": D.scalar("SELECT name FROM districts WHERE id=?", (dist,), "") if dist else "",
        "cat": D.scalar("SELECT title FROM categories WHERE id=?", (cat,), "") if cat else "",
    }
    pills = []
    for key, label in labels.items():                      # فیلترهای فعال، هر کدام با ✕ برداشتن
        if not label:
            continue
        rest = {k: v for k, v in params.items()
                if k != key and v not in (None, "") and not (k == "order" and v == "best")}
        pills.append({"key": key, "label": label,
                      "url": url_for("public.search", **rest) if rest else url_for("public.search")})
    return render_template("search.html", q=q, cat=cat, city=city, prov=prov, district=dist,
                           order=order, results=enrich_biz_rows(results), districts=districts,
                           pills=pills,
                           total=D.scalar("SELECT COUNT(*) FROM businesses WHERE status='active'",
                                          (), 0))


@bp.route("/daste/<int:cid>")
def category_page(cid):
    cat = D.query_d("SELECT * FROM categories WHERE id=?", (cid,), one=True)
    if not cat or not cat["is_active"]:
        abort(404)
    city = request.args.get("city", type=int)
    order = request.args.get("order", "best")
    results = do_search(cat=cid, city=city, order=order)
    cities = D.query_d(
        "SELECT DISTINCT c.id, c.name FROM cities c JOIN businesses b ON b.city_id=c.id "
        "WHERE b.category_id=? AND b.status='active' ORDER BY c.name", (cid,))
    return render_template("category.html", cat=cat, results=enrich_biz_rows(results),
                           cities=cities, city=city, order=order)


@bp.route("/k/<slug>")
def business_page(slug):
    biz = business_or_404(slug)
    if biz["status"] != "active" and not (current_user() and current_user()["role"] == "admin"):
        abort(404)
    decorate_biz(biz)
    services = D.query_d("SELECT * FROM services WHERE business_id=? AND is_active=1 ORDER BY sort",
                         (biz["id"],))
    for s in services:
        s["pay_now"] = min(biz["deposit_amount"] or 0, s["price"]) if biz["payment_mode"] == "deposit" \
            else s["price"]
    today = date.today().isoformat()
    iso = request.args.get("date") or today
    preview_svc = services[0] if services else None
    preview_slots = B.slot_times(biz["id"], iso, preview_svc["duration_min"] if preview_svc else 30)
    free_today = len([s for s in preview_slots if not s.get("past") and not s.get("taken")])
    hours = D.query_d("SELECT * FROM working_hours WHERE business_id=? AND is_active=1 ORDER BY weekday",
                      (biz["id"],))
    similar = D.query_d(
        "SELECT b.*, c.title AS cat_title, ct.name AS city_name FROM businesses b "
        "LEFT JOIN categories c ON c.id=b.category_id LEFT JOIN cities ct ON ct.id=b.city_id "
        "WHERE b.category_id=? AND b.id<>? AND b.status='active' ORDER BY "
        + SOLD + " DESC LIMIT 3",
        (biz["category_id"], biz["id"]))
    return render_template("business.html", biz=biz, services=services, hours=hours,
                           policy=parse_policy(biz["cancel_policy"]),
                           iso=iso, preview_slots=preview_slots, free_today=free_today,
                           similar=enrich_biz_rows(similar),
                           sold=D.scalar("SELECT COUNT(*) FROM appointments WHERE business_id=? "
                                         "AND status='confirmed'", (biz["id"],), 0),
                           blocked=B.is_blocked(biz["id"], iso))


@bp.route("/sms-box")
def sms_box():
    """صندوق پیامک: چون اپراتور واقعی وصل نیست، پیامک‌ها همین‌جا خوانده می‌شوند.
    هر کاربر فقط پیامک‌های شمارهٔ خودش را می‌بیند؛ همهٔ پیامک‌ها فقط برای مدیر."""
    blocked = require_login()
    if blocked:
        return blocked
    u = current_user()
    if u["role"] == "admin":
        rows = D.query_d("SELECT * FROM sms_log ORDER BY id DESC LIMIT 80")
        forbidden_note = ""
    else:
        rows = D.query_d("SELECT * FROM sms_log WHERE mobile=? ORDER BY id DESC LIMIT 80",
                         (u["mobile"],))
        forbidden_note = "فقط پیامک‌های شمارهٔ شما نمایش داده می‌شود."
    return render_template("sms_box.html", rows=rows, note=forbidden_note,
                           is_admin=(u["role"] == "admin"))


@bp.route("/darbare")
def about():
    return render_template("info.html", title="دربارهٔ نوبتین", kind="about")


@bp.route("/qavaed")
def terms():
    return render_template("info.html", title="قوانین و مقررات", kind="terms")


@bp.route("/pooshhe")
def press_kit():
    return render_template("info.html", title="هویت بصری نوبتین", kind="brand")
