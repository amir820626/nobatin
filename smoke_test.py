# -*- coding: utf-8 -*-
"""آزمون سریع نوبتین — جریان کامل رزرو، قفل هم‌زمانی، پرداخت، کنسلی و پنل‌ها.

دیتابیس جدا می‌سازد (instance/nobatin_test.db) تا دادهٔ نمونهٔ اصلی دست‌نخورده بماند:
    python smoke_test.py
"""
import json
import os
import re
import sys
import glob
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["NOBATIN_DB"] = "nobatin_test.db"
# همهٔ کلاینت‌های تستی از یک IP (127.0.0.1) و گاهی یک شماره وارد می‌شوند؛ برای اینکه
# محدودیت‌های نرخ ورود بقیهٔ سناریوها را خراب نکند، اینجا سقف‌ها باز می‌شوند.
# خودِ محدودیت‌ها در بخش «۲۶) امنیت ورود» با تنظیمات واقعی آزموده می‌شوند.
os.environ["NOBATIN_OTP_COOLDOWN"] = "0"
os.environ["NOBATIN_OTP_MAX_MOBILE"] = "500"
os.environ["NOBATIN_OTP_MAX_IP"] = "500"
os.environ["NOBATIN_OTP_MAX_ATTEMPTS"] = "500"
# سقف‌های نرخ نوشتن هم برای سناریوهای عادی باز می‌شوند؛ بخش ۳۳ آن‌ها را جدا و
# با تنظیمات واقعی می‌آزماید.
os.environ["NOBATIN_LIMIT_WRITES_PER_MIN"] = "0"
os.environ["NOBATIN_LIMIT_HOLDS_PER_HOUR"] = "0"
os.environ["NOBATIN_LIMIT_BOOKINGS_PER_HOUR"] = "0"
os.environ["NOBATIN_LIMIT_BIZ_PER_DAY"] = "0"
TEST_DB = os.path.join(ROOT, "instance", "nobatin_test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
sys.path.insert(0, ROOT)

import app as appmod                      # noqa: E402
from core import db as D                  # noqa: E402
from core import booking as B             # noqa: E402
from core.jalali import fa_money, jweekday, to_jdate  # noqa: E402
from routes.public import do_search       # noqa: E402

A = appmod.app
A.config["TESTING"] = True
fails = []

from flask.testing import FlaskClient as PlainClient   # کلاینتِ بی‌توکن، برای آزمودنِ CSRF


class CsrfClient(PlainClient):
    """همان کاری که فرم واقعی می‌کند: توکن نشست را با هر POST می‌فرستد."""

    def post(self, url, data=None, **kw):
        form = dict(data or {})
        with self.session_transaction() as s:
            tok = s.get("csrf_token")
            if not tok:
                tok = s["csrf_token"] = "test-token"
        form.setdefault("csrf", str(tok))
        kw.pop("data", None)
        return super().post(url, data=form, **kw)


A.test_client_class = CsrfClient


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  ← {extra}"))
    if not cond:
        fails.append(name)
    return bool(cond)


def ctx(fn):
    """اجازهٔ کوئری مستقیم بیرون از درخواست."""
    with A.app_context():
        return fn()


def new_client():
    c = A.test_client()
    c.follow = None
    return c


def login(client, mobile):
    r = client.post("/vorood", data={"mobile": mobile})
    r = client.post("/tayid", data={"mobile": mobile, "code": "12345"}, follow_redirects=True)
    return r


def flash_of(client, path):
    body = client.get(path).get_data(as_text=True)
    return re.sub(r"\s+", " ", " ".join(re.findall(r'class="msg[^"]*"[^>]*>(.*?)</div>', body, re.S)))


def first_free_day(biz_id, svc_id, min_off=0):
    """اولین روز (از min_off روز بعد) با حداقل دو زمان آزاد + لیست ساعت‌های آزادش."""
    dur = ctx(lambda: D.scalar("SELECT duration_min FROM services WHERE id=?", (svc_id,), 30))
    for off in range(min_off, 40):
        iso = (date.today() + timedelta(days=off)).isoformat()
        free = [s for s in ctx(lambda: B.slot_times(biz_id, iso, dur))
                if not s.get("past") and not s.get("taken")]
        if len(free) >= 2:
            return iso, [s["hm"] for s in free]
    raise SystemExit(f"زمان آزادی برای آزمون پیدا نشد (biz={biz_id}, min_off={min_off})")


def book(client, slug, svc_id, iso, hm):
    """POST انتخاب زمان؛ بازگرداندن (location, body_نهایی)."""
    r = client.post(f"/k/{slug}/zan", data={"date": iso, "service": svc_id, "time": hm})
    loc = r.headers.get("Location", "")
    body = ""
    if loc:
        body = client.get(loc, follow_redirects=True).get_data(as_text=True)
    return loc, body, r


def gateway_of(loc):
    m = re.search(r"/gateway/([0-9a-fA-F]+)", loc or "")
    return m.group(1) if m else ""


def appt_at(biz_id, iso, hm, status=""):
    """تازه‌ترین نوبتِ آن ساعت (نوبت‌های لغو/منقضی تاریخ‌اند و نباید نتیجه را عوض کنند)."""
    return ctx(lambda: D.query_d(
        "SELECT * FROM appointments WHERE business_id=? AND date=? AND start_hm=? "
        + ("AND status=? " if status else "") + "ORDER BY id DESC LIMIT 1",
        (biz_id, iso, hm, status) if status else (biz_id, iso, hm), one=True))


def logged_in(client):
    return "/khorooj" in client.get("/pankack").get_data(as_text=True)


def live_count(biz_id, iso, hm):
    """شمار نوبت‌های زندهٔ آن ساعت؛ رکوردهای لغو/منقضی تاریخچه‌اند و شمارش نمی‌شوند."""
    return ctx(lambda: D.scalar(
        "SELECT COUNT(*) FROM appointments WHERE business_id=? AND date=? AND start_hm=? "
        "AND status IN ('pending_payment','confirmed')", (biz_id, iso, hm), 0))


def pay(client, token, outcome):
    r = client.post(f"/gateway/{token}", data={"outcome": outcome})
    loc = r.headers.get("Location", "")
    body = client.get(loc, follow_redirects=True).get_data(as_text=True) if loc else ""
    return loc, body


# ---------------------------------------------------------------- دادهٔ پایه
with A.app_context():
    D.init_schema()
    D.seed()
    BIZ = {r["slug"]: r for r in D.query_d("SELECT * FROM businesses")}
    SVC = {}
    for s in D.query_d("SELECT * FROM services WHERE is_active=1 ORDER BY id"):
        SVC.setdefault(s["business_id"], []).append(s)

full_slug = "arasteh-beauty"                       # پرداخت کامل
dep_slug = "dental-plus"                           # بیعانه
free_slug = "karen-service"                        # بدون پرداخت
full_id = BIZ[full_slug]["id"]
dep_id = BIZ[dep_slug]["id"]
free_id = BIZ[free_slug]["id"]
full_svc = SVC[full_id][0]["id"]
dep_svc = SVC[dep_id][0]["id"]
free_svc = SVC[free_id][0]["id"]

print("── ۱) صفحات عمومی")
day, slots = first_free_day(full_id, full_svc)
for path in ["/", "/jostojoo?q=%D8%B1%D9%86%DA%AF", "/jostojoo?q=&city=1&cat=1&order=cheap",
             "/daste/1", f"/k/{full_slug}", f"/k/{full_slug}/rezerv?date={day}&service={full_svc}",
             f"/k/{full_slug}/rezerv?date={day}&shift=-3", "/darbare", "/qavaed", "/pooshhe",
             "/vorood", "/notfound-xyz"]:
    r = A.test_client().get(path)
    want = 404 if path == "/notfound-xyz" else 200
    check(f"GET {path} → {want}", r.status_code == want, r.status_code)

print("\n── ۲) جست‌وجو و فیلترها")
c = new_client()
body = c.get("/jostojoo?q=رنگ").get_data(as_text=True)
check("نتیجهٔ جست‌وجوی متنی شامل نام خدمت", "رنگ و لایت" in body)
check("کارت کسب‌وکار در نتایج رندر می‌شود", 'class="biz lift"' in body, body.count('class="biz lift"'))
dentist = BIZ["dental-plus"]["name"]
for label, term in (("ی/ک عربی", "كلينيك"), ("فاصله به‌جای نیم‌فاصله", "دندان پزشکی"),
                    ("نیم‌فاصلهٔ درست", "دندان‌پزشکی"), ("حرکت عربی", "كلينيک")):
    hits = ctx(lambda t=term: [r["name"] for r in do_search(q=t)])
    check(f"جست‌وجو با {label} همان کسب‌وکار را پیدا می‌کند", dentist in hits, (term, hits))
check("کاراکترهای ویژهٔ LIKE نتیجه را بی‌ربط نمی‌کنند",
      ctx(lambda: do_search(q="%_%")) == [] and ctx(lambda: do_search(q="%")) == [],
      ctx(lambda: len(do_search(q="%_%"))))
check("و جست‌وجوی معمولی همچنان کار می‌کند",
      dentist in [r["name"] for r in ctx(lambda: do_search(q="کلینیک"))])
body = c.get(f"/jostojoo?cat={BIZ[full_slug]['category_id']}").get_data(as_text=True)
check("فیلتر دسته", BIZ[full_slug]["name"] in body)
rows = ctx(lambda: do_search(order="cheap"))
prices = [r["min_price"] for r in rows if r["min_price"]]
check("مرتب‌سازی ارزان‌ترین: قیمت‌ها صعودی‌اند", prices == sorted(prices) and len(rows) >= 3,
      [r["min_price"] for r in rows][:6])
row_city = ctx(lambda: do_search(city=1))
check("فیلتر شهر فقط همان شهر را می‌دهد",
      all(r["city_id"] == 1 for r in row_city) and bool(row_city), len(row_city))
pid = BIZ[full_slug]["province_id"]
row_prov = ctx(lambda: do_search(prov=pid))
check("فیلتر استان فقط همان استان را می‌دهد",
      bool(row_prov) and all(r["province_id"] == pid for r in row_prov),
      [(r["name"], r["prov_name"]) for r in row_prov])
did = BIZ[full_slug]["district_id"]
row_dist = ctx(lambda: do_search(dist=did))
check("فیلتر محله (حوزهٔ کاری شهر) فقط همان محله را می‌دهد",
      bool(did) and bool(row_dist) and all(r["district_id"] == did for r in row_dist),
      [(r["name"], r["district_id"]) for r in row_dist])
check("محله تنگ‌تر از شهر است؛ کسب‌وکار محلهٔ دیگر حذف می‌شود",
      len(row_dist) < len(row_city) and BIZ[full_slug]["name"] in [r["name"] for r in row_dist],
      (len(row_dist), len(row_city)))
body = c.get(f"/jostojoo?district={did}").get_data(as_text=True)
check("محله از طریق URL هم فیلتر می‌شود",
      body.count('class="biz lift"') == len(row_dist)
      and BIZ[full_slug]["name"] in body
      and BIZ["dental-plus"]["name"] not in body,
      (body.count('class="biz lift"'), len(row_dist)))
body = c.get("/jostojoo").get_data(as_text=True)
check("گزینهٔ محله در فهرست فیلترها هست", 'name="district"' in body and "پاسداران" in body)
check("دسته فقط یک کنترل دارد (نه select و نه chip هم‌زمان)", 'name="cat"' not in body
      and 'href="/jostojoo?cat=' in body.replace("&amp;", "&"))
check("برای مرورگر بدون جاوااسکریپت، دکمهٔ اعمال فیلتر هست",
      "<noscript>" in body and "اعمال فیلترها" in body)
body = c.get(f"/jostojoo?district={did}&city={BIZ[full_slug]['city_id']}").get_data(as_text=True)
pill_zone = re.search(r"فیلترهای فعال.*?</div>\s*</div>", body, re.S)
pill_html = pill_zone.group(0) if pill_zone else ""
check("فیلترهای فعال روی صفحه برچسب برداشتنی می‌شوند",
      pill_html.count("✕") == 2 and "پاک کردن همه" in pill_html, pill_html[:200])
check("برچسب هر فیلتر، فقط همان فیلتر را از URL برمی‌دارد",
      f"district={did}" not in re.search(r'href="([^"]*)"[^>]*>\s*پاسداران', pill_html).group(1)
      and f"city={BIZ[full_slug]['city_id']}" in re.search(r'href="([^"]*)"[^>]*>\s*پاسداران', pill_html).group(1),
      pill_html[:300])
check("برچسب‌ها نتیجه را عوض نمی‌کنند؛ همان کسب‌وکار لیست است",
      body.count('class="biz lift"') == len(row_dist))
check("فرم نتایج برای فیلتر آب‌وهوایی علامت‌گذاری شده",
      "data-cascade" in body and body.count('data-prov="') >= 20, body.count('data-prov="'))
home_body = c.get("/").get_data(as_text=True)
check("فیلتر استان←شهر صفحهٔ خانه هم به هم وابسته است",
      "data-cascade" in home_body and 'data-prov="' in home_body)
r = c.get("/static/js/filters.js")
check("اسکریپت فیلترها سرو می‌شود", r.status_code == 200 and b"dropHidden" in r.data, r.status_code)
body = c.get("/jostojoo?q=نمی‌خواهیم-چیزی-نباشد").get_data(as_text=True)
check("جست‌وجوی بی‌نتیجه پیام راهنما دارد", "یافت نشد" in body or "نتیجه" in body)
body = c.get("/jostojoo?order=best").get_data(as_text=True)
check("بدون کوئری، همهٔ کسب‌وکارهای فعال لیست می‌شوند",
      body.count('class="biz lift"') >= 5, body.count('class="biz lift"'))

print("\n── ۳) ورود با OTP")
cu = new_client()
r = login(cu, "09391112233")
check("ورود کاربر تازه با کد ۱۲۳۴۵", r.status_code == 200 and logged_in(cu))
uid = ctx(lambda: D.scalar("SELECT id FROM users WHERE mobile='09391112233'", (), None))
check("کاربر با نقش customer ساخته شد", bool(uid) and ctx(
    lambda: D.scalar("SELECT role FROM users WHERE id=?", (uid,), "")) == "customer")
bad = new_client()
bad.post("/vorood", data={"mobile": "09391112233"})
r = bad.post("/tayid", data={"mobile": "09391112233", "code": "00000"}, follow_redirects=True)
check("کد اشتباه وارد پنل نمی‌شود", not logged_in(bad) and "صحیح نیست" in r.get_data(as_text=True))
r = new_client().post("/tayid", data={"mobile": "09397778899", "code": "12345"}, follow_redirects=True)
check("بدون درخواست کد، ورود انجام نمی‌شود", "کدی ارسال نشده" in r.get_data(as_text=True))

print("\n── ۴) رزرو با پرداخت کامل → قطعی")
day, slots = first_free_day(full_id, full_svc)
d1, t1 = day, slots[0]
loc, body, _ = book(cu, full_slug, full_svc, d1, t1)
tok = gateway_of(loc)
check("هدایت به درگاه پرداخت", bool(tok), loc)
check("صفحهٔ درگاه مبلغ و نام کسب‌وکار را نشان می‌دهد",
      BIZ[full_slug]["name"] in body and "درگاه" in body)
n_pending = ctx(lambda: D.scalar("SELECT COUNT(*) FROM appointments WHERE status='pending_payment'", (), 0))
check("پیش از پرداخت نوبت «در انتظار پرداخت» است", n_pending == 1, n_pending)
loc, body = pay(cu, tok, "success")
check("پیام «نوبت شما قطعی شد»", "قطعی شد" in body, flash_of(cu, "/pankack"))
a1 = appt_at(full_id, day, t1, "confirmed")
check("نوبت confirmed و pay_status=paid",
      a1 and a1["status"] == "confirmed" and a1["pay_status"] == "paid",
      a1 and (a1["status"], a1["pay_status"]))
check("مبلغ پرداختی = قیمت سرویس", a1 and a1["paid_amount"] == SVC[full_id][0]["price"],
      a1 and a1["paid_amount"])
tx1 = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE appointment_id=? AND kind='payment'",
                             (a1["id"],), one=True)) if a1 else None
check("تراکنش موفق با کد پیگیری ثبت شد", tx1 and tx1["status"] == "success" and tx1["trace"],
      tx1 and tx1["status"])
lk = ctx(lambda: D.query_d("SELECT * FROM slot_locks WHERE business_id=? AND date=? AND start_hm=?",
                           (full_id, day, t1), one=True))
check("قفل زمان پس از پرداخت ماندگار (booked) شد", lk and lk["status"] == "booked", lk and lk["status"])
sms = ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log", (), 0))
check("پیامک تأیید برای مشتری و کسب‌وکار ثبت شد", sms >= 2, sms)
body = cu.get(f"/nobat/{a1['id']}").get_data(as_text=True)
check("صفحهٔ نوبت: جزئیات، کد پیگیری و قانون کنسلی", a1["code"] in body and "کنسلی" in body)

print("\n── ۵) رزرو هم‌زمان یک زمان توسط کاربر دوم")
cu2 = new_client()
login(cu2, "09392223344")
loc, body, _ = book(cu2, full_slug, full_svc, day, t1)
check("زمان رزروشده قابل رزرو دوم نیست", not gateway_of(loc) and ("رزرو" in body or "زمان دیگری" in body),
      loc or body[:120])
check("فقط یک نوبت زنده برای آن زمان", live_count(full_id, day, t1) == 1, live_count(full_id, day, t1))

print("\n── ۶) قفل سبد و رقابت هم‌زمان")
t2 = slots[1]
loc, body, _ = book(cu, full_slug, full_svc, day, t2)
tok2 = gateway_of(loc)
lock = ctx(lambda: D.query_d("SELECT * FROM slot_locks WHERE business_id=? AND date=? AND start_hm=?",
                             (full_id, day, t2), one=True))
check("قفل hold برای کاربر اول ساخته شد", lock and lock["status"] == "hold", lock and lock["status"])
check("نوبت در انتظار پرداخت ساخته شد", lock and live_count(full_id, day, t2) == 1,
      live_count(full_id, day, t2))
loc2, body2, _ = book(cu2, full_slug, full_svc, day, t2)
check("کاربر دوم پیام سبد کاربر دیگر را می‌گیرد", "سبد" in body2 or "هم‌زمان" in body2,
      (flash_of(cu2, f"/k/{full_slug}/rezerv?date={day}") or body2[:150]))
check("زمان قفل‌شده در تقویم کاربر دوم «رزروشده» است",
      t2 in [s["hm"] for s in ctx(lambda: B.slot_times(full_id, day, SVC[full_id][0]["duration_min"]))
             if s.get("taken")])
check("نوبت دومی برای همان زمان ساخته نشد", live_count(full_id, day, t2) == 1,
      live_count(full_id, day, t2))

print("\n── ۷) پرداخت ناموفق → نوبتی قطعی نمی‌شود و زمان آزاد می‌گردد")
d3, s3 = first_free_day(full_id, full_svc)
taken3 = [s for s in s3 if s not in (t1, t2)][0]
loc, body, _ = book(cu, full_slug, full_svc, d3, taken3)
tok3 = gateway_of(loc)
check("درگاه برای رزرو تازه باز شد", bool(tok3), loc)
loc, body = pay(cu, tok3, "failed")
check("پیام پرداخت ناموفق", "ناموفق" in body, flash_of(cu, "/pankack"))
a3 = appt_at(full_id, d3, taken3, "expired")
check("نوبت expired شد و confirmed نیست", a3 and a3["status"] == "expired", a3 and a3["status"])
tx3 = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE appointment_id=? AND kind='payment'",
                             (a3["id"],), one=True)) if a3 else None
check("تراکنش failed با پیام قابل پیگیری", tx3 and tx3["status"] == "failed" and tx3["message"],
      tx3 and tx3["status"])
lk3 = ctx(lambda: D.query_d("SELECT * FROM slot_locks WHERE business_id=? AND date=? AND start_hm=?",
                             (full_id, d3, taken3), one=True))
check("زمان پس از پرداخت ناموفق آزاد شد", lk3 is None or lk3["status"] == "released", lk3 and lk3["status"])
check("همان ساعت دوباره قابل رزرو است (بازاستفاده از رکورد released)",
      bool(gateway_of(book(cu, full_slug, full_svc, d3, taken3)[0])))

print("\n── ۸) انصراف در درگاه")
d4, s4 = first_free_day(full_id, full_svc)
t4 = [s for s in s4 if s not in (t1, t2, taken3)][0]
loc, body, _ = book(cu, full_slug, full_svc, d4, t4)
loc, body = pay(cu, gateway_of(loc), "cancelled")
check("پیام انصراف از پرداخت", "لغو" in body, flash_of(cu, "/pankack"))
check("نوبت منقضی شد", ctx(lambda: D.scalar(
    "SELECT status FROM appointments WHERE business_id=? AND date=? AND start_hm=?",
    (full_id, d4, t4), "")) == "expired")

print("\n── ۹) بیعانه (deposit) و پرداخت رایگان")
d5, s5 = first_free_day(dep_id, dep_svc)
loc, body, _ = book(cu, dep_slug, dep_svc, d5, s5[0])
a5 = appt_at(dep_id, d5, s5[0], "pending_payment")
expected = min(BIZ[dep_slug]["deposit_amount"], SVC[dep_id][0]["price"])
check("پرداخت_now = بیعانه (نه کل قیمت)", a5 and a5["payable_now"] == expected,
      a5 and (a5["payable_now"], expected))
check("صفحهٔ درگاه مبلغ بیعانه را نشان می‌دهد", fa_money(expected) in body,
      (expected, body.count(fa_money(expected))))
loc, body = pay(cu, gateway_of(loc), "success")
check("نوبت بیعانه‌ای قطعی شد", ctx(lambda: D.scalar(
    "SELECT status FROM appointments WHERE id=?", (a5["id"],), "")) == "confirmed")

d6, s6 = first_free_day(free_id, free_svc)
loc, body, _ = book(cu, free_slug, free_svc, d6, s6[0])
check("کسب‌وکار رایگان به درگاه نمی‌رود", not gateway_of(loc) and "/nobat/" in loc, loc)
a6 = appt_at(free_id, d6, s6[0], "confirmed")
check("نوبت بلافاصله confirmed و پرداخت‌شده",
      a6 and a6["status"] == "confirmed" and a6["pay_status"] == "paid", a6 and a6["status"])
check("پیام «بدون پرداخت آنلاین»", "قطعی" in body, flash_of(cu, "/pankack"))

print("\n── ۱۰) کنسلی، بازپرداخت مستقل و آزادشدن زمان")
prev = ctx(lambda: B.cancellation_preview(a1))
print("      پیش‌بینی کنسلی:", json.dumps(prev, ensure_ascii=False))
loc, body = None, None
r = cu.post(f"/nobat/{a1['id']}/laghv", data={"reason": "تست آزمون"}, follow_redirects=True)
body = r.get_data(as_text=True)
a1x = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a1["id"],), one=True))
check("نوبت cancelled شد", a1x and a1x["status"] == "cancelled", a1x and a1x["status"])
rf = ctx(lambda: D.query_d("SELECT * FROM refunds WHERE appointment_id=?", (a1["id"],), one=True))
if prev["refund_percent"] > 0:
    check("رکورد بازپرداخت جدا از نوبت ایجاد شد", bool(rf))
    check("مبلغ بازپرداخت = درصد × پرداختی",
          rf and rf["amount"] == int(round(a1x["paid_amount"] * rf["percent"] / 100.0)),
          rf and (rf["amount"], a1x["paid_amount"], rf["percent"]))
    txr = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE appointment_id=? AND kind='refund'",
                                 (a1["id"],), one=True))
    check("تراکنش بازپرداخت مستقل (reference جدا) ثبت شد",
          txr and txr["reference"] != tx1["reference"], txr and txr["reference"])
else:
    check("خارج از بازهٔ بازپرداخت: رکورد بازپرداخت ساخته نمی‌شود", rf is None)
slots_now = ctx(lambda: B.slot_times(full_id, day, SVC[full_id][0]["duration_min"]))
check("ساعت نوبت لغوشده دیگر «رزروشده» نشان نمی‌دهد",
      t1 not in [s["hm"] for s in slots_now if s.get("taken")])
loc, body, _ = book(cu, full_slug, full_svc, day, t1)
check("ساعت لغوشده دوباره قابل رزرو است", bool(gateway_of(loc)), loc)
check("تاریخچهٔ نوبت لغوشده می‌ماند و فقط یک نوبت زنده آن ساعت است",
      live_count(full_id, day, t1) == 1 and ctx(lambda: D.scalar(
          "SELECT COUNT(*) FROM appointments WHERE business_id=? AND date=? AND start_hm=? "
          "AND status='cancelled'", (full_id, day, t1), 0)) == 1,
      (live_count(full_id, day, t1),))
check("لغو دوباره مجاز نیست",
      "قبلاً لغو" in cu.post(f"/nobat/{a1['id']}/laghv", data={"reason": "دوباره"},
                            follow_redirects=True).get_data(as_text=True))

print("\n── ۱۱) کنسلی با بازپرداخت کامل (قانون ۴۸ ساعتهٔ کلینیک دندان‌پزشکی)")
den_slug, den_id = "dental-plus", BIZ["dental-plus"]["id"]
den_svc = SVC[den_id][0]["id"]
d8, s8 = first_free_day(den_id, den_svc, min_off=3)
loc, body, _ = book(cu, den_slug, den_svc, d8, s8[0])
a8 = appt_at(den_id, d8, s8[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
a8 = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a8["id"],), one=True))
prev8 = ctx(lambda: B.cancellation_preview(a8))
check("پیش‌بینی کنسلی: ۱۰۰٪ از پرداختی (بیعانه) برگردانده می‌شود",
      prev8["refund_percent"] == 100 and prev8["refund_amount"] == a8["paid_amount"]
      and a8["paid_amount"] == BIZ[den_slug]["deposit_amount"],
      (prev8, a8["paid_amount"]))
r = cu.post(f"/nobat/{a8['id']}/laghv", data={"reason": "تست بازپرداخت"}, follow_redirects=True)
body8 = r.get_data(as_text=True)
rf8 = ctx(lambda: D.query_d("SELECT * FROM refunds WHERE appointment_id=?", (a8["id"],), one=True))
a8x = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a8["id"],), one=True))
tx8 = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE appointment_id=? AND kind='refund'",
                             (a8["id"],), one=True))
check("رکورد بازپرداخت با درصد و مبلغ درست",
      rf8 and rf8["percent"] == 100 and rf8["amount"] == a8["paid_amount"],
      rf8 and (rf8["amount"], a8["paid_amount"]))
check("تراکنش بازپرداخت مستقل (reference جدا) ثبت شد", tx8 and tx8["reference"], tx8)
check("نوبت لغو و pay_status آن refunded شد",
      a8x["status"] == "cancelled" and a8x["pay_status"] == "refunded",
      a8x and (a8x["status"], a8x["pay_status"]))
check("پیام مبلغ بازپرداخت به کاربر نشان داده می‌شود",
      "بازپرداخت" in body8 and fa_money(a8["paid_amount"]) in body8,
      flash_of(cu, f"/nobat/{a8['id']}")[:200])
check("ساعت پس از کنسلی آزاد شد", live_count(den_id, d8, s8[0]) == 0)
bal = ctx(lambda: B.business_balance(den_id))
check("در گزارش مالی کسب‌وکار، بازپرداخت از درآمد کم و کارمزد اعمال می‌شود",
      bal["refunds"] == rf8["amount"] and bal["platform_fee"] ==
      int(round(bal["gross"] * float(bal["fee_percent"]) / 100.0)) and bal["receivable"] >= 0, bal)

print("\n── ۱۲) مهلت قفل رو به اتمام: انقضای خودکار")
expired_iso = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
with A.app_context():
    D.execute("UPDATE slot_locks SET expires_at=? WHERE business_id=? AND date=? AND start_hm=?",
              (expired_iso, full_id, day, t2))
    n = B.expire_pending_appointments()
check("نوبت پرداخت‌نشده پس از پایان مهلت منقضی می‌شود",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE business_id=? AND date=? AND start_hm=?",
                           (full_id, day, t2), "")) == "expired", n)

print("\n── ۱۳) جابه‌جایی زمان نوبت")
d7, s7 = first_free_day(full_id, full_svc)
loc, body, _ = book(cu, full_slug, full_svc, d7, s7[0])
tok7 = gateway_of(loc)
a7 = appt_at(full_id, d7, s7[0], "pending_payment")
check("نوبتِ جابه‌جایی در انتظار پرداخت ساخته شد", bool(a7) and a7["status"] == "pending_payment",
      a7 and a7["status"])
loc, body = pay(cu, tok7, "success")
a7 = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a7["id"],), one=True))
check("نوبت برای جابه‌جایی قطعی شد", a7["status"] == "confirmed",
      (a7["status"], flash_of(cu, "/pankack")[:160]))
new_hm = s7[1] if len(s7) > 1 else s7[0]
r = cu.post(f"/nobat/{a7['id']}/javabeja", data={"date": d7, "time": s7[0]}, follow_redirects=True)
check("جابه‌جایی به همان ساعت فعلی رد می‌شود", "همین روز و ساعت" in r.get_data(as_text=True),
      flash_of(cu, f"/nobat/{a7['id']}")[:160])
r = cu.post(f"/nobat/{a7['id']}/javabeja", data={"date": d7, "time": new_hm}, follow_redirects=True)
a7x = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a7["id"],), one=True))
check("نوبت به ساعت جدید جابه‌جا شد", a7x and a7x["start_hm"] == new_hm,
      (a7x and a7x["start_hm"], new_hm, flash_of(cu, f"/nobat/{a7['id']}")[:200]))
check("پیام جابه‌جایی نمایش داده می‌شود", "جابه‌جا شد" in r.get_data(as_text=True))
check("ساعت قبلی آزاد شد",
      s7[0] not in [x["hm"] for x in ctx(lambda: B.slot_times(full_id, d7, SVC[full_id][0]["duration_min"]))
                    if x.get("taken")])

print("\n── ۱۴) پنل‌ها و تب‌ها")
cc = new_client()
login(cc, "09301234567")
check("پنل مشتری", cc.get("/pankack").status_code == 200)
cb = new_client()
login(cb, "09121112233")
bad = [t for t in ["today", "services", "hours", "rules", "profile", "all"]
       if cb.get(f"/kasbkar/{full_slug}?tab={t}").status_code != 200]
check("پنل کسب‌وکار با همهٔ تب‌ها", not bad, bad)
body = cb.get(f"/kasbkar/{full_slug}").get_data(as_text=True)
check("بخش مالی: سهم پلتفرم و تسویه نمایش داده می‌شود", "کارمزد" in body or "تسویه" in body)
cm = new_client()
login(cm, "09120000000")
bad = [t for t in ["businesses", "users", "cats", "money", "appointments", "sms", "settings", "log"]
       if cm.get(f"/modiriat?tab={t}").status_code != 200]
check("پنل مدیریت با همهٔ تب‌ها", not bad, bad)

print("\n── ۱۵) جداسازی دادهٔ کسب‌وکارها و دسترسی‌ها")
o = new_client()
login(o, "09123334455")   # مالک دنتال‌پرس
check("مالک کسب‌وکاری به پنل کسب‌وکار دیگر دسترسی ندارد",
      o.get(f"/kasbkar/{full_slug}").status_code == 403)
check("مشتری به پنل کسب‌وکار نمی‌رسد", cu.get("/kasbkar").status_code in (302, 403))
check("کاربر ناشناس به پنل نمی‌رسد", new_client().get("/pankack").status_code == 302)
check("نوبت کاربر دیگر قابل مشاهده نیست", new_client().get(f"/nobat/{a7['id']}").status_code in (302, 403))
check("لغو نوبت کاربر دیگر مجاز نیست",
      o.post(f"/nobat/{a7['id']}/laghv", data={"reason": "دخالت"}).status_code in (302, 403))

print("\n── ۱۶) صندوق پیامک توسعه")
r = cu.get("/sms-box")
check("صندوق پیامک باز می‌شود", r.status_code == 200 and "نوبتین" in r.get_data(as_text=True))

# ============================================================ مسیرهای نوشتن (POST)
print("\n── ۱۷) ثبت کسب‌وکار تازه و تأیید مدیر")
new_mob = "09395556677"
nb = new_client()
login(nb, new_mob)
uid_new = ctx(lambda: D.scalar("SELECT id FROM users WHERE mobile=?", (new_mob,), None))
loc = nb.get("/kasbkar").headers.get("Location", "")
check("لینک «کسب‌وکار خود را ثبت کنید» مشتری را به بن‌بست ۴۰۳ نمی‌اندازد",
      nb.get("/kasbkar").status_code == 302 and "/kasbkar/new" in loc, loc)
r = nb.get("/kasbkar/new")
check("مشتری تازه‌وارد فرم ثبت کسب‌وکار را می‌بیند", r.status_code == 200
      and 'name="slug"' in r.get_data(as_text=True), r.status_code)
check("تا قبل از ثبت، نقش کاربر هنوز customer است",
      ctx(lambda: D.scalar("SELECT role FROM users WHERE id=?", (uid_new,), "")) == "customer")
cm.post(f"/modiriat/user/{uid_new}/business", follow_redirects=True)
check("مدیر همچنان می‌تواند نقش را دستی business کند",
      ctx(lambda: D.scalar("SELECT role FROM users WHERE id=?", (uid_new,), "")) == "business")
ctx(lambda: D.execute("UPDATE users SET role='customer' WHERE id=?", (uid_new,)))
cat1 = ctx(lambda: D.scalar("SELECT id FROM categories WHERE is_active=1 ORDER BY sort", (), 0))
city1 = ctx(lambda: D.scalar("SELECT id FROM cities ORDER BY id", (), 0))
prov1 = ctx(lambda: D.scalar("SELECT province_id FROM cities WHERE id=?", (city1,), 0))
reg = {"name": "استودیو تست نوبتین", "slug": "test-studio", "category": cat1, "province": prov1,
       "city": city1, "district": "محلهٔ تست", "phone": "021-99887766",
       "about": "کسب‌وکاری که فقط در آزمون ساخته می‌شود.", "hue": 210,
       "payment_mode": "full", "deposit": 0, "hold_minutes": 8,
       "rule_hours": ["48", "2"], "refund_48": "100", "refund_2": "0"}
r = nb.post("/kasbkar/new", data=dict(reg, name=""), follow_redirects=True)
check("فرم بدون نام رد می‌شود", "نام کسب‌وکار" in r.get_data(as_text=True),
      flash_of(nb, "/kasbkar/new")[:140])
r = nb.post("/kasbkar/new", data=dict(reg, slug=""), follow_redirects=True)
check("فرم بدون نشانی (slug) رد می‌شود", "نشانی اینترنتی" in r.get_data(as_text=True))
r = nb.post("/kasbkar/new", data=dict(reg, slug="dental-plus"), follow_redirects=True)
check("slug تکراری رد می‌شود", "قبلاً استفاده شده" in r.get_data(as_text=True))
r = nb.post("/kasbkar/new", data=dict(reg, payment_mode="deposit", deposit="0"),
            follow_redirects=True)
check("ثبت با حالت «پیش‌پرداخت» و بیعانهٔ صفر رد می‌شود", "بیعانه" in r.get_data(as_text=True))
check("و هیچ کسب‌وکاری ساخته نشد",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM businesses WHERE slug='test-studio'", (), 0)) == 0)
r = nb.post("/kasbkar/new", data=reg, follow_redirects=True)
check("ثبت کسب‌وکار: پیام «ثبت شد» و ورود به پنل", "کسب‌وکار ثبت شد" in r.get_data(as_text=True),
      flash_of(nb, "/kasbkar/test-studio")[:160])
check("با ثبت اولین کسب‌وکار، نقش مشتری خودکار business می‌شود",
      ctx(lambda: D.scalar("SELECT role FROM users WHERE id=?", (uid_new,), "")) == "business",
      ctx(lambda: D.scalar("SELECT role FROM users WHERE id=?", (uid_new,), "")))
check("پیوند «پنل کسب‌وکار» در سربرگ ظاهر شد", "پنل کسب‌وکار" in r.get_data(as_text=True))
check("تبدیل نقش در ردپای اقدامات ثبت شد", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM audit_log WHERE action='user_become_business' AND target=?",
    (f"user:{uid_new}",), 0)) == 1)
check("کاربر واردنشده همچنان به صفحهٔ ورود می‌رود (نه فرم ثبت)",
      "/vorood" in new_client().get("/kasbkar").headers.get("Location", ""))
nbiz = ctx(lambda: D.query_d("SELECT * FROM businesses WHERE slug='test-studio'", (), one=True))
sl, nid = "test-studio", nbiz["id"]
check("مالک، وضعیت pending و شهر/دسته درست ذخیره شد",
      nbiz["owner_id"] == uid_new and nbiz["status"] == "pending"
      and nbiz["city_id"] == city1 and nbiz["category_id"] == cat1, nbiz and
      (nbiz["owner_id"], nbiz["status"], nbiz["city_id"]))
check("قوانین کنسلی فرم به JSON مرتب‌شده تبدیل شد",
      json.loads(nbiz["cancel_policy"]) == [{"min_hours": 48, "refund_percent": 100},
                                            {"min_hours": 2, "refund_percent": 0}],
      nbiz["cancel_policy"])
check("مهلت قفل سبد از فرم ثبت شد", nbiz["hold_minutes"] == 8, nbiz["hold_minutes"])
check("کمیسیون کسب‌وکار تازه از تنظیمات پلتفرم پر می‌شود (قولِ فرم دروغ نیست)",
      float(nbiz["commission_percent"]) == float(ctx(lambda: D.setting("platform_fee_percent", "5"))),
      nbiz["commission_percent"])
check("ساعات کاری پیش‌فرض برای هر ۷ روز ساخته شد (جمعه جا نمی‌ماند)",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM working_hours WHERE business_id=?", (nid,), 0)) == 7
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM working_hours WHERE business_id=? AND weekday=6",
                               (nid,), 0)) == 1)
fridays = [(date.today() + timedelta(days=k)) for k in range(21)
           if jweekday(date.today() + timedelta(days=k)) == 6]
fri_slots = ctx(lambda: sum(len([s for s in B.slot_times(nid, d.isoformat(), 30)
                                 if not s.get("past") and not s.get("taken")]) for d in fridays))
check("تقویم زمان‌های آزاد، جمعه‌ها را هم برای کسب‌وکار تازه نشان می‌دهد", fri_slots > 0, fri_slots)
check("محلهٔ تازه ساخته و به کسب‌وکار وصل شد",
      bool(nbiz["district_id"]) and ctx(lambda: D.scalar(
          "SELECT name FROM districts WHERE id=?", (nbiz["district_id"],), "")) == "محلهٔ تست")
check("صفحهٔ عمومی کسب‌وکار تأییدنشده ۴۰۴ است", new_client().get("/k/test-studio").status_code == 404)
check("کسب‌وکار pending در جست‌وجو نمایش داده نمی‌شود",
      "test-studio" not in [r2["slug"] for r2 in ctx(lambda: do_search())])
r = nb.get("/kasbkar/test-studio", follow_redirects=True)
check("مالک، پنل کسب‌وکار تأییدنشده را می‌بیند", "استودیو تست" in r.get_data(as_text=True))
cm.post(f"/modiriat/biz/{nid}/active", follow_redirects=True)
check("تأیید مدیر: وضعیت active شد",
      ctx(lambda: D.scalar("SELECT status FROM businesses WHERE id=?", (nid,), "")) == "active")
check("پس از تأیید، صفحهٔ عمومی باز می‌شود", new_client().get("/k/test-studio").status_code == 200)
check("پس از تأیید در جست‌وجو می‌آید",
      "test-studio" in [r2["slug"] for r2 in ctx(lambda: do_search(order="new"))],
      [r2["slug"] for r2 in ctx(lambda: do_search())])

print("\n── ۱۸) خدمات: افزودن، ویرایش، غیرفعال‌سازی")
r = nb.post(f"/kasbkar/{sl}/service", data={"title": "بررسی دوره‌ای", "price": 400000,
             "duration": 60, "description": "آزمون"}, follow_redirects=True)
svc_new = ctx(lambda: D.query_d("SELECT * FROM services WHERE business_id=? AND title=?",
                                 (nid, "بررسی دوره‌ای"), one=True))
check("سرویس تازه ذخیره شد", svc_new and svc_new["price"] == 400000
      and svc_new["duration_min"] == 60 and svc_new["is_active"] == 1, svc_new)
check("پیام «سرویس جدید اضافه شد»", "سرویس جدید" in r.get_data(as_text=True),
      flash_of(nb, f"/kasbkar/{sl}")[:140])
r = nb.post(f"/kasbkar/{sl}/service", data={"title": "  ", "price": 1}, follow_redirects=True)
check("سرویس بی‌عنوان رد می‌شود", "عنوان سرویس لازم است" in r.get_data(as_text=True))
check("و شمار سرویس‌ها زیاد نمی‌شود", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM services WHERE business_id=?", (nid,), 0)) == 1)
sid_new = svc_new["id"]
nb.post(f"/kasbkar/{sl}/service", data={"id": sid_new, "title": "بررسی تخصصی", "price": 550000,
          "duration": 45, "buffer": 15, "description": "ویرایش‌شده", "is_active": "on"},
        follow_redirects=True)
s2 = ctx(lambda: D.query_d("SELECT * FROM services WHERE id=?", (sid_new,), one=True))
check("ویرایش سرویس: عنوان/قیمت/مدت/فاصلهٔ بین نوبت‌ها عوض شد",
      s2["title"] == "بررسی تخصصی" and s2["price"] == 550000 and s2["duration_min"] == 45
      and s2["buffer_min"] == 15, s2)
check("ویرایش سرویس جدید نمی‌سازد", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM services WHERE business_id=?", (nid,), 0)) == 1)
check("سرویس در صفحهٔ رزرو لیست می‌شود", "بررسی تخصصی" in new_client().get(
    f"/k/{sl}").get_data(as_text=True))
r = cu.post(f"/kasbkar/{sl}/service", data={"id": sid_new, "title": "دخالت", "price": 1},
            follow_redirects=True)
check("مشتری نمی‌تواند سرویس کسب‌وکار دیگر را تغییر دهد", r.status_code == 403)
check("و سرویس دست‌نخورده مانده است", ctx(lambda: D.scalar(
    "SELECT title FROM services WHERE id=?", (sid_new,), "")) == "بررسی تخصصی")
# غیرفعال/فعال کردن از طریق همان فرم ویرایش (تیک «فعال» برداشته می‌شود)
nb.post(f"/kasbkar/{sl}/service", data={"id": sid_new, "title": "بررسی تخصصی", "price": 550000,
                                        "duration": 45, "buffer": 15}, follow_redirects=True)
check("غیرفعال‌سازی: is_active=0", ctx(lambda: D.scalar(
    "SELECT is_active FROM services WHERE id=?", (sid_new,), 1)) == 0)
check("سرویس غیرفعال در صفحهٔ عمومی نیست", "بررسی تخصصی" not in new_client().get(
    f"/k/{sl}").get_data(as_text=True))
r = cu.post(f"/k/{sl}/zan", data={"date": date.today().isoformat(), "service": sid_new,
                                 "time": "10:00"}, follow_redirects=True)
check("رزرو مستقیم سرویس غیرفعال هم رد می‌شود", "سرویس نامعتبر است" in r.get_data(as_text=True))
nb.post(f"/kasbkar/{sl}/service", data={"id": sid_new, "title": "بررسی تخصصی", "price": 550000,
                                        "duration": 45, "buffer": 15, "is_active": "on"},
        follow_redirects=True)
check("دوباره فعال شد", ctx(lambda: D.scalar(
    "SELECT is_active FROM services WHERE id=?", (sid_new,), 0)) == 1)

print("\n── ۱۹) ساعات کاری و تعطیلی روز: اثر روی تقویم")
wd_target = jweekday(date.today() + timedelta(days=3))
next_wd = jweekday(date.today() + timedelta(days=4))
r = nb.post(f"/kasbkar/{sl}/hours", data={f"on{wd_target}": "on", f"start{wd_target}": "12:00",
             f"end{wd_target}": "11:00", f"slot{wd_target}": "30"}, follow_redirects=True)
check("بازهٔ زمانی نامعتبر (پایان < شروع) رد می‌شود", "معتبر نیست" in r.get_data(as_text=True))
# کسب‌وکار تازه با ساعت پیش‌فرض ۰۹:۰۰–۱۹:۰۰ ساخته شده؛ فرم بی‌اعتبار نباید چیزی
# پاک یا جایگزین کند (نه ساعت آن روز، نه شش روز دیگر).
check("و ساعت کاری آن روز ذخیره نمی‌شود", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM working_hours WHERE business_id=? AND weekday=? AND start_hm='09:00' "
    "AND end_hm='19:00'", (nid, wd_target), 0)) == 1)
check("و شش روز دیگر هم دست‌نخورده می‌مانند", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM working_hours WHERE business_id=?", (nid,), 0)) == 7)
only = {f"on{wd_target}": "on", f"start{wd_target}": "09:00", f"end{wd_target}": "13:00",
        f"slot{wd_target}": "30"}
r = nb.post(f"/kasbkar/{sl}/hours", data=only, follow_redirects=True)
check("ذخیرهٔ ساعات تک‌روزه", "ساعات کاری ذخیره شد" in r.get_data(as_text=True))
check("فقط همان روز باز است", ctx(lambda: [h["weekday"] for h in D.query_d(
    "SELECT weekday FROM working_hours WHERE business_id=?", (nid,))]) == [wd_target])
day_open = date.today() + timedelta(days=3)
day_closed = date.today() + timedelta(days=4)
free_open = [s["hm"] for s in ctx(lambda: B.slot_times(nid, day_open.isoformat(), 45))
             if not s.get("past") and not s.get("taken")]
check("آن روز زمان‌های آزاد دارد و در روز تعطیل هیچ ساعتی نیست",
      bool(free_open) and ctx(lambda: B.slot_times(nid, day_closed.isoformat(), 45)) == [],
      (free_open[:3], day_open, day_closed))
check("تقویم ماهانه برای روز تعطیل صفر زمان آزاد نشان می‌دهد",
      ctx(lambda: B.month_availability(nid, to_jdate(day_closed).year,
                                       to_jdate(day_closed).month, 45).get(day_closed.isoformat())) == 0)
allweek = {}
for w in range(7):
    allweek[f"on{w}"] = "on"
    allweek[f"start{w}"] = "09:00"
    allweek[f"end{w}"] = "20:00"
    allweek[f"slot{w}"] = "30"
nb.post(f"/kasbkar/{sl}/hours", data=allweek, follow_redirects=True)
check("هفتهٔ کامل با جمعه فعال شد", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM working_hours WHERE business_id=?", (nid,), 0)) == 7)
d_block, s_block = first_free_day(nid, sid_new, min_off=1)
loc, body, _ = book(cu, sl, sid_new, d_block, s_block[0])
a_block = appt_at(nid, d_block, s_block[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
r = nb.post(f"/kasbkar/{sl}/block", data={"date": d_block, "reason": "مراسه داخلی"},
            follow_redirects=True)
txt = r.get_data(as_text=True)
check("تعطیل‌کردن روز، شمار نوبت‌های فعال آن روز را هم می‌گوید",
      "تعطیل شد" in txt and "نوبت فعال" in txt,
      flash_of(nb, f"/kasbkar/{sl}?tab=hours")[:200])
check("در روز تعطیل هیچ ساعت آزادی نیست", ctx(lambda: B.slot_times(nid, d_block, 45)) == [])
check("تقویم رزرو پیام «تعطیل» می‌دهد",
      ctx(lambda: B.is_blocked(nid, d_block)) is True and new_client().get(
          f"/k/{sl}/rezerv?date={d_block}&service={sid_new}").status_code == 200)
r = nb.post(f"/kasbkar/{sl}/block", data={"date": d_block, "_remove": "1"}, follow_redirects=True)
check("حذف تعطیلی: ساعت‌ها برمی‌گردند", "تعطیلی آن روز حذف شد" in r.get_data(as_text=True)
      and bool(ctx(lambda: B.slot_times(nid, d_block, 45))))

print("\n── ۲۰) قوانین کنسلی: نوبت‌های قبلی با قانون لحظهٔ رزرو می‌مانند")
d_a, s_a = first_free_day(nid, sid_new, min_off=3)          # بیش از ۴۸ ساعت آینده
loc, body, _ = book(cu, sl, sid_new, d_a, s_a[0])
a_a = appt_at(nid, d_a, s_a[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
a_a = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a_a["id"],), one=True))
prev_a0 = ctx(lambda: B.cancellation_preview(a_a))
check("در بازهٔ ۴۸ ساعت، پیش‌بینی کنسلی ۱۰۰٪ پرداختی است",
      prev_a0["refund_percent"] == 100 and prev_a0["refund_amount"] == a_a["paid_amount"], prev_a0)
check("قانون لحظهٔ رزرو داخل خود نوبت ذخیره می‌شود",
      json.loads(a_a["cancel_policy"]) == [{"min_hours": 48, "refund_percent": 100},
                                           {"min_hours": 2, "refund_percent": 0}],
      a_a["cancel_policy"])
rules_new = {"rule_hours": ["48"], "refund_48": "0", "payment_mode": "deposit",
             "deposit": "150000", "hold_minutes": "5"}
comm_before = ctx(lambda: D.scalar("SELECT commission_percent FROM businesses WHERE id=?", (nid,), 0.0))
r = nb.post(f"/kasbkar/{sl}/rules", data=dict(rules_new, commission_percent="120"),
           follow_redirects=True)
check("ذخیرهٔ قوانین تازه", "قوانین ذخیره شد" in r.get_data(as_text=True),
      flash_of(nb, f"/kasbkar/{sl}?tab=rules")[:200])
biz_now = ctx(lambda: D.query_d("SELECT * FROM businesses WHERE id=?", (nid,), one=True))
check("بیعانهٔ جدید و حالت پرداخت ذخیره شد",
      biz_now["payment_mode"] == "deposit" and biz_now["deposit_amount"] == 150000,
      biz_now and (biz_now["payment_mode"], biz_now["deposit_amount"]))
check("مهلت قفل از فرم قوانین به‌روز شد", biz_now["hold_minutes"] == 5, biz_now["hold_minutes"])
# کارمزد پلتفرم فیلدی در پنل کسب‌وکار نیست؛ اگر از همین POST خوانده شود، با یک
# درخواست دستی می‌توان سهم سایت را دلخواه کرد. تنها راه مشروع، پنل مدیریت است.
check("کارمزد از POST خودِ کسب‌وکار (حتی ۱۲۰٪) عوض نمی‌شود",
      float(ctx(lambda: D.scalar("SELECT commission_percent FROM businesses WHERE id=?",
                                 (nid,), 0.0))) == float(comm_before),
      (comm_before, biz_now["commission_percent"]))
cm.post(f"/modiriat/biz/{nid}/commission", data={"value": "8"}, follow_redirects=True)
check("کارمزد از پنل مدیریت تعیین می‌شود", float(ctx(lambda: D.scalar(
    "SELECT commission_percent FROM businesses WHERE id=?", (nid,), 0.0))) == 8.0)
r = nb.post(f"/kasbkar/{sl}/rules", data={"payment_mode": "deposit"}, follow_redirects=True)
check("بدون هیچ قانون کنسلی ذخیره نمی‌شود", "حداقل یک قانون" in r.get_data(as_text=True))
r = nb.post(f"/kasbkar/{sl}/rules", data={"rule_hours": ["24"], "refund_24": "50",
             "payment_mode": "deposit", "deposit": "0"}, follow_redirects=True)
check("ذخیرهٔ حالت «پیش‌پرداخت» با بیعانهٔ صفر رد می‌شود", "بیشتر از صفر" in r.get_data(as_text=True))
check("و بیعانه/حالت پرداختِ پیشین دست‌نخورده می‌ماند",
      ctx(lambda: D.scalar("SELECT payment_mode FROM businesses WHERE id=?", (nid,), "")) == "deposit"
      and ctx(lambda: D.scalar("SELECT deposit_amount FROM businesses WHERE id=?", (nid,), 0)) == 150000)
prev_a = ctx(lambda: B.cancellation_preview(a_a))
check("نوبت قبلی با وجود قانون جدید، هنوز ۱۰۰٪ بازپرداخت می‌گیرد (اسنپ‌شات)",
      prev_a["refund_percent"] == 100 and json.loads(biz_now["cancel_policy"]) ==
      [{"min_hours": 48, "refund_percent": 0}], (prev_a, biz_now["cancel_policy"]))
loc, body, _ = book(cu, sl, sid_new, d_a, s_a[1])
a_b = appt_at(nid, d_a, s_a[1], "pending_payment")
check("نوبت تازه با قانون ۰٪ و بیعانهٔ تازه ساخته می‌شود",
      json.loads(a_b["cancel_policy"]) == [{"min_hours": 48, "refund_percent": 0}]
      and a_b["payable_now"] == 150000, a_b and (a_b["cancel_policy"], a_b["payable_now"]))
pay(cu, gateway_of(loc), "success")
a_b = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a_b["id"],), one=True))
check("و مبلغ پرداختی‌اش همان بیعانه است", a_b["paid_amount"] == 150000, a_b["paid_amount"])
r = cu.post(f"/nobat/{a_b['id']}/laghv", data={"reason": "بدون بازپرداخت"}, follow_redirects=True)
fl_b = flash_of(cu, f"/nobat/{a_b['id']}")
check("کنسلی بدون بازپرداخت: هیچ رکورد بازپرداختی ثبت نمی‌شود",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM refunds WHERE appointment_id=?",
                           (a_b["id"],), 0)) == 0)
check("و پیام لغو درست به کاربر نشان داده می‌شود (نه None)",
      "لغو شد" in fl_b and "None" not in fl_b, fl_b[:200])
r = cu.post(f"/nobat/{a_a['id']}/laghv", data={"reason": "با قانون قدیمی"}, follow_redirects=True)
rf_a = ctx(lambda: D.query_d("SELECT * FROM refunds WHERE appointment_id=?", (a_a["id"],), one=True))
check("کنسلی نوبت قدیمی با قانون خودش ۱۰۰٪ بازپرداخت می‌کند",
      rf_a and rf_a["percent"] == 100 and rf_a["amount"] == a_a["paid_amount"],
      rf_a and (rf_a["percent"], rf_a["amount"], a_a["paid_amount"]))

print("\n── ۲۱) اقدامات کسب‌وکار روی نوبت‌ها، یادآوری و پروفایل")
d_c, s_c = first_free_day(nid, sid_new, min_off=1)
loc, body, _ = book(cu, sl, sid_new, d_c, s_c[0])
a_c = appt_at(nid, d_c, s_c[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
a_c = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a_c["id"],), one=True))
r = cu.post(f"/nobat/{a_c['id']}/completed", follow_redirects=True)
check("مشتری نمی‌تواند وضعیت نوبت را خودش «انجام شد» کند", r.status_code == 403)
r = nb.post(f"/nobat/{a_c['id']}/completed", data={}, follow_redirects=True)
check("کسب‌وکار نوبت را انجام‌شده ثبت می‌کند",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (a_c["id"],), "")) == "completed"
      and "انجام‌شده" in r.get_data(as_text=True))
d_e, s_e = first_free_day(nid, sid_new, min_off=1)
loc, body, _ = book(cu, sl, sid_new, d_e, s_e[0])
a_e = appt_at(nid, d_e, s_e[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
nb.post(f"/nobat/{a_e['id']}/no_show", follow_redirects=True)
check("حضور نیافت ثبت شد", ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?",
                                                 (a_e["id"],), "")) == "no_show")
d_f, s_f = first_free_day(nid, sid_new, min_off=2)
loc, body, _ = book(cu, sl, sid_new, d_f, s_f[0])
a_f = appt_at(nid, d_f, s_f[0], "pending_payment")
r = nb.post(f"/nobat/{a_f['id']}/cancel", data={"reason": "تعمیرات"}, follow_redirects=True)
check("لغو نوبت در انتظار پرداخت توسط کسب‌وکار",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (a_f["id"],), "")) == "cancelled")
check("و ساعت آن آزاد شد", live_count(nid, d_f, s_f[0]) == 0)
d_g, s_g = first_free_day(den_id, den_svc, min_off=3)
loc, body, _ = book(cu, den_slug, den_svc, d_g, s_g[0])
a_g = appt_at(den_id, d_g, s_g[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
a_g = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a_g["id"],), one=True))
r = o.post(f"/nobat/{a_g['id']}/cancel", data={"reason": "لغو توسط مجموعه"}, follow_redirects=True)
rf_g = ctx(lambda: D.query_d("SELECT * FROM refunds WHERE appointment_id=?", (a_g["id"],), one=True))
check("لغو نوبت قطعی توسط کسب‌وکار، طبق قانون مشتری بازپرداخت می‌شود",
      rf_g and rf_g["amount"] == a_g["paid_amount"] == 120000 and "بازپرداخت" in r.get_data(as_text=True),
      rf_g and (rf_g["amount"], a_g["paid_amount"]))
check("کسب‌وکار دیگر نمی‌تواند آن نوبت را لغو کند",
      nb.post(f"/nobat/{a_g['id']}/cancel", data={"reason": "دخالت"}).status_code == 403)
r = o.post(f"/nobat/{a_g['id']}/widget", follow_redirects=True)
check("اقدام ناشناخته ۴۰۴ می‌دهد", r.status_code == 404, r.status_code)
sms_before = ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log", (), 0))
r = nb.post(f"/kasbkar/{sl}/reminders", follow_redirects=True)
check("دکمهٔ یادآوری پیامکی اجرا می‌شود", "یادآوری" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log", (), 0)) >= sms_before)
r = nb.post(f"/kasbkar/{sl}/profile", data={"name": "استودیو تست (ویرایش)", "about": "دربارهٔ تازه",
             "address": "تهران، خیابان آزمون ۱۲", "phone": "021-11122333",
             "district": "محلهٔ تست", "city": city1, "province": prov1, "hue": "285",
             "category": cat1}, follow_redirects=True)
biz3 = ctx(lambda: D.query_d("SELECT * FROM businesses WHERE id=?", (nid,), one=True))
check("پروفایل کسب‌وکار ذخیره شد",
      biz3["name"] == "استودیو تست (ویرایش)" and biz3["hue"] == 285
      and biz3["phone"] == "021-11122333" and "اطلاعات صفحهٔ کسب‌وکار" in r.get_data(as_text=True), biz3)
check("تغییر نام در صفحهٔ عمومی اثر می‌گذارد",
      "استودیو تست (ویرایش)" in new_client().get(f"/k/{sl}").get_data(as_text=True))
did_new = ctx(lambda: D.scalar("SELECT id FROM districts WHERE city_id=? AND name='محلهٔ تست'", (city1,), None))
biz3b = ctx(lambda: D.query_d("SELECT * FROM businesses WHERE id=?", (nid,), one=True))
check("محلهٔ تازه ساخته و به کسب‌وکار وصل شد",
      bool(did_new) and biz3b["district_id"] == did_new, (did_new, biz3b["district_id"]))
rows_new = ctx(lambda: do_search(dist=did_new))
check("فیلتر محله دقیقاً همان کسب‌وکار را می‌دهد",
      [r["slug"] for r in rows_new] == [sl], [r["slug"] for r in rows_new])
check("محلهٔ تازه در فهرست فیلترهای جست‌وجو می‌آید",
      "محلهٔ تست" in new_client().get(f"/jostojoo?city={city1}").get_data(as_text=True))
check("مالکیت دو کسب‌وکار: مالک اول نمی‌توانی پنل دومی را باز کند",
      cb.get(f"/kasbkar/{sl}").status_code == 403)

print("\n── ۲۲) مشتری: ویرایش نام و پرداخت دوباره")
r = cu.post("/profile", data={"name": "سارا مشتری"}, follow_redirects=True)
check("نام مشتری ذخیره شد",
      ctx(lambda: D.scalar("SELECT name FROM users WHERE mobile='09391112233'", (), "")) == "سارا مشتری"
      and "نام شما ذخیره شد" in r.get_data(as_text=True))
check("و در پنل مشتری نمایش داده می‌شود", "سارا مشتری" in cu.get("/pankack").get_data(as_text=True))
d_h, s_h = first_free_day(nid, sid_new, min_off=4)
loc, body, _ = book(cu, sl, sid_new, d_h, s_h[0])
tok_h = gateway_of(loc)
a_h = appt_at(nid, d_h, s_h[0], "pending_payment")
r = cu.get(f"/nobat/{a_h['id']}/pardakht-dobare")
tok_retry = gateway_of(r.headers.get("Location", ""))
check("«پرداخت دوباره» همان تراکنش باز را نگه می‌دارد", tok_retry == tok_h, (tok_retry, tok_h))
open_tx = ctx(lambda: D.scalar("SELECT COUNT(*) FROM transactions WHERE appointment_id=? "
                               "AND kind='payment' AND status='created'", (a_h["id"],), 0))
check("و تراکنش جدیدی برای همان نوبت ساخته نمی‌شود", open_tx == 1, open_tx)
loc, body = pay(cu, tok_retry, "success")
check("پرداخت دوباره با موفقیت نوبت را قطعی می‌کند",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (a_h["id"],), "")) == "confirmed")
r = cu.get(f"/nobat/{a_h['id']}/pardakht-dobare", follow_redirects=True)
check("پس از قطعی‌شدن، پرداخت دوباره به صفحهٔ نوبت برمی‌گردد",
      f"/nobat/{a_h['id']}" in r.request.path)

print("\n── ۲۳) پنل مدیریت: مالی، تنظیمات و محتوا")
check("کاربر عادی نمی‌تواند تنظیمات مدیریتی را ذخیره کند",
      cu.post("/modiriat/settings", data={"site_name": "x"}).status_code == 403)
check("مالک کسب‌وکار هم نه", nb.post("/modiriat/settings", data={"site_name": "y"}).status_code == 403)
r = cm.post("/modiriat/settings", data={"site_name": "نوبتین", "platform_fee_percent": "7",
             "reminder_hours": "18", "support_phone": "021-91000000", "hold_minutes_default": "12",
             "min_pay_percent": "30", "tos_text": "متن شرایط و قوانین آزمون"}, follow_redirects=True)
check("تنظیمات ذخیره شد", "تنظیمات ذخیره شد" in r.get_data(as_text=True)
      and ctx(lambda: D.setting("reminder_hours", "")) == "18", ctx(
          lambda: D.setting("reminder_hours", "")))
check("تنظیمات ناشناخته ذخیره نمی‌شود", ctx(lambda: D.setting("site_logo", None)) is None)
n_tx = ctx(lambda: D.scalar("SELECT COUNT(*) FROM transactions WHERE business_id=? "
                             "AND kind='payment' AND status='success'", (nid,), 0))
bal_n = ctx(lambda: B.business_balance(nid))
check("مالی کسب‌وکار: کارمزد پلتفرم و بازپرداخت‌ها از درآمد ناخالص کم می‌شود",
      n_tx >= 3 and bal_n["platform_fee"] == int(round(bal_n["gross"] * 0.08))
      and bal_n["refunds"] > 0 and bal_n["receivable"] > 0, bal_n)
r = cm.post(f"/modiriat/settle/{nid}", data={}, follow_redirects=True)
st = ctx(lambda: D.query_d("SELECT * FROM settlements WHERE business_id=? ORDER BY id DESC LIMIT 1",
                           (nid,), one=True))
check("تسویه با مبلغ خودکارِ مانده ساخته شد",
      st and st["status"] == "pending" and st["amount"] == bal_n["receivable"], st and
      (st["amount"], bal_n["receivable"]))
if st:
    cm.post(f"/modiriat/settlement/{st['id']}/pay", follow_redirects=True)
    check("تسویه پرداخت‌شده شد", ctx(lambda: D.scalar("SELECT status FROM settlements WHERE id=?",
          (st["id"],), "")) == "paid")
    stl = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE kind='settlement' "
                                "AND business_id=? ORDER BY id DESC LIMIT 1", (nid,), one=True))
    check("پرداخت تسویه در دفتر کل تراکنش‌ها ثبت شد",
          stl and stl["amount"] == st["amount"] and stl["status"] == "success"
          and stl["appointment_id"] is None and stl["trace"] == f"settlement:{st['id']}",
          stl and (stl["amount"], stl["status"], stl["trace"]))
    check("و مرجع یکتای STL دارد", stl and stl["reference"].startswith("STL"), stl and stl["reference"])
    cm.post(f"/modiriat/settlement/{st['id']}/pay", follow_redirects=True)
    check("دوباره‌پرداخت همان تسویه، تراکنش دوم نمی‌سازد", ctx(lambda: D.scalar(
        "SELECT COUNT(*) FROM transactions WHERE kind='settlement' AND business_id=?", (nid,), 0)) == 1)
    check("پیامک تسویه به صاحب کسب‌وکار رفت", ctx(lambda: D.scalar(
        "SELECT COUNT(*) FROM sms_log WHERE template='settlement' AND mobile=?", (new_mob,), 0)) == 1)
    check("تراکنش تسویه، درآمد ناخالصِ «پرداخت‌ها» را زیاد نمی‌کند", ctx(lambda: D.scalar(
        "SELECT COUNT(*) FROM transactions WHERE kind='payment' AND trace LIKE 'settlement:%'",
        (), 0)) == 0)
    bal2 = ctx(lambda: B.business_balance(nid))
    check("پس از تسویه، ماندهٔ قابل پرداخت صفر می‌شود", bal2["paid_out"] == st["amount"]
          and bal2["receivable"] == 0, bal2)
    r = cm.post(f"/modiriat/settlement/{st['id']}/delete", follow_redirects=True)
    check("تسویهٔ پرداخت‌شده حذف نمی‌شود", ctx(lambda: D.scalar(
        "SELECT COUNT(*) FROM settlements WHERE id=?", (st["id"],), 0)) == 1)
r = cm.post(f"/modiriat/settle/{nid}", data={"amount": "0"}, follow_redirects=True)
check("تسویهٔ صفر یا منفی رد می‌شود", "بزرگ‌تر از صفر" in r.get_data(as_text=True))
# شبیه‌سازی درگاه واقعی: بازپرداخت در وضعیت pending می‌ماند
ctx(lambda: D.execute("UPDATE refunds SET status='pending', done_at=NULL WHERE id=?", (rf_a["id"],)))
ctx(lambda: D.execute("UPDATE transactions SET status='created', done_at=NULL WHERE id=?",
                      (rf_a["transaction_id"],)))
pend_ref = ctx(lambda: B.business_balance(nid))["pending_refunds"]
check("بازپرداخت در انتظار از مانده کم می‌شود", pend_ref == rf_a["amount"], pend_ref)
sms_before = ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log", (), 0))
r = cm.post(f"/modiriat/refund/{rf_a['id']}/done", follow_redirects=True)
rf_x = ctx(lambda: D.query_d("SELECT * FROM refunds WHERE id=?", (rf_a["id"],), one=True))
tx_x = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE id=?", (rf_a["transaction_id"],), one=True))
check("مدیر بازپرداخت را انجام‌شده می‌کند (رکورد + تراکنش + پیامک)",
      rf_x["status"] == "done" and tx_x["status"] == "success"
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log", (), 0)) > sms_before,
      rf_x and (rf_x["status"], tx_x["status"]))
check("و pay_status نوبت refunded می‌ماند", ctx(lambda: D.scalar(
    "SELECT pay_status FROM appointments WHERE id=?", (a_a["id"],), "")) == "refunded")
ctx(lambda: D.execute("UPDATE refunds SET status='pending' WHERE id=?", (rf_g["id"],)))
r = cm.post(f"/modiriat/refund/{rf_g['id']}/reject", follow_redirects=True)
check("رد بازپرداخت: رکورد rejected و تراکنش cancelled شد",
      ctx(lambda: D.scalar("SELECT status FROM refunds WHERE id=?", (rf_g["id"],), "")) == "rejected"
      and ctx(lambda: D.scalar("SELECT status FROM transactions WHERE id=?",
                               (rf_g["transaction_id"],), "")) == "cancelled"
      and "رد شد" in r.get_data(as_text=True))
r = cm.post("/modiriat/sms", data={"mobile": "09301234567", "template": "reminder"},
            follow_redirects=True)
check("پیامک آزمایشی مدیر ثبت می‌شود", "پیامک آزمایشی" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log WHERE mobile='09301234567'", (), 0)) > 0)
r = cm.post("/modiriat/sms", data={"mobile": "123", "template": "reminder"}, follow_redirects=True)
check("شمارهٔ نامعتبر پیامک رد می‌شود", "معتبر نیست" in r.get_data(as_text=True))
r = cm.post("/modiriat/category", data={"title": "دستهٔ تست", "icon": "sparkle",
             "blurb": "برای آزمون"}, follow_redirects=True)
cid_new = ctx(lambda: D.scalar("SELECT id FROM categories WHERE title='دستهٔ تست'", (), None))
check("دستهٔ تازه اضافه شد", bool(cid_new) and "دستهٔ تازه" in r.get_data(as_text=True))
r = cm.post("/modiriat/category", data={"id": cat1, "delete": "1"}, follow_redirects=True)
check("دستهٔ دارای کسب‌وکار حذف نمی‌شود", "ابتدا آن‌ها را منتقل کنید" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT is_active FROM categories WHERE id=?", (cat1,), 0)) == 1)
if cid_new:
    cm.post("/modiriat/category", data={"id": cid_new, "title": "دستهٔ تست", "delete": "1"},
            follow_redirects=True)
    check("دستهٔ بی‌کاربرد غیرفعال می‌شود", ctx(lambda: D.scalar(
        "SELECT is_active FROM categories WHERE id=?", (cid_new,), 1)) == 0)
r = cm.post("/modiriat/location", data={"kind": "province", "name": "استان تست"},
            follow_redirects=True)
pid_new = ctx(lambda: D.scalar("SELECT id FROM provinces WHERE name='استان تست'", (), None))
check("استان اضافه شد", bool(pid_new) and "استان اضافه شد" in r.get_data(as_text=True))
r = cm.post("/modiriat/location", data={"kind": "city", "name": "شهر تست"}, follow_redirects=True)
check("شهر بدون استان رد می‌شود", "استان را انتخاب کنید" in r.get_data(as_text=True))
cm.post("/modiriat/location", data={"kind": "city", "name": "شهر تست", "province": pid_new},
        follow_redirects=True)
check("شهر تازه زیرمجموعهٔ استان شد", ctx(lambda: D.scalar(
    "SELECT province_id FROM cities WHERE name='شهر تست'", (), None)) == pid_new)
r = cm.post("/modiriat/location", data={"kind": "city", "name": ""}, follow_redirects=True)
check("نام خالی مکان رد می‌شود", "نام لازم است" in r.get_data(as_text=True))
uid_c1 = ctx(lambda: D.scalar("SELECT id FROM users WHERE mobile='09301234567'", (), None))
cm.post(f"/modiriat/user/{uid_c1}/toggle", follow_redirects=True)
check("مدیر کاربر را غیرفعال می‌کند", ctx(lambda: D.scalar(
    "SELECT is_active FROM users WHERE id=?", (uid_c1,), 1)) == 0)
cc2 = new_client()
login(cc2, "09301234567")
check("کاربر غیرفعال نمی‌تواند وارد شود", not logged_in(cc2))
cm.post(f"/modiriat/user/{uid_c1}/name", data={"name": "مهدی ویرایش‌شده"}, follow_redirects=True)
check("نام کاربر از پنل مدیریت عوض شد", ctx(lambda: D.scalar(
    "SELECT name FROM users WHERE id=?", (uid_c1,), "")) == "مهدی ویرایش‌شده")
uid_admin = ctx(lambda: D.scalar("SELECT id FROM users WHERE mobile='09120000000'", (), None))
r = cm.post(f"/modiriat/user/{uid_admin}/toggle", follow_redirects=True)
check("مدیر حساب خودش را غیرفعال نمی‌کند", "خودتان" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT is_active FROM users WHERE id=?", (uid_admin,), 0)) == 1)
cm.post(f"/modiriat/user/{uid_c1}/toggle", follow_redirects=True)
r = cm.post(f"/modiriat/biz/{nid}/commission", data={"value": "120"}, follow_redirects=True)
check("کارمزد بیش از حد مجاز محدود می‌شود", float(ctx(lambda: D.scalar(
    "SELECT commission_percent FROM businesses WHERE id=?", (nid,), 0.0))) == 50.0)
fk = BIZ["peykane-fitness"]
cm.post(f"/modiriat/biz/{fk['id']}/slug", data={"value": "peykane-fitness-x"}, follow_redirects=True)
check("نشانی صفحهٔ کسب‌وکار تغییر کرد",
      ctx(lambda: D.scalar("SELECT slug FROM businesses WHERE id=?", (fk["id"],), "")) == "peykane-fitness-x"
      and new_client().get("/k/peykane-fitness-x").status_code == 200)
r = cm.post(f"/modiriat/biz/{fk['id']}/slug", data={"value": "dental-plus"}, follow_redirects=True)
check("نشانی تکراری رد می‌شود", "نامعتبر یا تکراری" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT slug FROM businesses WHERE id=?", (fk["id"],), "")) ==
      "peykane-fitness-x")
cm.post(f"/modiriat/biz/{nid}/suspended", follow_redirects=True)
d_i, s_i = first_free_day(nid, sid_new, min_off=5)
loc, body, _ = book(cu, sl, sid_new, d_i, s_i[0])
check("کسب‌وکار معلق‌شده نوبت آنلاین نمی‌گیرد",
      not gateway_of(loc) and "نوبت آنلاین نمی‌گیرد" in body,
      (flash_of(cu, f"/k/{sl}")[:160], loc))
cm.post(f"/modiriat/biz/{nid}/active", follow_redirects=True)
r = cm.get("/modiriat/api/stats")
j = r.get_json() if r.status_code == 200 else {}
check("API آمار: JSON با شاخص‌های مالی", r.status_code == 200
      and "finance" in j and "gross" in j["finance"] and len(j.get("days", [])) == 7,
      (r.status_code, list(j)[:6]))
check("API آمار برای غیرمدیر ۴۰۳ است",
      cu.get("/modiriat/api/stats").status_code in (403, 302))
body = cm.get("/modiriat?tab=money").get_data(as_text=True)
check("تب مالی: تراکنش‌ها و تسویه‌ها نمایش داده می‌شوند",
      "تسویه" in body and body.count("TRX") > 0)
body = cm.get("/modiriat?tab=appointments&status=cancelled").get_data(as_text=True)
check("تب نوبت‌ها با فیلتر وضعیت، نوبت لغوشده را نشان می‌دهد",
      cm.get("/modiriat?tab=appointments&status=cancelled").status_code == 200
      and a_a["code"] in body, a_a["code"])

print("\n── ۲۴) درگاه: جلوگیری از تراکنش دوباره")
d_j, s_j = first_free_day(nid, sid_new, min_off=6)
loc, body, _ = book(cu, sl, sid_new, d_j, s_j[0])
tok_j = gateway_of(loc)
a_j = appt_at(nid, d_j, s_j[0], "pending_payment")
pay(cu, tok_j, "success")
a_j = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a_j["id"],), one=True))
check("پرداخت اول نوبت را قطعی می‌کند", a_j["status"] == "confirmed", a_j["status"])
r = cu.post(f"/gateway/{tok_j}", data={"outcome": "failed"})
check("توکن مصرف‌شدهٔ درگاه ۴۰۴ می‌دهد (نه تغییر وضعیت)", r.status_code == 404, r.status_code)
check("نوبت همچنان قطعی و پرداخت‌شده است",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (a_j["id"],), "")) == "confirmed"
      and ctx(lambda: D.scalar("SELECT pay_status FROM appointments WHERE id=?",
                               (a_j["id"],), "")) == "paid")
tx_j = ctx(lambda: D.query_d("SELECT * FROM transactions WHERE appointment_id=? AND kind='payment'",
                             (a_j["id"],), one=True))
check("و تراکنش فقط یک بار و موفق است",
      tx_j and tx_j["status"] == "success" and ctx(lambda: D.scalar(
          "SELECT COUNT(*) FROM transactions WHERE appointment_id=? AND kind='payment'",
          (a_j["id"],), 0)) == 1, tx_j and tx_j["status"])
r = cu.get(f"/gateway/{tok_j}")
check("بازگشت کاربر به آدرس درگاه پس از موفقیت ۴۰۴ است", r.status_code == 404)
d_k, s_k = first_free_day(nid, sid_new, min_off=7)
loc, body, _ = book(cu, sl, sid_new, d_k, s_k[0])
tok_k = gateway_of(loc)
check("درگاه نوبت کاربر دیگر برای دیگری مسدود است",
      cu2.post(f"/gateway/{tok_k}", data={"outcome": "success"}).status_code == 403)
check("و کاربر ناشناس هم نمی‌تواند پرداخت را نتیجه‌دهی کند",
      new_client().post(f"/gateway/{tok_k}", data={"outcome": "success"}).status_code == 403)
check("نوبت بدون پرداخت همان کاربر، در انتظار پرداخت می‌ماند",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE business_id=? AND date=? "
                           "AND start_hm=?", (nid, d_k, s_k[0]), "")) == "pending_payment")

print("\n── ۲۵) پیامک: هر رویداد به طرف درست می‌رسد و در لاگ پیگیری می‌شود")
CUST_MOB = "09391112233"


def sms_rows(tpl, mob=None, appt=None):
    """پیامک‌های ثبت‌شدهٔ یک قالب؛ با موبایل یا نوبت محدود می‌شود."""
    q, args = "SELECT * FROM sms_log WHERE template=?", [tpl]
    if mob:
        q += " AND mobile=?"
        args.append(mob)
    if appt is not None:
        q += " AND appointment_id=?"
        args.append(appt)
    return ctx(lambda: D.query_d(q + " ORDER BY id", tuple(args)))


def appt_biz(appt_id):
    return ctx(lambda: D.scalar("SELECT business_id FROM appointments WHERE id=?", (appt_id,), 0))


owner_free = ctx(lambda: D.business_owner_mobile(free_id))
check("شمارهٔ صاحب کسب‌وکار از مالک خوانده می‌شود", len(owner_free or "") == 11, owner_free)

# الف) نوبت «پرداخت در محل»: بدون درگاه قطعی می‌شود و دو طرف پیامک می‌گیرند
d_c1, s_c1 = first_free_day(free_id, free_svc, min_off=3)
loc, body, _ = book(cu, free_slug, free_svc, d_c1, s_c1[0])
ap_c1 = appt_at(free_id, d_c1, s_c1[0], "confirmed")
check("نوبت پرداخت‌درمحل بی‌درگاه قطعی می‌شود", bool(ap_c1) and not gateway_of(loc), loc)
check("پیامک تأیید به مشتری، با کد پیگیری و لینک به نوبت",
      len(sms_rows("booking_ok", CUST_MOB, ap_c1["id"])) == 1
      and ap_c1["code"] in sms_rows("booking_ok", CUST_MOB, ap_c1["id"])[0]["body"])
check("پیامک «رزرو جدید» به صاحب کسب‌وکار رفت",
      len(sms_rows("new_booking", owner_free, ap_c1["id"])) == 1)

# ب) نوبت با پرداخت: پیامک‌ها پس از موفقیت پرداخت می‌روند، نه پیش از آن
d_c2, s_c2 = first_free_day(nid, sid_new, min_off=9)
loc, body, _ = book(cu, sl, sid_new, d_c2, s_c2[0])
tok_c2 = gateway_of(loc)
ap_c2 = appt_at(nid, d_c2, s_c2[0], "pending_payment")
check("پیش از پرداخت، پیامک تأیید نوبتی فرستاده نشده", not sms_rows("booking_ok", appt=ap_c2["id"]))
pay(cu, tok_c2, "success")
check("پس از پرداخت موفق، پیامک تأیید برای مشتری و اطلاع برای مالک",
      len(sms_rows("booking_ok", CUST_MOB, ap_c2["id"])) == 1
      and len(sms_rows("new_booking", new_mob, ap_c2["id"])) == 1)

# پ) پرداخت ناموفق و پایان مهلت
d_c3, s_c3 = first_free_day(nid, sid_new, min_off=10)
loc, body, _ = book(cu, sl, sid_new, d_c3, s_c3[0])
tok_c3 = gateway_of(loc)
ap_c3 = appt_at(nid, d_c3, s_c3[0], "pending_payment")
pay(cu, tok_c3, "failed")
check("پرداخت ناموفق: پیامک به مشتری و نوبت قطعی نمی‌شود",
      len(sms_rows("payment_failed", CUST_MOB, ap_c3["id"])) == 1
      and ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (ap_c3["id"],), ""))
      == "expired")
check("و ساعت پس از پرداخت ناموفق آزاد می‌ماند", live_count(nid, d_c3, s_c3[0]) == 0)
d_c4, s_c4 = first_free_day(nid, sid_new, min_off=11)
loc, body, _ = book(cu, sl, sid_new, d_c4, s_c4[0])
ap_c4 = appt_at(nid, d_c4, s_c4[0], "pending_payment")
ctx(lambda: D.execute("UPDATE slot_locks SET expires_at=? WHERE business_id=? AND date=? "
                      "AND start_hm=? AND holder_user_id=(SELECT id FROM users WHERE mobile=?)",
                      ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
                       nid, d_c4, s_c4[0], CUST_MOB)))
n_exp = ctx(lambda: B.expire_pending_appointments())
check("مهلت پرداخت که تمام شود، نوبت منقضی و پیامک می‌رود",
      n_exp >= 1 and len(sms_rows("expired", CUST_MOB, ap_c4["id"])) == 1
      and ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (ap_c4["id"],), ""))
      == "expired", n_exp)

# ت) جابه‌جایی زمان: پیامک «تغییر زمان» به مشتری و به کسب‌وکار
d_c5, s_c5 = first_free_day(nid, sid_new, min_off=12)
new_hm = s_c5[1] if len(s_c5) > 1 else s_c5[0]
r = cu.post(f"/nobat/{ap_c2['id']}/javabeja", data={"date": d_c5, "time": new_hm},
            follow_redirects=True)
check("جابه‌جایی زمان انجام شد", "جابه‌جا شد" in r.get_data(as_text=True),
      flash_of(cu, f"/nobat/{ap_c2['id']}")[:160])
check("پیامک تغییر زمان به مشتری و به صاحب کسب‌وکار رفت",
      len(sms_rows("rescheduled", CUST_MOB, ap_c2["id"])) == 1
      and len(sms_rows("rescheduled", new_mob, ap_c2["id"])) == 1)
check("جابه‌جایی، پیامک «تأیید رزرو» تکراری نساخت",
      len(sms_rows("booking_ok", appt=ap_c2["id"])) == 1)

# ث) تغییر وضعیت از پنل کسب‌وکار
nb.post(f"/nobat/{ap_c2['id']}/completed", follow_redirects=True)
check("«انجام شد» برای مشتری پیامک می‌شود",
      len(sms_rows("completed", CUST_MOB, ap_c2["id"])) == 1)
d_c6, s_c6 = first_free_day(nid, sid_new, min_off=13)
loc, body, _ = book(cu, sl, sid_new, d_c6, s_c6[0])
ap_c6 = appt_at(nid, d_c6, s_c6[0], "pending_payment")
pay(cu, gateway_of(loc), "success")
nb.post(f"/nobat/{ap_c6['id']}/no_show", follow_redirects=True)
check("«حضور نیافت» هم برای مشتری پیامک می‌شود",
      len(sms_rows("no_show", CUST_MOB, ap_c6["id"])) == 1)

# ج) یادآوری: خودکار، ضدتکرار و فقط برای همان کسب‌وکار
prev_rh = ctx(lambda: D.setting("reminder_hours", "12"))
ctx(lambda: D.set_setting("reminder_hours", "720"))
ctx(lambda: D.execute("DELETE FROM sms_log WHERE template='reminder'"))
n_dep = ctx(lambda: B.send_reminders(dep_id))
check("یادآوریِ یک کسب‌وکار به نوبت کسب‌وکار دیگر نمی‌رسد",
      all(appt_biz(r["appointment_id"]) == dep_id for r in sms_rows("reminder"))
      and not any(r["appointment_id"] == ap_c6["id"] for r in sms_rows("reminder")), n_dep)
ctx(lambda: D.execute("DELETE FROM sms_log WHERE template='reminder'"))
n_own = ctx(lambda: B.send_reminders(nid))
own_rows = sms_rows("reminder")
check("یادآوری فقط برای نوبت‌های همین کسب‌وکار فرستاده شد",
      n_own >= 1 and own_rows and all(appt_biz(r["appointment_id"]) == nid for r in own_rows), n_own)
check("یادآوری به شمارهٔ صاحب همان نوبت رسید",
      own_rows and all(r["mobile"] == ctx(lambda: D.scalar(
          "SELECT u.mobile FROM users u JOIN appointments a ON a.user_id=u.id WHERE a.id=?",
          (r["appointment_id"],), "")) for r in own_rows),
      [(r["mobile"], r["appointment_id"]) for r in own_rows][:3])
check("یادآوری دوباره ارسال نمی‌شود (هر نوبت یک بار)", ctx(lambda: B.send_reminders(nid)) == 0)
check("با درخواست صریحِ «ارسال مجدد» دوباره می‌رود", ctx(lambda: B.send_reminders(nid, True)) >= n_own)
ctx(lambda: D.execute("DELETE FROM sms_log WHERE template='reminder'"))
ctx(lambda: D.execute("DELETE FROM settings WHERE key='last_reminder_run'"))
with A.app_context():
    appmod.auto_reminders()
    first_auto = ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log WHERE template='reminder'", (), 0))
    appmod.auto_reminders()
    second_auto = ctx(lambda: D.scalar("SELECT COUNT(*) FROM sms_log WHERE template='reminder'", (), 0))
check("کار خودکارِ یادآوری، پیامک‌ها را می‌فرستد و دوباره در همان بازه اجرا نمی‌شود",
      first_auto > 0 and second_auto == first_auto, (first_auto, second_auto))
ctx(lambda: D.set_setting("reminder_hours", prev_rh))

# ح) شمارهٔ ثابت/نامعتبر: گم نمی‌شود، با وضعیت skipped ثبت می‌شود
ctx(lambda: D.send_sms("02199887766", "otp", code="12345"))
land = sms_rows("otp", "02199887766")
check("خط ثابت با وضعیت skipped در لاگ می‌ماند (بی‌سروصدا حذف نمی‌شود)",
      land and land[-1]["status"] == "skipped", land and land[-1]["status"])
ctx(lambda: D.send_sms("09391112233", "otp", code="65432"))
check("شمارهٔ درست با وضعیت sent ثبت می‌شود", sms_rows("otp", "09391112233")[-1]["status"] == "sent")
bodies = ctx(lambda: D.query_d("SELECT body FROM sms_log"))
check("متن هیچ پیامکی جای‌خالیِ پر‌نشده ندارد",
      bodies and all("{" not in (b["body"] or "") for b in bodies),
      [b["body"] for b in bodies if "{" in (b["body"] or "")][:2])
body = cm.get("/modiriat?tab=sms").get_data(as_text=True)
check("تب پیامک‌ها در پنل مدیریت، متن، قالب و وضعیت را نشان می‌دهد",
      "02199887766" in body and "پیامک‌پذیر" in body and "booking_ok" in body, body[:120])
box_cust = cu.get("/sms-box").get_data(as_text=True)
check("صندوق پیامک به مشتری فقط پیامک‌های خودش را نشان می‌دهد",
      CUST_MOB in box_cust and new_mob not in box_cust and owner_free not in box_cust)
box_admin = cm.get("/sms-box").get_data(as_text=True)
check("صندوق پیامکِ مدیر، همهٔ شماره‌ها را دارد", "02199887766" in box_admin and new_mob in box_admin)
check("صندوق پیامک برای کاربر ناشناس باز نمی‌شود",
      new_client().get("/sms-box").status_code in (302, 403))

print("\n── ۲۶) امنیت ورود: کد تصادفی، انقضا، مهلت و سقف تلاش")
from flask import Flask                                # noqa: E402
import core.config as CF                               # noqa: E402

OLD_CFG = {k: A.config.get(k) for k in ("OTP_MASTER", "OTP_COOLDOWN", "OTP_MAX_MOBILE",
                                        "OTP_MAX_IP", "OTP_MAX_ATTEMPTS")}
A.config.update(OTP_MASTER=False, OTP_COOLDOWN=60, OTP_MAX_MOBILE=2, OTP_MAX_IP=99,
                OTP_MAX_ATTEMPTS=3)
m_s1 = "09395550001"
c_s1 = new_client()
r = c_s1.post("/vorood", data={"mobile": m_s1})
row_s1 = ctx(lambda: D.query_d("SELECT * FROM otp_codes WHERE mobile=? ORDER BY id DESC LIMIT 1",
                               (m_s1,), one=True))
code_s1 = row_s1["code"] if row_s1 else ""
check("کدِ بدون حالت توسعه، پنج‌رقمی و تصادفی است",
      row_s1 and re.fullmatch(r"\d{5}", code_s1) and code_s1 != "12345", code_s1)
check("صفحهٔ تأیید، کد را روی صفحه نمی‌نویسد (حالت توسعه خاموش)",
      "حالت توسعه" not in r.get_data(as_text=True))
check("IPِ درخواست‌کننده در رکورد کد ذخیره شد", row_s1 and row_s1["ip"], row_s1 and row_s1["ip"])
check("پیامک کد با متن واقعی و وضعیت sent لاگ شد",
      len(sms_rows("otp", m_s1)) == 1 and code_s1 in sms_rows("otp", m_s1)[0]["body"])
r = c_s1.post("/tayid", data={"mobile": m_s1, "code": "11111"}, follow_redirects=True)
check("کد اشتباه رد می‌شود و یک تلاش ثبت شد",
      not logged_in(c_s1) and "صحیح نیست" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT attempts FROM otp_codes WHERE id=?", (row_s1["id"],), 0)) == 1)
r = c_s1.post("/tayid", data={"mobile": m_s1, "code": code_s1}, follow_redirects=True)
check("کد درست (پس از تلاشِ اشتباه) وارد می‌کند", logged_in(c_s1))
c_s1.get("/khorooj")
check("کد مصرف‌شده دیگر قابل استفاده نیست", ctx(lambda: D.scalar(
    "SELECT used_at FROM otp_codes WHERE id=?", (row_s1["id"],), None)) is not None)
r = new_client().post("/vorood", data={"mobile": m_s1}, follow_redirects=True)
check("درخواست دومِ کد برای همان شماره، بی‌درنگ مسدود می‌شود",
      "فقط هر" in r.get_data(as_text=True))
A.config["OTP_COOLDOWN"] = 0
m_s2 = "09395550002"
c_s2 = new_client()
for _ in range(2):
    c_s2.post("/vorood", data={"mobile": m_s2}, follow_redirects=True)
r = c_s2.post("/vorood", data={"mobile": m_s2}, follow_redirects=True).get_data(as_text=True)
check("سقف کدِ هر شماره در ساعت رعایت می‌شود",
      "سقف" in r and ctx(lambda: D.scalar("SELECT COUNT(*) FROM otp_codes WHERE mobile=?",
                                          (m_s2,), 0)) == 2)
A.config["OTP_MAX_IP"] = 1
r = new_client().post("/vorood", data={"mobile": "09395550003"}, follow_redirects=True)
check("سقف کد از یک IP هم محدود می‌شود", "رایانه" in r.get_data(as_text=True))
A.config.update(OTP_MAX_IP=99, OTP_MAX_ATTEMPTS=2)
m_s3 = "09395550004"
c_s3 = new_client()
c_s3.post("/vorood", data={"mobile": m_s3})
row_s3 = ctx(lambda: D.query_d("SELECT * FROM otp_codes WHERE mobile=? ORDER BY id DESC LIMIT 1",
                               (m_s3,), one=True))
c_s3.post("/tayid", data={"mobile": m_s3, "code": "00000"})
c_s3.post("/tayid", data={"mobile": m_s3, "code": "00001"})
r = c_s3.post("/tayid", data={"mobile": m_s3, "code": row_s3["code"]}, follow_redirects=True)
check("پس از چند کدِ اشتباه، حتی کد درست هم موقتاً مسدود می‌شود",
      not logged_in(c_s3) and "مسدود" in r.get_data(as_text=True))
ctx(lambda: D.execute("UPDATE otp_codes SET created_at=? WHERE mobile=? AND used_at IS NULL",
                      ((datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), m_s3)))
r = c_s3.post("/tayid", data={"mobile": m_s3, "code": row_s3["code"]}, follow_redirects=True)
check("کد پس از سه دقیقه منقضی می‌شود", "منقضی" in r.get_data(as_text=True)
      and not logged_in(c_s3))
r = new_client().post("/vorood", data={"mobile": "02111223344"}, follow_redirects=True)
check("شمارهٔ ثابت در فرم ورود رد می‌شود", "۱۱ رقم" in r.get_data(as_text=True))
r = new_client().post("/vorood", data={"mobile": "093912"}, follow_redirects=True)
check("شمارهٔ کوتاه رد می‌شود", "۱۱ رقم" in r.get_data(as_text=True))
A.config.update(OLD_CFG)
r = new_client().post("/vorood", data={"mobile": "09395550009"}, follow_redirects=True)
check("در حالت توسعه راهنمای کد مستر روی صفحه است (برای تست دستی)",
      "حالت توسعه" in r.get_data(as_text=True))
relaxed = {k: os.environ.pop(k) for k in ("NOBATIN_OTP_COOLDOWN", "NOBATIN_OTP_MAX_MOBILE",
                                          "NOBATIN_OTP_MAX_IP", "NOBATIN_OTP_MAX_ATTEMPTS")
           if k in os.environ}
try:
    CF.apply(Flask("t"), "production")
    blocked_secret = False
except RuntimeError as e:
    blocked_secret = "NOBATIN_SECRET" in str(e)
check("در production بدون کلیدِ رمز، برنامه بالا نمی‌آید", blocked_secret)
os.environ["NOBATIN_SECRET"] = "x" * 48
os.environ["NOBATIN_OTP_MASTER"] = "1"
try:
    CF.apply(Flask("t"), "production")
    blocked_master = False
except RuntimeError as e:
    blocked_master = "OTP_MASTER" in str(e)
check("کد مستر در production، اجرا را متوقف می‌کند", blocked_master)
os.environ.pop("NOBATIN_OTP_MASTER")

# دو fail-open که بی‌صدا رخ می‌دهند و در همین بخش جلوی بالا آمدن را می‌گیرند:
# هاست مجازِ خالی (پیوندهای پیامکی با دامنهٔ مهاجم) و درگاه آزمایشی در تولید
# (هر کاربری می‌تواند «پرداخت موفق» بزند و نوبت رایگان قطعی کند).
os.environ.pop("NOBATIN_TRUSTED_HOSTS", None)
try:
    CF.apply(Flask("t"), "production")
    blocked_hosts = False
except RuntimeError as e:
    blocked_hosts = "NOBATIN_TRUSTED_HOSTS" in str(e)
check("production بی‌فهرست هاست مجاز: برنامه بالا نمی‌آید", blocked_hosts)
os.environ["NOBATIN_TRUSTED_HOSTS"] = "example.com"
try:
    CF.apply(Flask("t"), "production")
    blocked_sbx = False
except RuntimeError as e:
    blocked_sbx = "NOBATIN_ALLOW_SANDBOX" in str(e)
check("production با درگاه آزمایشی و بی‌اجازهٔ صریح: بالا نمی‌آید", blocked_sbx)
os.environ["NOBATIN_ALLOW_SANDBOX"] = "1"
prod_app = CF.apply(Flask("t"), "production")
check("با تنظیمات کامل، production بالا می‌آید و پیش‌فرض‌ها امن است",
      prod_app.config["IS_PRODUCTION"] and not prod_app.config["OTP_MASTER"]
      and prod_app.config["SMS_PROVIDER"] == "log"
      and prod_app.config["PAYMENT_PROVIDER"] == "sandbox"
      and prod_app.config["TRUSTED_HOSTS"][:1] == ("example.com",)
      and prod_app.config["OTP_COOLDOWN"] == 60 and prod_app.config["OTP_MAX_MOBILE"] == 5,
      {k: prod_app.config[k] for k in ("ENV", "OTP_COOLDOWN", "SMS_PROVIDER")})
check("production: متن پیامکِ کد در sms_log ماسکه می‌شود",
      prod_app.config["SMS_MASK_OTP"] is True)
check("production: درگاه sandbox با اجازهٔ صریح فعال می‌ماند (محیط نمایشی)",
      prod_app.config["PAYMENT_PROVIDER"] == "sandbox")
os.environ.pop("NOBATIN_ALLOW_SANDBOX")
os.environ["NOBATIN_PAYMENT_PROVIDER"] = "idpay"
check("با درگاه واقعی، production بی‌NOBATIN_ALLOW_SANDBOX هم بالا می‌آید",
      CF.apply(Flask("t2"), "production").config["PAYMENT_PROVIDER"] == "idpay")
os.environ.pop("NOBATIN_PAYMENT_PROVIDER")
os.environ.pop("NOBATIN_SECRET")
os.environ.pop("NOBATIN_TRUSTED_HOSTS")
os.environ.update(relaxed)
check("حالت توسعهٔ اپ اصلی دست‌نخورده برگشت", A.config["OTP_MASTER"] is True
      and A.config["OTP_COOLDOWN"] == 0, (A.config["OTP_MASTER"], A.config["OTP_COOLDOWN"]))

print("\n── ۲۷) رندر پنل‌ها پس از همهٔ تغییرات (بدون خطای قالب)")
bad = [t for t in ["today", "services", "hours", "rules", "profile", "all"]
       if nb.get(f"/kasbkar/{sl}?tab={t}").status_code != 200]
check("پنل کسب‌وکار تازه با همهٔ تب‌ها", not bad, bad)
bad = [t for t in ["overview", "businesses", "users", "cats", "money", "appointments", "sms", "settings",
                   "log"]
       if cm.get(f"/modiriat?tab={t}").status_code != 200]
check("پنل مدیریت با همهٔ تب‌ها پس از نوشتن‌ها", not bad, bad)
check("پنل مشتری پس از لغو/جابه‌جایی/بازپرداخت‌ها",
      cu.get("/pankack").status_code == 200 and new_client().get("/").status_code == 200)
check("صفحهٔ نوبتِ انجام‌شده (تاریخچه) خطا نمی‌دهد",
      cu.get(f"/nobat/{a_c['id']}").status_code == 200)
check("کاربر غیرفعال‌شده از پنل بیرون می‌ماند",
      cc2.get("/pankack").status_code in (302, 403))

print("\n── ۲۸) CSRF: هیچ نوشتنی بی‌توکنِ نشست انجام نمی‌شود")
from core import csrf as CS                              # noqa: E402


def forms_missing_token(text, needle):
    """فرم‌های POST که توکن محافظتی ندارند (هم در قالب، هم در HTMLِ رندرشده)."""
    bad = []
    for m in re.finditer(r"<form\b[^>]*>", text, re.I):
        if not re.search(r'method\s*=\s*["\']?post', m.group(0), re.I):
            continue
        end = text.find("</form>", m.end())
        seg = text[m.end():end if end > -1 else None]
        if needle not in seg:
            act = re.search(r'action="([^"]*)"', m.group(0))
            bad.append(act.group(1) if act else m.group(0)[:70])
    return bad


m_c1 = "09395550101"
n_otp = ctx(lambda: D.scalar("SELECT COUNT(*) FROM otp_codes", (), 0))
r = PlainClient(A).post("/vorood", data={"mobile": m_c1})
check("POST بی‌توکن با ۴۰۰ رد می‌شود", r.status_code == 400, r.status_code)
check("صفحهٔ ۴۰۰ پیام خوانا دارد (نه صفحهٔ خام خطا)",
      "نشست شما تمام شده" in r.get_data(as_text=True))
check("نوشتنِ ردشده هیچ اثری در پایگاه داده نگذاشت",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM otp_codes", (), 0)) == n_otp
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM users WHERE mobile=?", (m_c1,), 0)) == 0,
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM otp_codes", (), 0)))
check("توکنِ نادرست هم رد می‌شود",
      PlainClient(A).post("/vorood", data={"mobile": m_c1, "csrf": "ASHGHAL"}).status_code == 400)
check("GET بی‌توکن دست‌نخورده است", PlainClient(A).get("/").status_code == 200)
c_hdr = PlainClient(A)
with c_hdr.session_transaction() as s:
    s["csrf_token"] = "tok-from-header"
r = c_hdr.post("/vorood", data={"mobile": m_c1}, headers={"X-CSRF-Token": "tok-from-header"})
check("توکن از سرصفحهٔ X-CSRF-Token هم پذیرفته می‌شود",
      r.status_code != 400 and ctx(lambda: D.scalar(
          "SELECT COUNT(*) FROM otp_codes WHERE mobile=?", (m_c1,), 0)) == 1, r.status_code)
check("فرم واقعی (توکن در فیلد مخفی) می‌گذرد",
      CsrfClient(A).post("/vorood", data={"mobile": "09395550102"}).status_code != 400)
c_rot = new_client()
c_rot.get("/vorood")
with c_rot.session_transaction() as s:
    tok_before = s.get("csrf_token") or ""
login(c_rot, "09123334455")
with c_rot.session_transaction() as s:
    tok_after = s.get("csrf_token") or ""
check("با هر ورود توکن تازه می‌شود (توکنِ پیش از ورود بی‌ارزش می‌گردد)",
      tok_before and tok_after and tok_before != tok_after, (tok_before[:6], tok_after[:6]))
tpl_bad = [os.path.basename(f) for f in sorted(glob.glob(os.path.join(ROOT, "templates", "**", "*.html"),
                                                         recursive=True))
           if forms_missing_token(open(f, encoding="utf-8").read(), "csrf()")]
check("همهٔ فرم‌های POST در قالب‌ها {{ csrf() }} دارند", not tpl_bad, tpl_bad)
pages = [("/", new_client()), ("/vorood", new_client()), ("/jostojoo", new_client()),
         (f"/k/{full_slug}", new_client()), (f"/k/{full_slug}/rezerv?date={day}&service={full_svc}",
                                             new_client()),
         ("/pankack", cu), (f"/kasbkar/{sl}", nb), ("/modiriat?tab=log", cm), ("/profile", cu)]
bad_pages = [p for p, cl in pages if forms_missing_token(cl.get(p).get_data(as_text=True),
                                                         'name="csrf"')]
check("در صفحاتِ رندرشده هم هیچ فرم POST بی‌توکنی نیست", not bad_pages, bad_pages)
check("همهٔ مسیرهای نوشتن در برنامه، محافظت‌شده‌اند",
      CS.WRITE_METHODS == ("POST", "PUT", "PATCH", "DELETE")
      and not A.config.get("CSRF_EXEMPT_ENDPOINTS"), A.config.get("CSRF_EXEMPT_ENDPOINTS"))
EXEMPT_OLD = A.config.get("CSRF_EXEMPT_ENDPOINTS", ())
A.config["CSRF_EXEMPT_ENDPOINTS"] = ("auth.send_otp",)
check("با معاف‌سازیِ endpoint، همان POST بی‌توکن می‌گذرد (برای بازگشت سرور‌به‌سرور درگاه)",
      PlainClient(A).post("/vorood", data={"mobile": "09395550103"}).status_code != 400)
A.config["CSRF_EXEMPT_ENDPOINTS"] = EXEMPT_OLD
check("پس از بازگرداندنِ تنظیم، همان درخواست دوباره ۴۰۰ می‌گیرد",
      PlainClient(A).post("/vorood", data={"mobile": "09395550104"}).status_code == 400)

print("\n── ۲۹) محله و مکان‌ها: املاهای مختلفِ یک نام، یک رکورد")
import sqlite3                                           # noqa: E402
from core.jalali import norm_place                       # noqa: E402

variants = ["سعادت آباد", "سعادت‌آباد", "سعادت  آباد", " سعادت‌آباد "]
check("نیم‌فاصله، فاصلهٔ اضافه و همزه در کلیدِ نام یکی می‌شوند",
      len({norm_place(v) for v in variants}) == 1, [norm_place(v) for v in variants])
check("«محلهٔ» و «محله» یک کلید دارند", norm_place("محلهٔ تست") == norm_place("محله تست"))
check("ی/کِ عربی با معادل فارسی یکی است",
      norm_place("كوشش") == norm_place("کوشش") and norm_place("ياسی") == norm_place("یاسی"))
check("حرکت و کشیده از کلید حذف می‌شوند", norm_place("تـهْران") == norm_place("تهران"))
check("شکستِ فارسی/عربی هم جمع می‌شود", norm_place("  دريای   غرب ") == "دریای غرب")
did_v1 = ctx(lambda: D.get_or_create_district(city1, "سعادت آباد"))
did_v2 = ctx(lambda: D.get_or_create_district(city1, "سعادت‌آباد"))
check("املای دیگرِ یک محله، رکورد تازه نمی‌سازد", did_v1 and did_v1 == did_v2, (did_v1, did_v2))
check("شمار رکوردهای هم‌کلید در آن شهر ۱ می‌ماند", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM districts WHERE city_id=? AND norm=?", (city1, norm_place("سعادت آباد")), 0)) == 1)
check("نام نمایشی به آخرین شکلِ نوشته‌شده به‌روز می‌شود",
      ctx(lambda: D.scalar("SELECT name FROM districts WHERE id=?", (did_v2,), "")) == "سعادت‌آباد")
check("نام خالی هیچ محله‌ای نمی‌سازد", ctx(lambda: D.get_or_create_district(city1, "   ")) is None)
dup_blocked = False
try:
    ctx(lambda: D.execute("INSERT INTO districts(city_id,name,norm) VALUES(?,?,?)",
                          (city1, "سعادت آباد ۲", norm_place("سعادت آباد"))))
except sqlite3.IntegrityError:
    dup_blocked = True
ctx(lambda: D.execute("DELETE FROM districts WHERE city_id=? AND name=?", (city1, "سعادت آباد ۲")))
check("اینکسِ یکتایی، درجِ مستقیمِ تکراری را هم در سطح پایگاه داده رد می‌کند", dup_blocked)
r = cm.post("/modiriat/location", data={"kind": "district", "city": city1, "name": "محله تست"},
            follow_redirects=True)
did_same = ctx(lambda: D.scalar("SELECT id FROM districts WHERE city_id=? AND norm=?",
                                (city1, norm_place("محلهٔ تست")), None))
check("مدیر هم با املای دیگر، همان محلهٔ مالک را می‌یابد",
      did_same == did_new and "محله ثبت شد" in r.get_data(as_text=True), (did_new, did_same))
check("نام خالی از پنل رد می‌شود",
      "نام لازم است" in cm.post("/modiriat/location",
                               data={"kind": "district", "city": city1, "name": "  "},
                               follow_redirects=True).get_data(as_text=True))
check("محله بی‌شهر ساخته نمی‌شود",
      "شهر را انتخاب کنید" in cm.post("/modiriat/location", data={"kind": "district", "name": "بی‌شهر"},
                                     follow_redirects=True).get_data(as_text=True))
r = cm.post(f"/modiriat/district/{did_new}/rename", data={"name": "  محلهٔ  نمونه "},
            follow_redirects=True)
row_ren = ctx(lambda: D.query_d("SELECT * FROM districts WHERE id=?", (did_new,), one=True))
check("تغییر نام از پنل، فاصله‌های اضافه را هم می‌گیرد و کلید را به‌روز می‌کند",
      row_ren and row_ren["name"] == "محلهٔ نمونه" and row_ren["norm"] == norm_place("محله نمونه"),
      row_ren and (row_ren["name"], row_ren["norm"]))
cm.post("/modiriat/location", data={"kind": "district", "city": city1, "name": "محله ب"},
        follow_redirects=True)
did_b = ctx(lambda: D.scalar("SELECT id FROM districts WHERE city_id=? AND norm=?",
                             (city1, norm_place("محله ب")), None))
r = cm.post(f"/modiriat/district/{did_b}/rename", data={"name": "محله نمونه"}, follow_redirects=True)
check("تغییر نام به محلهٔ هم‌نامِ دیگرِ همان شهر رد می‌شود",
      "کسب‌وکارها را" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT name FROM districts WHERE id=?", (did_b,), "")) == "محله ب")
n_moved = ctx(lambda: D.scalar("SELECT COUNT(*) FROM businesses WHERE district_id=?", (did_new,), 0))
r = cm.post(f"/modiriat/district/{did_new}/merge", data={"into": did_b}, follow_redirects=True)
check("ادغام دستی، کسب‌وکارها را به مقصد می‌برد و مبدأ را حذف می‌کند",
      n_moved > 0 and "منتقل شد" in r.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM districts WHERE id=?", (did_new,), 0)) == 0
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM businesses WHERE district_id=?", (did_b,), 0)) == n_moved,
      n_moved)
check("ادغام محله در خودش رد می‌شود",
      "در همان شهر باشد" in cm.post(f"/modiriat/district/{did_b}/merge", data={"into": did_b},
                                   follow_redirects=True).get_data(as_text=True))
check("حذف محله‌ای که کسب‌وکار دارد مسدود است",
      "منتقل کنید" in cm.post(f"/modiriat/district/{did_b}/delete",
                             follow_redirects=True).get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM districts WHERE id=?", (did_b,), 0)) == 1)
cm.post("/modiriat/location", data={"kind": "district", "city": city1, "name": "محلهٔ خالی"},
        follow_redirects=True)
did_empty = ctx(lambda: D.scalar("SELECT id FROM districts WHERE city_id=? AND norm=?",
                                 (city1, norm_place("محلهٔ خالی")), None))
check("محلهٔ بی‌کسب‌وکار حذف می‌شود",
      "محله حذف شد" in cm.post(f"/modiriat/district/{did_empty}/delete",
                              follow_redirects=True).get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM districts WHERE id=?", (did_empty,), 0)) == 0)
p_name = ctx(lambda: D.scalar("SELECT name FROM provinces WHERE id=?", (prov1,), ""))
n_prov = ctx(lambda: D.scalar("SELECT COUNT(*) FROM provinces", (), 0))
check("استان تکراری (حتی با فاصلهٔ اضافه) از پنل رد می‌شود",
      "از قبل ثبت شده" in cm.post("/modiriat/location",
                                 data={"kind": "province", "name": f" {p_name} "},
                                 follow_redirects=True).get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM provinces", (), 0)) == n_prov)
c_name = ctx(lambda: D.scalar("SELECT name FROM cities WHERE id=?", (city1,), ""))
n_city = ctx(lambda: D.scalar("SELECT COUNT(*) FROM cities WHERE province_id=?", (prov1,), 0))
check("شهر تکراری در همان استان رد می‌شود",
      "از قبل وجود دارد" in cm.post("/modiriat/location",
                                    data={"kind": "city", "province": prov1, "name": c_name + " "},
                                    follow_redirects=True).get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT COUNT(*) FROM cities WHERE province_id=?", (prov1,), 0)) == n_city)
check("شهر بی‌استان رد می‌شود",
      "استان را انتخاب کنید" in cm.post("/modiriat/location", data={"kind": "city", "name": "بی‌استان"},
                                        follow_redirects=True).get_data(as_text=True))
leg_a = ctx(lambda: D.execute("INSERT INTO districts(city_id,name,norm) VALUES(?,?,?)", (city1, "محله قدیم", "")))
leg_b = ctx(lambda: D.execute("INSERT INTO districts(city_id,name,norm) VALUES(?,?,?)", (city1, "محله قدیم ", "")))
ctx(lambda: D.execute("UPDATE businesses SET district_id=? WHERE id=?", (leg_b, nid)))
ctx(lambda: (D.migrate_places(D.get_db()), D.get_db().commit()))
left = ctx(lambda: D.query_d("SELECT * FROM districts WHERE city_id=? AND norm=?",
                             (city1, norm_place("محله قدیم")), one=True))
check("مهاجرت، محله‌های هم‌نامِ قدیمی را خودکار ادغام می‌کند",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM districts WHERE id IN (?,?)", (leg_a, leg_b), 0)) == 1
      and left["id"] == leg_a, (leg_a, leg_b, left and left["id"]))
check("و کسب‌وکارِ محلهٔ حذف‌شده بی‌صاحب نمی‌ماند",
      ctx(lambda: D.scalar("SELECT district_id FROM businesses WHERE id=?", (nid,), None)) == leg_a)
did_back = ctx(lambda: D.get_or_create_district(city1, "محلهٔ تست"))
ctx(lambda: D.execute("UPDATE businesses SET district_id=? WHERE id=?", (did_back, nid)))
ctx(lambda: D.execute("DELETE FROM districts WHERE id=?", (did_b,)))
check("محلهٔ کسب‌وکار آزمایشی به حالت اول برگشت",
      ctx(lambda: D.scalar("SELECT district_id FROM businesses WHERE id=?", (nid,), None)) == did_back)
body = nb.get(f"/kasbkar/{sl}?tab=profile").get_data(as_text=True)
check("فرم پروفایل مالک، محله‌های همان شهر را به‌عنوان پیشنهاد می‌دهد",
      'list="distList"' in body and '<datalist id="distList">' in body and "یک‌شکل" in body)
n_dist_before = ctx(lambda: D.scalar("SELECT COUNT(*) FROM districts WHERE city_id=?", (city1,), 0))
r = nb.post(f"/kasbkar/{sl}/profile", data={"name": "استودیو تست (ویرایش)", "about": "دربارهٔ تازه",
              "address": "تهران، خیابان آزمون ۱۲", "phone": "021-11122333",
              "district": "محله تست", "city": city1, "province": prov1, "hue": "285",
              "category": cat1}, follow_redirects=True)
check("املای متفاوتِ مالک (بی‌نیم‌فاصله)، محلهٔ دومی نمی‌سازد",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM districts WHERE city_id=?", (city1,), 0)) == n_dist_before
      and ctx(lambda: D.scalar("SELECT district_id FROM businesses WHERE id=?", (nid,), None)) == did_back,
      n_dist_before)
body = cm.get("/modiriat?tab=cats").get_data(as_text=True)
check("تب مکان‌ها، کارت محله‌ها را با فرم تغییر نام/ادغام/حذف نشان می‌دهد",
      "خودکار ادغام می‌شوند" in body and 'value="district"' in body
      and 'name="into"' in body and "حذف این محله؟" in body)
check("محله‌های تازه در همان کارت فهرست شده‌اند",
      "محله قدیم" in body and "سعادت‌آباد" in body)

print("\n── ۳۰) ردپای اقدامات و لایهٔ پروکسی")
logs = ctx(lambda: D.query_d("SELECT * FROM audit_log ORDER BY id DESC LIMIT 80"))
acts = {l["action"] for l in logs}
check("اقدام‌های محله‌ها در ردپا ثبت می‌شوند",
      {"district_add", "district_rename", "district_merge", "district_delete"} <= acts, sorted(acts)[:14])
check("اقدام‌های کسب‌وکار (تغییر وضعیت نوبت) هم در ردپاست",
      any(a.startswith("appt_") for a in acts), sorted(acts)[:14])
adm_uid = ctx(lambda: D.scalar("SELECT id FROM users WHERE mobile=?", ("09120000000",), 0))
check("ردپا کاربر و نقشش را می‌شناسد",
      any(l["user_id"] == adm_uid and l["role"] == "admin" for l in logs)
      and all(l["role"] for l in logs if l["user_id"] == adm_uid))
check("ردپا زمان و موضوعِ قابل پیگیری دارد",
      any(l["target"].startswith("district:") and l["created_at"] and l["detail"] for l in logs))
check("تغییر نقش کاربر (بخش ۱۷) هم در ردپاست",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM audit_log WHERE action='user_role'", (), 0)) >= 1)
body = cm.get("/modiriat?tab=log").get_data(as_text=True)
check("تب ردپا، آخرین اقدام‌ها را با کاربر و اقدام نشان می‌دهد",
      "ردپای اقدامات" in body and "district_merge" in body and "09120000000" in body)
check("تب ردپا فهرست پرتکرارترین اقدام‌ها را هم دارد", "پرتکرارترین" in body)
check("کاربر ناشناس ردپا را نمی‌بیند", new_client().get("/modiriat?tab=log").status_code == 302)
check("هیچ نوشتنی در ردپا، رمز/گذرواژه یا کدِ یک‌بارمصرف نگه نمی‌دارد",
      not any(re.search(r"(?<!\d)12345(?!\d)|گذرواژه|رمز|secret|token",
                        (l["detail"] or "") + (l["target"] or "")) for l in logs),
      [(l["action"], l["target"], l["detail"]) for l in logs
       if re.search(r"(?<!\d)12345(?!\d)|گذرواژه|رمز|secret|token",
                    (l["detail"] or "") + (l["target"] or ""))][:3])
check("شمارهٔ موبایلِ خودِ کاربرِ فعال‌شده در ردپا ثبت شده (قابل پیگیری است)",
      any(l["action"] == "user_toggle" and "09301234567" in (l["detail"] or "") for l in logs))
tmpapp = Flask("proxytest")
from flask import request                                 # noqa: E402
tmpapp.add_url_rule("/ip", "ip", view_func=lambda: f"{request.remote_addr}|{request.is_secure}")
with tmpapp.test_client() as tc:
    plain_ip = tc.get("/ip", headers={"X-Forwarded-For": "203.0.113.9",
                                      "X-Forwarded-Proto": "https"}).get_data(as_text=True)
tmpapp.config["BEHIND_PROXY"] = True
appmod.apply_proxy(tmpapp)
with tmpapp.test_client() as tc:
    fixed_ip = tc.get("/ip", headers={"X-Forwarded-For": "203.0.113.9",
                                      "X-Forwarded-Proto": "https"}).get_data(as_text=True)
check("بدون فعال‌سازی، سرصفحهٔ X-Forwarded-For باور نمی‌شود",
      plain_ip.startswith("127.0.0.1") and plain_ip.endswith("|False"), plain_ip)
check("با NOBATIN_BEHIND_PROXY، IP و پروتکل واقعی از سرصفحه خوانده می‌شود",
      fixed_ip.startswith("203.0.113.9") and fixed_ip.endswith("|True"), fixed_ip)
check("خودِ اپ (تنظیم پیش‌فرض) لایهٔ پروکسی ندارد",
      type(A.wsgi_app).__name__ != "ProxyFix" and not A.config["BEHIND_PROXY"],
      type(A.wsgi_app).__name__)
env_bak = {k: os.environ.get(k) for k in ("NOBATIN_BEHIND_PROXY", "NOBATIN_CSRF_EXEMPT")}
os.environ["NOBATIN_BEHIND_PROXY"] = "1"
os.environ["NOBATIN_CSRF_EXEMPT"] = "gateway.pay, admin.panel"
cfg_app = CF.apply(Flask("t3"))
check("NOBATIN_BEHIND_PROXY=1 در تنظیمات اثر می‌کند", cfg_app.config["BEHIND_PROXY"] is True)
check("NOBATIN_CSRF_EXEMPT با ویرگول و فاصلهٔ اضافه درست خوانده می‌شود",
      cfg_app.config["CSRF_EXEMPT_ENDPOINTS"] == ("gateway.pay", "admin.panel"),
      cfg_app.config["CSRF_EXEMPT_ENDPOINTS"])
os.environ.pop("NOBATIN_CSRF_EXEMPT")
for k, v in env_bak.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
check("تنظیمات اصلیِ اپ دست‌نخورده ماند",
      A.config["BEHIND_PROXY"] is False and not A.config.get("CSRF_EXEMPT_ENDPOINTS"))

print("\n── ۳۱) مسیر ورود کسب‌وکار: پیوند «کسب‌وکار خود را ثبت کنید» به بن‌بست نمی‌رسد")
r = cu.get("/kasbkar/new")
check("مشتریِ بی‌کسب‌وکار فرم ثبت را می‌بیند (نه ۴۰۳)",
      r.status_code == 200 and 'name="slug"' in r.get_data(as_text=True), r.status_code)
r = cu.get("/kasbkar", follow_redirects=True)
check("«پنل کسب‌وکار» برای مشتریِ بی‌کسب‌وکار به همان فرم می‌رسد",
      r.status_code == 200 and "/kasbkar/new" in r.request.path, r.request.path)
check("ثبت‌نام باز است و توضیح وضعیت «پس از تأیید مدیر» را نشان می‌دهد",
      "تأیید مدیر" in r.get_data(as_text=True))
check("جداسازی دست‌نخورده: مشتری به پنل کسب‌وکار دیگری راه ندارد",
      cu.get(f"/kasbkar/{full_slug}").status_code == 403)
check("صفحهٔ ۴۰۳ برای مشتری راه‌حل را پیشنهاد می‌کند",
      "کسب‌وکار خود را ثبت کنید" in cu.get(f"/kasbkar/{full_slug}").get_data(as_text=True))
check("مالکِ دارای کسب‌وکار به فرم ثبت نمی‌رود و به پنل خودش می‌رسد",
      "/kasbkar/new" not in cb.get("/kasbkar/new").headers.get("Location", ""),
      cb.get("/kasbkar/new").headers.get("Location", ""))
check("کاربر واردنشده به صفحهٔ ورود می‌رود",
      "/vorood" in new_client().get("/kasbkar/new").headers.get("Location", ""))
check("مدیر هم می‌تواند کسب‌وکار ثبت کند و نقشش تغییر نمی‌کند",
      cm.get("/kasbkar/new").status_code == 200
      and ctx(lambda: D.scalar("SELECT role FROM users WHERE mobile=?", ("09120000000",), "")) == "admin")

print("\n── ۳۲) بنر تبلیغ ثبت کسب‌وکار در صفحهٔ اصلی")
hp_g = new_client().get("/").get_data(as_text=True)
check("بنر تبلیغ رندر می‌شود", 'class="section promo"' in hp_g and "تبلیغ" in hp_g)
check("متن تبلیغ فقط قابلیت‌های پیاده‌شده را می‌شمارد",
      all(x in hp_g for x in ("تقویم شمسی", "قوانین کنسلی", "گزارش مالی و تسویه", "پیش‌پرداخت")),
      [x for x in ("تقویم شمسی", "قوانین کنسلی", "گزارش مالی و تسویه", "پیش‌پرداخت") if x not in hp_g])
check("تبلیغ قول «بدون پرداخت موفق، نوبت قطعی نیست» را می‌دهد (الزام نسخهٔ اول)",
      "پرداخت موفق نشود نوبتی قطعی نمی‌شود" in hp_g)
check("شرط تأیید مدیر در تبلیغ پنهان نیست", "پس از تأیید مدیر" in hp_g)
check("کمیسیون واقعی پلتفرم در تبلیغ گفته می‌شود", "کمیسیون پیش‌فرض" in hp_g,
      hp_g[hp_g.find("کمیسیون"):][:80])
check("دکمهٔ تبلیغ برای مهمان به ورود با مقصدِ فرم ثبت می‌رود",
      'href="/vorood?next=' in hp_g and "/kasbkar/new" in hp_g)
check("«نمونهٔ صفحه» به یک کسب‌وکار فعال لینک می‌شود",
      'btn-outline-l' in hp_g and 'href="/k/' in hp_g)
hp_c = cu.get("/").get_data(as_text=True)
check("دکمهٔ تبلیغ برای مشتریِ بی‌کسب‌وکار مستقیم به فرم ثبت می‌رود", 'href="/kasbkar/new"' in hp_c)
hp_b = nb.get("/").get_data(as_text=True)
check("دکمهٔ تبلیغ برای مالک به پنل کسب‌وکار خودش می‌رود",
      f'href="/kasbkar/{sl}"' in hp_b and "btn-gold" in hp_b)

# ادعای بی‌پشتوانه: نظر و امتیاز در نسخهٔ اول پیاده نشده‌اند، پس هیچ صفحه‌ای نباید
# آن‌ها را نشان دهد یا مرتب‌سازی را به ستونِ خالیِ rating ببندد.
c_nr = new_client()
b_nr = c_nr.get("/jostojoo?order=best").get_data(as_text=True)
check("گزینهٔ مرتب‌سازی «پرطرفدارترین» است و «بهترین امتیاز» حذف شده",
      "پرطرفدارترین" in b_nr and "بهترین امتیاز" not in b_nr)
check("کارت‌های نتایج هیچ بنر امتیازی ندارند", 'class="rating"' not in b_nr)
b_bz = c_nr.get(f"/k/{sl}").get_data(as_text=True)
check("صفحهٔ کسب‌وکار «… نظر» نشان نمی‌دهد (نظردهی در نسخهٔ اول نیست)", " نظر)" not in b_bz)
check("و به‌جایش عددِ واقعیِ نوبت‌های قطعی را می‌گوید",
      "نوبت قطعی" in b_bz or "نوبت فروش" in b_bz)
b_nw = c_nr.get("/kasbkar/new").get_data(as_text=True)
check("فرم ثبت کسب‌وکار هم پیش‌نمایشِ امتیاز ندارد", 'class="rating"' not in b_nw)
check("هیچ کسب‌وکاری امتیازِ ساختگی ندارد (دادهٔ سِید صفر است)",
      ctx(lambda: D.scalar("SELECT COUNT(*) FROM businesses WHERE rating>0 OR rating_count>0",
                           (), 0)) == 0)
check("مرتب‌سازی‌های دیگر و نامعلوم بدون خطا می‌گذرند",
      c_nr.get("/jostojoo?order=busy").status_code == 200
      and c_nr.get("/jostojoo?order=nonsense").status_code == 200)

print("\n── ۳۳) همزمانی واقعی: چند کاربر، یک ساعت، یک برنده")
# این بخش با تردهای واقعی روی همان پایگاه دادهٔ آزمون می‌نویسد: «دو نفر هم‌زمان یک
# ساعت را بخواهند» باید دقیقاً یک برنده داشته باشد و نیم‌رکور هم از خود به جا بماند.
import threading
from core import cache as CACHE


def r_hammer(fn, n):
    """n ترد که هم‌زمان (با barrier) fn(i) را اجرا می‌کنند؛ نتیجه‌ها به ترتیب."""
    out = [None] * n
    gate = threading.Barrier(n)

    def w(i):
        gate.wait()
        try:
            out[i] = fn(i)
        except Exception as exc:                  # خطای ترد نباید آزمون را نصفه رها کند
            out[i] = ("thread-error", repr(exc))
    ths = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    return out


r_users = []
for i in range(8):
    uid_r = ctx(lambda i=i: D.execute(
        "INSERT INTO users(name,mobile,role,created_at) VALUES(?,?,?,?)",
        ("کاربر همزمان %s" % (i + 1), "09211%06d" % i, "customer", D.now_str())))
    r_users.append(ctx(lambda uid_r=uid_r: D.query_d("SELECT * FROM users WHERE id=?", (uid_r,), one=True))
                     )
check("هشت حساب رقابت ساخته شد", len([u for u in r_users if u]) == 8)

r_biz = BIZ[full_slug]
r_svc = SVC[full_id][0]
r_day, r_slots = first_free_day(full_id, r_svc["id"], min_off=3)
r_slot = r_slots[0]

res = r_hammer(lambda i: ctx(lambda i=i: B.hold_slot(r_users[i]["id"], full_id, r_svc["id"],
                                                     r_day, r_slot, 10)), 8)
winners = [x for x in res if isinstance(x, tuple) and x[0]]
check("۸ قفل هم‌زمان روی یک ساعت: دقیقاً یک برنده", len(winners) == 1,
      [(x[0] if isinstance(x, tuple) else x) for x in res])
check("بازنده‌ها پیام «هم‌زمان/سبد کاربر دیگر» می‌گیرند، نه استثنا",
      all(isinstance(x, tuple) and not x[0] and ("این زمان" in x[1] or "سبد" in x[1])
          for x in res if x not in winners))
check("در دیتابیس هم فقط یک قفل فعال است", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM slot_locks WHERE business_id=? AND date=? AND start_hm=? AND status='hold'",
    (full_id, r_day, r_slot), 0)) == 1)
check("قفل برنده به همان کاربری است که پیام موفقیت گرفت",
      ctx(lambda: D.scalar("SELECT holder_user_id FROM slot_locks WHERE business_id=? AND date=? "
                           "AND start_hm=? AND status='hold'", (full_id, r_day, r_slot), None))
      == r_users[[x for x in res].index(winners[0])]["id"])

ctx(lambda: B.release_slot(full_id, r_day, r_slot))
res2 = r_hammer(lambda i: ctx(lambda i=i: B.create_pending_appointment(
    r_users[i], r_biz, r_svc, r_day, r_slot)), 8)
made = [x for x in res2 if isinstance(x, tuple) and x[0]]
check("۸ ساخت نوبت هم‌زمان روی یک ساعت: فقط یک نوبت ساخته می‌شود", len(made) == 1,
      [(bool(x[0]) if isinstance(x, tuple) else x) for x in res2])
check("ساعت فقط یک نوبت زنده دارد", live_count(full_id, r_day, r_slot) == 1)
check("بازنده‌ها پیام «دقیقاً هم‌زمان رزرو شد» یا «سبد کاربر دیگری» می‌گیرند",
      all(isinstance(x, tuple) and not x[0] and ("هم‌زمان" in (x[1] or "") or "سبد" in (x[1] or ""))
          for x in res2 if x not in made))
check("برای بازنده‌ها هیچ رکورد نوبتی نمانده", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM appointments WHERE business_id=? AND date=? AND start_hm=?",
    (full_id, r_day, r_slot), 0)) == 1)
check("نیم‌نوبتی با وضعیت نامعلوم باقی نمانده", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM appointments WHERE business_id=? AND date=? AND start_hm=? "
    "AND status NOT IN ('pending_payment','confirmed')", (full_id, r_day, r_slot), 0)) == 0)

r_appt = appt_at(full_id, r_day, r_slot, "pending_payment")
check("نوبت برنده در انتظار پرداخت است (قطعی نشده)", bool(r_appt) and r_appt["status"] == "pending_payment")
# تراکنش درگاه (همان کاری که مسیر /gateway می‌کند) تا «دو تأیید هم‌زمان» واقعی باشد
tok_r = ctx(lambda: B.new_token_and_tx(r_appt["id"], r_appt["payable_now"]))
check("یک تراکنش باز برای پرداخت ساخته شد", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM transactions WHERE appointment_id=? AND kind='payment' "
    "AND status IN ('created','pending')", (r_appt["id"],), 0)) == 1, tok_r)
fin = r_hammer(lambda i: ctx(lambda i=i: B.finalize_payment(r_appt["id"], "AUTH-RACE", "TRACE-R")), 6)
check("شش تأیید هم‌زمان پرداخت: همه موفق‌اند", all(isinstance(x, tuple) and x[0] for x in fin), fin)
check("نوبت یک‌بار قطعی شد", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM appointments WHERE id=? AND status='confirmed'", (r_appt["id"],), 0)) == 1)
check("تراکنش موفق یکی ماند (پرداخت دوباره ثبت نمی‌شود)", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM transactions WHERE appointment_id=? AND kind='payment' AND status='success'",
    (r_appt["id"],), 0)) == 1)
check("پیامک «قطعی شد» هم یک‌بار می‌رود", ctx(lambda: D.scalar(
    "SELECT COUNT(*) FROM sms_log WHERE appointment_id=? AND template='booking_ok'",
    (r_appt["id"],), 0)) == 1)

d_x, s_x = first_free_day(full_id, r_svc["id"], min_off=10)
r_slots_x = s_x[:8]
res3 = r_hammer(lambda i: ctx(lambda i=i: B.create_pending_appointment(
    r_users[i], r_biz, r_svc, d_x, r_slots_x[i])), 8)
ok_x = [x for x in res3 if isinstance(x, tuple) and x[0]]
check("هشت ساعت متفاوت، هشت کاربر: همه می‌توانند (قفل بی‌مصرف نیست)", len(ok_x) == 8,
      [(x[1] if isinstance(x, tuple) and not x[0] else "ok") for x in res3])
check("هیچ‌کدام ساعت دیگری را قفل نکرد", ctx(lambda: D.scalar(
    "SELECT COUNT(DISTINCT date || start_hm) FROM appointments WHERE business_id=? AND date=?",
    (full_id, d_x), 0)) == 8)


def r_atomic():
    try:
        with A.app_context():
            with D.tx():
                D.execute("INSERT INTO users(name,mobile,role,created_at) VALUES(?,?,?,?)",
                          ("نیمه‌کاره", "09990000111", "customer", D.now_str()))
                raise RuntimeError("خطای وسط کار")
    except RuntimeError:
        return "rolled-back"


check("اگر وسط تراکنش خطا بدهد، هیچ نوشته نمی‌شود",
      r_atomic() == "rolled-back" and ctx(lambda: D.scalar(
          "SELECT COUNT(*) FROM users WHERE mobile='09990000111'", (), 0)) == 0)
check("و تراکنشِ بعدی هم نمی‌ماند (قفل آزاد شد)", ctx(lambda: D.tx_depth()) == 0)

BKT = "smoke:race-counter"
ctx(lambda: D.throttle_reset(BKT))
cnt = r_hammer(lambda i: ctx(lambda i=i: D.throttle(BKT, 5, 60)[0]), 12)
check("شمارندهٔ سقف نرخ زیر بار هم دقیق است (۵ از ۱۲)", sum(1 for x in cnt if x) == 5,
      [x for x in cnt])
ctx(lambda: D.throttle_reset(BKT))

print("\n── ۳۴) آمادهٔ سرور: سرصفحه‌ها، هاست معتبر، سلامت و نگهداری")
rr = new_client().get("/")
hh = rr.headers
csp = hh.get("Content-Security-Policy", "")
check("CSP روی هر پاسخی می‌نشیند", csp.startswith("default-src 'self'"), csp[:60])
check("CSP اسکریپت درون‌خطی را ممنوع می‌کند",
      "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0])
check("هیچ iframe/object برای نوبتین لازم نیست", "frame-ancestors 'none'" in csp and "object-src 'none'" in csp)
check("X-Frame-Options: DENY", hh.get("X-Frame-Options") == "DENY")
check("X-Content-Type-Options: nosniff", hh.get("X-Content-Type-Options") == "nosniff")
check("Referrer-Policy و Permissions-Policy بسته‌اند",
      hh.get("Referrer-Policy") == "strict-origin-when-cross-origin" and
      "geolocation=()" in hh.get("Permissions-Policy", ""))
check("نسخهٔ وب‌سرور فاش نمی‌شود", hh.get("Server") == "nobatin" and "werkzeug" not in str(hh).lower())
check("در حالت توسعه HSTS نمی‌آید (فقط HTTPS واقعی)", "Strict-Transport-Security" not in hh)
biz_hdr = new_client().get(f"/k/{full_slug}").headers
check("صفحهٔ عمومی کش کوتاه دارد (زمان‌ها زنده عوض می‌شوند)",
      "max-age=10" in biz_hdr.get("Cache-Control", ""))
pan_hdr = cu.get("/pankack").headers
check("صفحهٔ پنل no-store و Vary: Cookie می‌شود",
      "no-store" in pan_hdr.get("Cache-Control", "") and "Cookie" in pan_hdr.get("Vary", ""))
ck = PlainClient(A).get("/vorood").headers.get("Set-Cookie", "")
check("کوکی نشست HttpOnly و SameSite=Lax است", "HttpOnly" in ck and "SameSite=Lax" in ck, ck[:120])
check("کوکی نشست بدون Secure در حالت توسعه هم فرستاده می‌شود", "Secure" not in ck)
check("هیچ اسکریپت یا هندلر درون‌خطی در قالب‌ها نمانده",
      not [os.path.basename(f) for f in glob.glob(os.path.join(ROOT, "templates", "**", "*.html"),
                                                  recursive=True)
           if re.search(r"<script(?![^>]*\bsrc=)", open(f, encoding="utf-8").read())
           or re.search(r"\son(?:click|change|submit|input|load|error|focus|blur|keypress)\s*=",
                        open(f, encoding="utf-8").read())])
css_url = rr.get_data(as_text=True)
check("فایل ایستا با اثر انگشت محتوا می‌آید (کش بلند بی‌خطر)",
      re.search(r"/static/css/nobatin\.css\?v=[0-9a-f]+", css_url), css_url[css_url.find("nobatin.css")-20:][:60])

prev_hosts = A.config.get("TRUSTED_HOSTS")
A.config["TRUSTED_HOSTS"] = ("example.com", ".example.com", "localhost", "127.0.0.1")


def with_host(h, path="/"):
    c = new_client()
    r = c.get(path, headers={"Host": h})
    return r, r.get_data(as_text=True)


r_bad, b_bad = with_host("evil.com")
check("با TRUSTED_HOSTS گذاشته‌شده، Host جعلی ۴۰۰ می‌گیرد", r_bad.status_code == 400,
      r_bad.status_code)
check("۴۰۰ِ هدر Host صفحهٔ ساده می‌دهد (قالب در این حالت نمی‌سازد)",
      "شناخته‌شده نیست" in b_bad, b_bad[:70])
check("صفحهٔ ساده هم سرصفحه‌های امنیتیِ لازم را دارد (از لایهٔ request رد می‌شود)",
      r_bad.headers.get("X-Content-Type-Options") == "nosniff"
      and r_bad.headers.get("X-Frame-Options") == "DENY"
      and r_bad.headers.get("Server") == "nobatin", dict(r_bad.headers))
check("خودِ دامنهٔ مجاز پذیرفته می‌شود", with_host("example.com")[0].status_code == 200)
check("زیردامنه فقط با پیشوند «.» در فهرست پذیرفته می‌شود (همان قاعدهٔ werkzeug)",
      with_host("shop.example.com")[0].status_code == 200)
check("هاستِ localhost با پورت و نقطهٔ انتهایی پذیرفته می‌شود",
      with_host("localhost:8111.")[0].status_code == 200)
check("Host با حروف بزرگ هم می‌گذرد (DNS بی‌تفاوت است، werkzeug نبود)",
      with_host("LOCALHOST")[0].status_code == 200)
check("پورت نامعتبر ۴۰۰ می‌گیرد نه ۵۰۰", with_host("localhost:99999")[0].status_code == 400)
check("نشانی خالی ۴۰۰ می‌گیرد", with_host("")[0].status_code == 400)
check("پیوندهای صفحه با هدرِ نرمال‌شده ساخته می‌شوند (نه Host جعلی)",
      'href="/jostojoo' in with_host("localhost.", "/")[1])
check("صفحهٔ کسب‌وکار زیر هدرِ نرمال‌شده هم درست ساخته می‌شود",
      with_host("localhost.", f"/k/{full_slug}")[0].status_code == 200)
A.config["TRUSTED_HOSTS"] = prev_hosts
check("بدون TRUSTED_HOSTS (ماشین محلی) هر Host رد نمی‌شود",
      new_client().get("/", headers={"Host": "whatever.test"}).status_code == 200)

big = "x" * (int(A.config["MAX_CONTENT_LENGTH"]) + 2048)
r413 = new_client().post("/vorood", data={"mobile": "09395550102", "blob": big})
check("بدنهٔ بزرگ‌تر از سقف، ۴۱۳ می‌گیرد", r413.status_code == 413, r413.status_code)
check("۴۱۳ پیام خوانای فارسی دارد", "خیلی بزرگ" in r413.get_data(as_text=True))

prev_lim = A.config.get("LIMIT_WRITES_IP")
ctx(lambda: D.throttle_reset("w:127.0.0.1"))
A.config["LIMIT_WRITES_IP"] = 2
codes429 = [new_client().post("/vorood", data={"mobile": "09395550102"}).status_code for _ in range(3)]
r_limited = new_client().post("/vorood", data={"mobile": "09395550102"})
# POST /vorood در حالت عادی ۲۰۰ است (صفحهٔ واردکردن کد را نشان می‌دهد، ریدایرکت ندارد)
check("سقف نوشتن هر IP: دو درخواست اول می‌گذرند", codes429[:2] == [200, 200], codes429)
check("سوم ۴۲۹ است", codes429[2] == 429, codes429)
check("۴۲۹ سرصفحهٔ Retry-After و پیام دارد",
      str(r_limited.headers.get("Retry-After", "")).isdigit() and "بیش از حد" in r_limited.get_data(as_text=True))
A.config["LIMIT_WRITES_IP"] = prev_lim
ctx(lambda: D.throttle_reset("w:127.0.0.1"))

hz = new_client().get("/healthz")
check("healthz از loopback پاسخ JSON می‌دهد", hz.status_code == 200 and hz.get_json()["status"] in ("ok", "degraded"),
      hz.status_code)
check("healthz فقط آنچه لازم است نشان می‌دهد",
      set(hz.get_json()) <= {"status", "db", "uptime", "env", "cache"}, sorted(hz.get_json()))
check("healthz از بیرون ۴۰۴ است (پنهان از اسکنرها)",
      new_client().get("/healthz", environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 404)
A.config["HEALTHZ_TOKEN"] = "tok-for-smoke"
check("با توکن درست از بیرون هم بررسی‌پذیر است",
      new_client().get("/healthz?token=tok-for-smoke",
                       environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 200)
check("توکن غلط همان ۴۰۴ را می‌گیرد",
      new_client().get("/healthz?token=nope",
                       environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 404)
A.config["HEALTHZ_TOKEN"] = ""
A.config["HEALTHZ_ENABLED"] = False
check("با NOBATIN_HEALTHZ=0 حتی از loopback هم ۴۰۴ است",
      new_client().get("/healthz").status_code == 404)
A.config["HEALTHZ_ENABLED"] = True

prev_cfg = {k: A.config.get(k) for k in ("IS_PRODUCTION", "OTP_MASTER", "TRUSTED_HOSTS",
                                         "HSTS_SECONDS", "SESSION_COOKIE_SECURE")}
A.config.update(IS_PRODUCTION=True, OTP_MASTER=True, TRUSTED_HOSTS=(), HSTS_SECONDS=0)
codes_fail = {c for lvl, c, _ in appmod.selfcheck(A) if lvl == "FAIL"}
check("selfcheck: کد مستر روشن در production → FAIL", "otp_master" in codes_fail, codes_fail)
check("selfcheck: هاست مجاز خالی در production → FAIL", "hosts" in codes_fail)
check("selfcheck: HSTS خاموش در production → FAIL", "hsts" in codes_fail)
check("selfcheck: حساب نمونه در production → FAIL", "seed" in codes_fail)
check("selfcheck: ایندکس‌های گرم جاافتاده ندارند", "indexes" not in codes_fail, codes_fail)
check("selfcheck: کوکی Secure خاموش در production → FAIL",
      "cookie_secure" in codes_fail, codes_fail)
check("selfcheck: WAL و busy_timeout روی این پایگاه درست‌اند",
      {"wal", "busy_timeout"} & {c for lvl, c, _ in appmod.selfcheck(A) if lvl in ("FAIL", "WARN")} == set())
A.config.update(prev_cfg)
dev_fails = {c for lvl, c, _ in appmod.selfcheck(A) if lvl == "FAIL"}
check("selfcheck در حالت توسعه همان‌ها را FAIL نمی‌داند",
      not (dev_fails & {"otp_master", "hosts", "hsts", "secret"}), dev_fails)
check("selfcheck: کوکی Secure خاموش در توسعه FAIL نیست (پیش‌فرضِ http محلی)",
      "cookie_secure" not in dev_fails, dev_fails)

mob_p = "09397770011"
ctx(lambda: D.execute("INSERT INTO otp_codes(mobile,code,attempts,ip,created_at,used_at) "
                      "VALUES(?,?,0,?,?,?)",
                      (mob_p, "246813", "127.0.0.1", D.now_str(), D.now_str())))
ctx(lambda: D.execute("INSERT INTO sms_log(mobile,template,body,status,created_at) VALUES(?,?,?,?,?)",
                      (mob_p, "otp", "کد تأیید شما 246813 است", "sent", D.now_str())))
ctx(lambda: D.execute("INSERT INTO rate_events(bucket,window_start,cnt) VALUES(?,?,?)",
                      ("old:bucket", (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d %H:%M:%S"), 3)))
res_prune = ctx(lambda: D.prune(days_otp=1, days_sms=60, days_audit=365, days_rate=2))
check("--prune کد یک‌بارمصرف را از دیتابیس ماسک می‌کند",
      ctx(lambda: D.scalar("SELECT code FROM otp_codes WHERE mobile=?", (mob_p,), "")) == "—")
check("و متن پیامک کد را هم ماسک می‌کند", ctx(lambda: D.scalar(
    "SELECT body FROM sms_log WHERE mobile=? AND template='otp'", (mob_p,), "")) == "—")
check("شماره‌های قدیمیِ سقف نرخ دور ریخته می‌شوند", res_prune["rate_deleted"] >= 1 and ctx(
    lambda: D.scalar("SELECT COUNT(*) FROM rate_events WHERE bucket='old:bucket'", (), 0)) == 0)
check("پس از prune، کش هم پاک می‌شود (دادهٔ کهنه نمی‌ماند)", CACHE.stats()["keys"] == 0, CACHE.stats())

key_cache = "smoke:nav_cats"
n0 = len(ctx(lambda: CACHE.remember(key_cache, lambda: D.query_d(
    "SELECT * FROM categories WHERE is_active=1 ORDER BY sort"), tables=("categories",))))
n0b = len(ctx(lambda: CACHE.remember(key_cache, lambda: D.query_d(
    "SELECT * FROM categories WHERE is_active=1 ORDER BY sort"), tables=("categories",))))
new_cat = ctx(lambda: D.execute(
    "INSERT INTO categories(title,icon,sort,is_active) VALUES('دستهٔ کش آزمایشی','bolt',999,1)", ()))
n1 = len(ctx(lambda: CACHE.remember(key_cache, lambda: D.query_d(
    "SELECT * FROM categories WHERE is_active=1 ORDER BY sort"), tables=("categories",))))
check("دوباره‌خوانی کش بدون کوئری به دیتابیس انجام می‌شود", n0 == n0b and CACHE.stats()["hits"] > 0)
check("نوشتن روی همان جدول، کش را بی‌اعتبار می‌کند", n1 == n0 + 1, (n0, n1))
ctx(lambda: D.execute("DELETE FROM categories WHERE id=?", (new_cat,)))
check("کش پس از حذف هم درست برمی‌گردد", len(ctx(lambda: CACHE.remember(
    key_cache, lambda: D.query_d("SELECT * FROM categories WHERE is_active=1 ORDER BY sort"),
    tables=("categories",)))) == n0)

def _boom():
    raise ValueError("رازِ داخلیِ مسیر")


# `add_url_rule` پس از اولین درخواست بسته است (Flask نمی‌خواهد مسیرها میانهٔ کار
# عوض شوند)، پس این‌جا مستقیم به نقشه و تابع‌های نما می‌افزاییم: همان چیزی که یک
# مسیر واقعیِ خراب در تولید می‌کند — خطای بی‌برنامه، بدون handler ویژه.
from werkzeug.routing import Rule                            # noqa: E402
if "smoke_boom" not in A.view_functions:
    A.view_functions["smoke_boom"] = _boom
    A.url_map.add(Rule("/__smoke_boom", endpoint="smoke_boom"))
check("خطای پیش‌بینی‌نشده صفحهٔ ۵۰۰ تمیز می‌دهد",
      new_client().get("/__smoke_boom").status_code == 500)
b500 = new_client().get("/__smoke_boom").get_data(as_text=True)
check("پشتهٔ خطا و متن فنی به کاربر نشت نمی‌کند",
      "Traceback" not in b500 and "رازِ داخلی" not in b500 and "خطای سرور" in b500)
check("۵۰۰ هم سرصفحه‌های امنیتی را می‌گیرد",
      "default-src 'self'" in new_client().get("/__smoke_boom").headers.get(
          "Content-Security-Policy", ""))

_env_backup = {k: os.environ.get(k) for k in ("NOBATIN_ENV", "NOBATIN_SECRET", "NOBATIN_OTP_MASTER",
                                              "NOBATIN_SEED", "NOBATIN_COOKIE_SECURE",
                                              "NOBATIN_TRUSTED_HOSTS", "NOBATIN_ALLOW_SANDBOX")}
os.environ["NOBATIN_ENV"] = "production"
os.environ["NOBATIN_SECRET"] = "k" * 48
os.environ.pop("NOBATIN_OTP_MASTER", None)
os.environ.pop("NOBATIN_SEED", None)
# این دو در production اجباری‌اند؛ بدونشان خودِ «بالا آمدن» رد می‌شود و
# آزمونِ پیش‌فرض‌های production هیچ وقت به config نمی‌رسد.
os.environ["NOBATIN_TRUSTED_HOSTS"] = "example.com"
os.environ["NOBATIN_ALLOW_SANDBOX"] = "1"
from flask import Flask as _Flask
from core import config as _C
prod_cfg = _C.apply(_Flask("probe"), "production").config
check("production: دادهٔ نمونه خاموش است", prod_cfg["SEED_DEMO"] is False)
check("production: کوکی Secure پیش‌فرض روشن است", prod_cfg["SESSION_COOKIE_SECURE"] is True)
check("production: HSTS یک سال است", prod_cfg["HSTS_SECONDS"] == 31536000)
check("production: کد مستر خاموش است", prod_cfg["OTP_MASTER"] is False)
check("production: عمر نشست ۱۲ ساعت است", prod_cfg["PERMANENT_SESSION_LIFETIME"] == 12 * 3600)


def prod_refuse(why):
    """با این دلیلِ محیطی، برنامهٔ production باید بالا نیاید (نه هشدار، نه فقط لاگ)."""
    try:
        appmod.create_app(env_name=why)
        return "sneaked-through"
    except RuntimeError as exc:
        return str(exc)


os.environ["NOBATIN_SECRET"] = ""
r_nosecret = prod_refuse("production")
check("production بی‌کلید نشست: ساخت اپ رد می‌شود", "NOBATIN_SECRET" in r_nosecret, r_nosecret[:70])
os.environ["NOBATIN_SECRET"] = "Short"
r_short = prod_refuse("production")
check("production با کلید کوتاه: همان رد شدن", "NOBATIN_SECRET" in r_short, r_short[:80])
os.environ["NOBATIN_SECRET"] = "k" * 48
os.environ["NOBATIN_OTP_MASTER"] = "1"
r_master = prod_refuse("production")
check("production با کد مستر روشن: بالا نمی‌آید", "مجاز نیست" in r_master, r_master[:80])
os.environ.pop("NOBATIN_OTP_MASTER", None)
os.environ["NOBATIN_TRUSTED_HOSTS"] = " Nobatin.Site. , .example.com"
host_cfg = _C.apply(_Flask("probe5"), "production").config["TRUSTED_HOSTS"]
check("فهرست هاست‌ها تمیز می‌شود: بزرگی/کوچکی و نقطهٔ انتهایی",
      host_cfg[:2] == ("nobatin.site", ".example.com"), host_cfg)
check("لوکال‌ها خودکار اضافه می‌شوند تا healthcheck محلی ۴۰۰ نگیرد",
      "localhost" in host_cfg and "127.0.0.1" in host_cfg, host_cfg)
check("پیشوند «.» زیردامنهٔ مجاز را حفظ می‌کند", ".example.com" in host_cfg, host_cfg)
os.environ["NOBATIN_TRUSTED_HOSTS"] = ""
check("گارد Host همان قاعدهٔ werkzeug را دارد: زیردامنه بی‌«.» رد می‌شود",
      appmod.trusted_host("shop.nobatin.site", ("nobatin.site",)) is None
      and appmod.trusted_host("shop.nobatin.site", (".nobatin.site",)) == "shop.nobatin.site")
check("نرمال‌سازی: حروف بزرگ، نقطهٔ انتهایی و پورت",
      appmod.trusted_host("LOCALHOST.", ("localhost",)) == "localhost"
      and appmod.trusted_host("localhost:8111.", ("localhost",)) == "localhost:8111"
      and appmod.trusted_host("localhost:0", ("localhost",)) is None
      and appmod.trusted_host("[::1]:8111", ("::1",)) is None)
os.environ["NOBATIN_OTP_MASTER"] = "1"
os.environ["NOBATIN_SEED"] = "0"
check("config در حالت توسعه سخت‌گیر نیست (کد مستر و دادهٔ نمونه مجاز)",
      _C.apply(_Flask("probe6"), "development").config.get("OTP_MASTER") is True)
for k, v in _env_backup.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
check("تنظیمات محیطیِ آزمون دست‌نخورده برگشت",
      os.environ.get("NOBATIN_SECRET") == _env_backup["NOBATIN_SECRET"]
      and os.environ.get("NOBATIN_OTP_MASTER") == _env_backup["NOBATIN_OTP_MASTER"],
      os.environ.get("NOBATIN_OTP_MASTER"))

print("\n── ۳۵) سخت‌سازی بازبینی امنیتی: قانون پول، نشست، سبد و لایهٔ پروکسی")
from core.jalali import evaluate_cancellation, parse_policy, hours_until   # noqa: E402
from core.helpers import safe_next, business_or_404                        # noqa: E402

# ---- قانون بازپرداخت: مرزها و دادهٔ خراب
base_now = datetime(2026, 10, 20, 10, 0)
pol2 = [{"min_hours": 24, "refund_percent": 50}, {"min_hours": 2, "refund_percent": 10}]
ev24 = evaluate_cancellation(pol2, "2026-10-21 10:00", now=base_now)
ev23 = evaluate_cancellation(pol2, "2026-10-21 09:00", now=base_now)
ev01 = evaluate_cancellation(pol2, "2026-10-20 11:00", now=base_now)
check("الزام نسخهٔ اول: دقیقاً ۲۴ ساعت پیش از نوبت → ۵۰٪ بازپرداخت",
      ev24["refund_percent"] == 50, ev24)
check("۲۳ ساعت پیش: از بازهٔ ۵۰٪ بیرون است", ev23["refund_percent"] == 10, ev23)
check("یک ساعت پیش: هیچ قانونی برقرار نیست → صفر، با پیامِ روشن",
      ev01["refund_percent"] == 0 and "غیرقابل کنسلی" in ev01["label"], ev01)
check("قانونِ JSONِ ناخوانا بی‌صدا صفر است و استثنا نمی‌دهد",
      parse_policy("[{bad json") == [] and parse_policy(None) == [] and parse_policy("") == []
      and evaluate_cancellation("[{bad", "2026-11-01 10:00", now=base_now)["refund_percent"] == 0)
check("عناصر ناآشنا در فهرستِ قانون حذف می‌شوند (نه ۵۰۰ وسطِ لغو)",
      parse_policy([1, "x", None, {"min_hours": 24, "refund_percent": 50}])
      == [{"min_hours": 24, "refund_percent": 50}])
check("ساعتِ منفی یا غیرواقعی قانون نیست؛ وگرنه هر لغوِ دیررس ۱۰۰٪ می‌شد",
      parse_policy('[{"min_hours": -5, "refund_percent": 100}]') == []
      and parse_policy('[{"min_hours": 999999, "refund_percent": 100}]') == [])
check("درصد بیرون از ۰..۱۰۰ به همان بازه برمی‌گردد",
      [x["refund_percent"] for x in parse_policy(
          '[{"min_hours": 1, "refund_percent": 150},{"min_hours": 0, "refund_percent": -20}]')]
      == [100, 0])
check("یک دیکشنریِ تنها هم قانون حساب می‌شود",
      parse_policy({"min_hours": 48, "refund_percent": 80}) == [{"min_hours": 48, "refund_percent": 80}])
check("ساعتِ ناخوانای نوبت → صفر و بدون ۵۰۰ (نه پشتهٔ خطا برای کاربر)",
      hours_until("nope") == 0.0
      and evaluate_cancellation(pol2, "nope", now=base_now)["refund_percent"] == 0)

# ---- فرم قوانین: ورودیِ آشوب از POST دستی
sl_id = ctx(lambda: D.scalar("SELECT id FROM businesses WHERE slug=?", (sl,), 0))
r = nb.post(f"/kasbkar/{sl}/rules", data={"payment_mode": "full", "deposit": 0,
                                          "hold_minutes": "99999",
                                          "rule_hours": ["abc", "-5", "24", "24", "48"],
                                          "refund_24": "150", "refund_48": "not-a-number"},
            follow_redirects=True)
saved_b = ctx(lambda: D.query_d("SELECT cancel_policy, hold_minutes, payment_mode FROM businesses "
                                "WHERE id=?", (sl_id,), one=True))
check("فرم قوانین با ورودیِ خراب ۵۰۰ نمی‌دهد", r.status_code == 200, r.status_code)
check("فقط قانونِ درست، یک‌بار، با سقف درصد و مرتب ذخیره می‌شود",
      json.loads(saved_b["cancel_policy"]) == [{"min_hours": 48, "refund_percent": 0},
                                               {"min_hours": 24, "refund_percent": 100}],
      saved_b and saved_b["cancel_policy"])
check("مهلت قفل ساعت سقف دارد (با POST دستی بی‌کران نمی‌شود)",
      saved_b["hold_minutes"] == 30, saved_b)

# ---- ماسکِ کدِ ورود در sms_log (پنل پیامک‌ها نباید رمزِ زنده داشته باشد)
prev_mask = A.config.get("SMS_MASK_OTP")
m_mask = "09395557001"
A.config["SMS_MASK_OTP"] = True
code_mask = ctx(lambda: D.create_otp(m_mask, "127.0.0.1"))[0]
body_mask = ctx(lambda: D.scalar("SELECT body FROM sms_log WHERE mobile=? AND template='otp' "
                                 "ORDER BY id DESC LIMIT 1", (m_mask,), ""))
check("با ماسک روشن، بدنهٔ پیامکِ کد در دیتابیس هیچ رقمی ندارد",
      bool(body_mask) and not re.search(r"\d", body_mask), body_mask)
check("و کدِ واقعی در جدولِ کدها دست‌نخورده می‌ماند (ورود نمی‌شکند)",
      ctx(lambda: D.scalar("SELECT code FROM otp_codes WHERE mobile=? ORDER BY id DESC LIMIT 1",
                           (m_mask,), "")) == str(code_mask))
c_mask = new_client()
r_mask = c_mask.post("/tayid", data={"mobile": m_mask, "code": code_mask}, follow_redirects=False)
check("با وجود ماسک، ورود با همان کد کار می‌کند",
      r_mask.status_code == 302 and logged_in(c_mask), r_mask.status_code)
A.config["SMS_MASK_OTP"] = False
code_raw = ctx(lambda: D.create_otp("09395557002", "127.0.0.1"))[0]
check("در توسعه (ماسک خاموش) همان بدنه با کدِ خوانا ثبت می‌شود",
      str(code_raw) in ctx(lambda: D.scalar("SELECT body FROM sms_log WHERE mobile=? "
                                            "AND template='otp' ORDER BY id DESC LIMIT 1",
                                            ("09395557002",), "")))
A.config["SMS_MASK_OTP"] = prev_mask

# ---- درگاه: صفحهٔ «پرداخت موفق» فقط در حالت آزمایشی وجود دارد
d8, s8 = first_free_day(full_id, full_svc)
loc8, _b8, _r8 = book(cu, full_slug, full_svc, d8, s8[0])
tok8 = gateway_of(loc8)
a8 = appt_at(full_id, d8, s8[0], "pending_payment")
prev_prov = A.config.get("PAYMENT_PROVIDER")
A.config["PAYMENT_PROVIDER"] = "idpay"
check("با درگاه واقعی، صفحهٔ درگاه آزمایشی ۴۰۴ است", cu.get(f"/gateway/{tok8}").status_code == 404)
r_free = cu.post(f"/gateway/{tok8}", data={"outcome": "success"})
check("POST به همان مسیر هم نمی‌تواند نوبت را رایگان قطعی کند",
      r_free.status_code == 404 and appt_at(full_id, d8, s8[0], "confirmed") is None,
      (r_free.status_code, a8 and a8["status"]))
A.config["PAYMENT_PROVIDER"] = prev_prov
check("در حالت sandbox همان صفحه در دسترس می‌ماند (برگشت‌پذیر)",
      cu.get(f"/gateway/{tok8}").status_code == 200)
pay(cu, tok8, "success")
a8 = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a8["id"],), one=True))
check("نوبتِ آزمونِ این بخش قطعی شد (برای ادامه لازم است)", a8["status"] == "confirmed", a8["status"])

# ---- بازگشت پس از ورود: مهاجم ساعت را از کوئری تعیین نمی‌کند
n_appt = ctx(lambda: D.scalar("SELECT COUNT(*) FROM appointments", (), 0))
c_g = new_client()
login(c_g, "09395557003")
r_g = c_g.get(f"/k/{full_slug}/takmil?date={d8}&service={full_svc}&time={s8[0]}")
n_after = ctx(lambda: D.scalar("SELECT COUNT(*) FROM appointments", (), 0))
check("مسیر بازگشت از ورود، روز/ساعت را از کوئری نمی‌خواند (CSRF روی GET)",
      n_after == n_appt, (r_g.status_code, n_appt, n_after))
check("و به جای ساختن نوبت، کاربر را به صفحهٔ انتخاب زمان می‌برد",
      r_g.status_code in (200, 302), r_g.status_code)

# ---- نشانی بازگشت (next/back): فقط داخلی
check("safe_next نشانیِ بیرونی، پروتکل‌دار و «//» را رد می‌کند",
      all(safe_next(t, "/in") == "/in" for t in
          ("//evil.com/x", "/\\evil.com", "https://evil.com", "javascript:alert(1)",
           "@evil.com", "\\evil.com", "", "/\r\n/x")),
      [safe_next(t, "/in") for t in ("//evil.com", "/\\evil.com", "https://e.com", "javascript:1")])
check("و نشانیِ داخلیِ واقعی را دست‌نخورده نگه می‌دارد",
      safe_next("/pankack?tab=1", "/in") == "/pankack?tab=1"
      and safe_next("/k/arasteh-beauty", "") == "/k/arasteh-beauty")
c_or = new_client()
c_or.post("/vorood", data={"mobile": "09395557004", "next": "//evil.com"})
code_or = ctx(lambda: D.scalar("SELECT code FROM otp_codes WHERE mobile=? ORDER BY id DESC LIMIT 1",
                               ("09395557004",), ""))
r_or = c_or.post("/tayid", data={"mobile": "09395557004", "code": code_or,
                                 "next": "//evil.com"}, follow_redirects=False)
loc_or = r_or.headers.get("Location", "")
check("ورود با next=//evil.com کاربر را به دامنهٔ مهاجم نمی‌فرستد",
      r_or.status_code == 302 and loc_or.startswith("/") and not loc_or.startswith("//")
      and not loc_or.startswith("\\"), loc_or)

# ---- کوکیِ سالمِ کاربرِ غیرفعال‌شده
u_or = ctx(lambda: D.query_d("SELECT * FROM users WHERE mobile=?", ("09395557004",), one=True))
ctx(lambda: D.execute("UPDATE users SET is_active=0 WHERE id=?", (u_or["id"],)))
r_off = c_or.get("/pankack")
check("کاربر غیرفعال‌شده با کوکیِ پیشین هم به پنل نمی‌رسد",
      r_off.status_code in (302, 403) and not logged_in(c_or), r_off.status_code)
check("و کوکیِ نشستِ آن حساب بازنویسی می‌شود (نشستِ تعلیق‌شده زنده نمی‌ماند)",
      "session=" in r_off.headers.get("Set-Cookie", ""), dict(r_off.headers))
r_offb = c_or.post(f"/k/{full_slug}/zan", data={"date": d8, "service": full_svc, "time": s8[-1]})
check("و رزرو هم نمی‌تواند بسازد", r_offb.status_code in (302, 403), r_offb.status_code)
ctx(lambda: D.execute("UPDATE users SET is_active=1 WHERE id=?", (u_or["id"],)))

# ---- لغوِ هم‌زمان: یک بازپرداخت، نه دو تا
n_before_ref = ctx(lambda: D.scalar("SELECT COUNT(*) FROM refunds WHERE appointment_id=?",
                                    (a8["id"],), 0))
prev_ev = B.cancellation_preview(a8)
want_ref = 1 if (prev_ev["refund_amount"] or 0) > 0 else 0
res_c = r_hammer(lambda i: ctx(lambda i=i: B.cancel_appointment(a8["id"], "آزمون همزمانی")), 6)
oks = [x for x in res_c if isinstance(x, tuple) and x[0]]
check("شش لغوی هم‌زمان: فقط یک بار «لغو شد»", len(oks) == 1,
      [(x[0] if isinstance(x, tuple) else x) for x in res_c])
check("هیچ تردی استثنا نگرفت", all(isinstance(x, tuple) for x in res_c), res_c)
n_ref_after = ctx(lambda: D.scalar("SELECT COUNT(*) FROM refunds WHERE appointment_id=?",
                                   (a8["id"],), 0))
check("بازپرداختِ این نوبت هرگز دوبرابر نشد", n_ref_after == n_before_ref + want_ref,
      (n_before_ref, want_ref, n_ref_after))
st_a8 = ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (a8["id"],), ""))
check("نوبت در وضعیت لغو مانده", st_a8 == "cancelled", st_a8)
r_late = ctx(lambda: B.finalize_payment(a8["id"], "AUTH-AFTER-CANCEL", "TRACE-AFTER"))
check("تأیید پرداختِ نوبتِ لغوشده نوبت را زنده نمی‌کند",
      isinstance(r_late, tuple) and not r_late[0], r_late)
check("و وضعیت همان «cancelled» می‌ماند",
      ctx(lambda: D.scalar("SELECT status FROM appointments WHERE id=?", (a8["id"],), "")) == "cancelled")

# ---- جابه‌جایی زمان: فقط به ساعتِ واقعی و آزاد (همه از نوی‌خوانیِ تازه)
a7 = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a7["id"],), one=True))
old_date, old_hm = a7["date"], a7["start_hm"]
d9, s9 = first_free_day(full_id, full_svc, min_off=2)
loc9, _b9, _r9 = book(cu, full_slug, full_svc, d9, s9[0])
pay(cu, gateway_of(loc9), "success")
r_res = cu.post(f"/nobat/{a7['id']}/javabeja", data={"date": d9, "time": s9[0]},
                follow_redirects=True)
check("جابه‌جایی به ساعتِ رزروشده رد می‌شود و نوبت سرِ جایش می‌ماند",
      "رزرو شده" in r_res.get_data(as_text=True)
      and ctx(lambda: D.scalar("SELECT start_hm FROM appointments WHERE id=?", (a7["id"],), "")) == old_hm,
      flash_of(cu, f"/nobat/{a7['id']}")[:120])
r_res2 = cu.post(f"/nobat/{a7['id']}/javabeja", data={"date": d9, "time": "25:71"},
                 follow_redirects=True)
check("ساعتِ بیرون از تقویم (۲۵:۷۱) نوبت را خراب نمی‌کند",
      r_res2.status_code == 200
      and ctx(lambda: D.scalar("SELECT start_hm FROM appointments WHERE id=?", (a7["id"],), "")) == old_hm,
      r_res2.status_code)
d10, s10 = first_free_day(full_id, full_svc, min_off=4)
cu.post(f"/nobat/{a7['id']}/javabeja", data={"date": d10, "time": s10[0]}, follow_redirects=True)
a10 = ctx(lambda: D.query_d("SELECT * FROM appointments WHERE id=?", (a7["id"],), one=True))
check("جابه‌جایی درست انجام شد", a10["date"] == d10 and a10["start_hm"] == s10[0],
      (a10["date"], a10["start_hm"], flash_of(cu, f"/nobat/{a7['id']}")[:120]))
lk_old = ctx(lambda: D.query_d("SELECT * FROM slot_locks WHERE business_id=? AND date=? AND start_hm=?",
                               (full_id, old_date, old_hm), one=True))
check("قفل «booked» ساعت کهنه هم آزاد شد (نه فقط قفل سبد)",
      lk_old is None or lk_old["status"] == "released", lk_old and lk_old["status"])
taken_old = [x["hm"] for x in ctx(lambda: B.slot_times(full_id, old_date,
                                                        SVC[full_id][0]["duration_min"])) if x.get("taken")]
check("و آن ساعت در تقویم دوباره خالی دیده می‌شود", old_hm not in taken_old, (old_hm, taken_old[:6]))

# ---- جاروی پیامکِ بی‌نتیجه
mob_j = "09395557005"
old_sms = ctx(lambda: D.execute("INSERT INTO sms_log(mobile,template,body,status,created_at) "
                                 "VALUES(?,?,?,?,?)",
                                 (mob_j, "reminder", "متن آزمایشی", "queued",
                                  (datetime.now() - timedelta(minutes=40)).strftime(
                                      "%Y-%m-%d %H:%M:%S"))))
n_reaped = ctx(lambda: D.reap_stuck_sms(minutes=15))
check("پیامکِ «در حال ارسال»ِ بی‌نتیجه پس از مهلت به failed می‌رود",
      n_reaped >= 1 and ctx(lambda: D.scalar("SELECT status FROM sms_log WHERE id=?", (old_sms,), "")) == "failed",
      n_reaped)
check("و پیامکِ تازه دست‌نخورده می‌ماند",
      ctx(lambda: D.scalar("SELECT status FROM sms_log WHERE mobile=? AND template='otp' "
                           "ORDER BY id DESC LIMIT 1", (m_mask,), ""))
      in ("sent", "queued", "sending"))

# ---- بی‌اعتبارسازی کش: شکل‌های نوشتن
check("regexِ جدول، شکل‌های گوناگونِ نوشتن را می‌شناسد",
      all(CACHE.table_of(q) == "categories" for q in (
          "INSERT INTO categories(a) VALUES(1)",
          "INSERT OR REPLACE INTO categories(a) VALUES(1)",
          "insert or ignore into categories(a) VALUES(1)",
          "UPDATE categories SET a=1",
          "UPDATE OR IGNORE categories SET a=1",
          "DELETE FROM categories",
          'INSERT INTO "categories"(a) VALUES(1)')),
      [CACHE.table_of(q) for q in ("INSERT OR REPLACE INTO categories(a) VALUES(1)",
                                   "UPDATE OR IGNORE categories SET a=1")])
k_set, key_set = "smoke:settings_probe", "smoke_probe_key"
ctx(lambda: D.set_setting(key_set, "one"))
v0 = ctx(lambda: CACHE.remember(k_set, lambda: D.scalar(
    "SELECT value FROM settings WHERE key=?", (key_set,), "?"), tables=("settings",)))
ctx(lambda: D.set_setting(key_set, "two"))
v1 = ctx(lambda: CACHE.remember(k_set, lambda: D.scalar(
    "SELECT value FROM settings WHERE key=?", (key_set,), "?"), tables=("settings",)))
check("نوشتنِ تنظیمات با ON CONFLICT هم کش را بی‌اعتبار می‌کند",
      v0 == "one" and v1 == "two", (v0, v1))
ctx(lambda: D.execute("DELETE FROM settings WHERE key=?", (key_set,)))
ctx(lambda: CACHE.touch("settings"))

# ---- لایهٔ پروکسی + گارد Host روی پشتهٔ واقعی
from flask import Flask as _F35, request as _req35                       # noqa: E402
probe = _F35("stack35")
probe.config["TRUSTED_HOSTS"] = ("example.com", "localhost", "127.0.0.1")
probe.config["BEHIND_PROXY"] = True
probe.add_url_rule("/who", "who", view_func=lambda: "%s|%s|%s" % (
    _req35.host, _req35.remote_addr, _req35.environ.get("nobatin.peer_addr", "-")))
probe.wsgi_app = appmod.host_gate(probe, probe.wsgi_app)
appmod.apply_proxy(probe)                    # همان ترتیب create_app: gate → ProxyFix → peer_stamp
probe.wsgi_app = appmod.peer_stamp(probe.wsgi_app)
with probe.test_client() as pc:
    xf_host = pc.get("/who", headers={"X-Forwarded-Host": "evil.com"}).status_code
    ok_host = pc.get("/who", headers={"X-Forwarded-Host": "example.com"})
    forged = pc.get("/who", headers={"X-Forwarded-For": "127.0.0.1"},
                    environ_base={"REMOTE_ADDR": "203.0.113.9"}).get_data(as_text=True)
    chain = pc.get("/who", headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"},
                   environ_base={"REMOTE_ADDR": "203.0.113.9"}).get_data(as_text=True)
check("جعل هاست از راه X-Forwarded-Host هم ۴۰۰ می‌گیرد", xf_host == 400, xf_host)
check("هاستِ مجازِ همان سرصفحه پذیرفته می‌شود", ok_host.status_code == 200
      and ok_host.get_data(as_text=True).startswith("example.com"), ok_host.get_data(as_text=True))
check("IPِ خوانده‌شده فقط از آخرین مقدارِ XFFِ افزوده‌شده توسط پروکسی معتبر می‌آید",
      chain.endswith("|203.0.113.9|203.0.113.9"), chain)
check("با XFFِ تک‌مقداریِ جعلی، ردپای سوکت (ملاک /healthz و سقفِ نرخ) همان IPِ واقعی می‌ماند",
      forged.split("|")[2] == "203.0.113.9", forged)
check("/healthz با XFF جعلی از بیرون باز نمی‌شود",
      new_client().get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"},
                       environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 404)

# ---- پایش‌های کوچکِ بازگشتی
check("؟shift= خراب صفحهٔ رزرو را ۵۰۰ نمی‌کند",
      new_client().get(f"/k/{full_slug}/rezerv?shift=zzz").status_code in (200, 302))
check("؟date= خراب هم همان‌طور",
      new_client().get(f"/k/{full_slug}/rezerv?date=not-a-date&shift=99").status_code in (200, 302))
bo_a = ctx(lambda: business_or_404(full_slug))
bo_b = ctx(lambda: business_or_404(full_id))
check("business_or_404 هم با نشانی و هم با شناسه کار می‌کند",
      bo_a["id"] == full_id == bo_b["id"], (bo_a and bo_a["id"], bo_b and bo_b["id"]))
cm.post(f"/modiriat/biz/{full_id}/commission", data={"value": "900"})
check("کارمزدِ غیرمنطقی از پنل مدیریت اصلاح می‌شود (نه ۵۰۰، نه ۹۰۰٪)",
      ctx(lambda: D.scalar("SELECT commission_percent FROM businesses WHERE id=?",
                           (full_id,), 0)) <= 50)

print("\n" + ("همهٔ بررسی‌ها پاس شد ✅" if not fails else f"❌ {len(fails)} مورد رد شد:\n   " + "\n   ".join(fails)))
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
sys.exit(1 if fails else 0)
