# -*- coding: utf-8 -*-
"""نوبتین — ابزارهای کمکی: تقویم شمسی، اعداد فارسی، پول،قانون کنسلی."""
import json
import math
import re
import secrets
import unicodedata
from datetime import datetime, date, time, timedelta

import jdatetime

# ---------------------------------------------------------------- اعداد / متن
DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def _warn(msg, *args):
    """هشدار در لاگ اپ؛ بیرون از app context (اسکریپت/cron) بی‌صدا ولی بی‌ضرر."""
    try:
        from flask import current_app
        current_app.logger.warning(msg, *args)
    except Exception:
        pass


def fa(value):
    """تبدیل رقم‌های لاتین به فارسی."""
    if value is None:
        return ""
    return str(value).translate(str.maketrans("0123456789", DIGITS))


def fa_money(value):
    """1200000 -> ۱٬۲۰۰٬۰۰۰"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    return fa(f"{n:,}").replace(",", "٬")


def fa_date(dt, with_year=True):
    """تاریخ شمسی خوانا از datetime/date/str."""
    j = to_jdate(dt)
    if j is None:
        return ""
    wd = WEEKDAYS_FA[j.weekday()]
    if with_year:
        return f"{wd} {fa(j.day)} {JALALI_MONTHS[j.month - 1]} {fa(j.year)}"
    return f"{wd} {fa(j.day)} {JALALI_MONTHS[j.month - 1]}"


def fa_datetime(dt):
    j = to_jdate(dt)
    if j is None:
        return ""
    return f"{fa_date(j)} — ساعت {fa(j.strftime('%H:%M'))}"


JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
JALALI_MONTHS_SHORT = ["فرو", "ارد", "خرد", "تیر", "مرد", "شهر", "مهر", "آبا", "آذر", "دی", "بهم", "اسف"]
WEEKDAYS_SHORT = ["ش", "ی", "د", "س", "چ", "پ", "ج"]  # شنبه تا جمعه
WEEKDAYS_FA = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]


def jweekday(value):
    """شنبه=0 ... جمعه=6 — همان ترتیبی که در دیتابیس (working_hours.weekday) ذخیره می‌شود.
    ورودی: date/datetime میلادی یا رشتهٔ ISO."""
    d = value
    if isinstance(d, str):
        d = parse_dt(d)
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, jdatetime.date):
        return d.weekday()
    return (d.weekday() + 1) % 7


def is_jalali_leap(jyear):
    v = jdatetime.date(int(jyear), 1, 1).isleap
    return bool(v() if callable(v) else v)


def jalali_month_length(jyear, jmonth):
    """۶ ماه اول ۳۱ روزه، ۵ ماه بعد ۳۰ روزه، اسفند ۲۹/۳۰."""
    jmonth = int(jmonth)
    if jmonth <= 6:
        return 31
    if jmonth <= 11:
        return 30
    return 30 if is_jalali_leap(jyear) else 29


def to_jdate(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_dt(value)
    if isinstance(value, datetime):
        # datetime جdatetime ساعت را هم نگه می‌دارد (برای fa_datetime لازم است)
        return jdatetime.datetime.fromgregorian(datetime=value)
    if isinstance(value, date):
        return jdatetime.date.fromgregorian(date=value)
    return value if isinstance(value, jdatetime.date) else None


def to_jdatetime(value):
    if isinstance(value, str):
        value = parse_dt(value)
    if isinstance(value, datetime):
        return jdatetime.datetime.fromgregorian(datetime=value)
    return value


def parse_dt(value):
    """انعطاف‌پذیر: 'YYYY-MM-DD HH:MM(:SS)' / 'YYYY-MM-DD' / datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def jalali_to_date(jy, jm, jd):
    return jdatetime.date(jy, jm, jd).togregorian()


def month_grid(jyear, jmonth):
    """تقویم ماه شمسی: لیست هفته‌ها؛ هر خانه dict(day, date, in_month, today, past)."""
    first_g = jalali_to_date(jyear, jmonth, 1)
    days_in_month = jalali_month_length(jyear, jmonth)
    today_g = date.today()
    # شنبه=0 ... جمعه=6
    lead = jweekday(first_g)
    cells = []
    start = first_g - timedelta(days=lead)
    total = math.ceil((lead + days_in_month) / 7) * 7
    for i in range(total):
        g = start + timedelta(days=i)
        j = jdatetime.date.fromgregorian(date=g)
        cells.append({
            "day": j.day,
            "iso": g.isoformat(),
            "in_month": j.month == jmonth and j.year == jyear,
            "is_today": g == today_g,
            "is_past": g < today_g,
            "weekday": WEEKDAYS_SHORT[jweekday(g)],
            "weekday_fa": WEEKDAYS_FA[jweekday(g)],
        })
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    return {"weeks": weeks, "year": jyear, "month": jmonth,
            "month_name": JALALI_MONTHS[jmonth - 1],
            "prev": _neighbor(jyear, jmonth, -1), "next": _neighbor(jyear, jmonth, 1)}


def _neighbor(jy, jm, delta):
    idx = (jy * 12 + (jm - 1)) + delta
    y, m = idx // 12, idx % 12 + 1
    return {"y": y, "m": m, "iso": jalali_to_date(y, m, 1).isoformat()}


def upcoming_days(count=14, skip_days=0):
    """نوار روزهای جلوی تقویم."""
    out = []
    today = date.today()
    for i in range(count):
        g = today + timedelta(days=i + skip_days)
        j = jdatetime.date.fromgregorian(date=g)
        out.append({
            "iso": g.isoformat(),
            "day": fa(j.day),
            "weekday": WEEKDAYS_SHORT[jweekday(g)],
            "month": JALALI_MONTHS_SHORT[j.month - 1],
            "today": i == 0,
        })
    return out


def minutes_label(minutes):
    h, m = divmod(int(minutes or 0), 60)
    parts = []
    if h:
        parts.append(f"{fa(h)} ساعت")
    if m:
        parts.append(f"{fa(m)} دقیقه")
    return " ".join(parts) or f"{fa(minutes)} دقیقه"


# ---------------------------------------------------------------- قانون کنسلی
def hours_until(start_dt, now=None):
    """ساعت مانده تا شروع نوبت؛ می‌تواند منفی باشد (نوبت شروع شده است).

    اگر «شروع» خوانده نشود صفر برمی‌گرداند، نه استثنا: صفر یعنی کمترینِ
    سخاوتِ ممکن در چیدنِ قانونِ بازپرداخت، پس دادهٔ خراب پولی را آزاد نمی‌کند و
    مسیر لغو هم ۵۰۰ نمی‌شود.
    """
    start = parse_dt(start_dt)
    if start is None:
        _warn("hours_until: unreadable appointment start %r", start_dt)
        return 0.0
    return (start - (now or datetime.now())).total_seconds() / 3600.0


def parse_policy(value):
    """هر شکلِ قانون کنسلی را به فهرستِ پاکِ مرتب‌شده تبدیل می‌کند.

    ورودی می‌تواند رشتهٔ JSON، فهرست، یک دیکشنریِ تنها، یا هیچ باشد — و ستون
    cancel_policy در دیتابیس دقیقاً همان چیزی است که یک نفر (یا دادهٔ نمونهٔ قدیمی)
    نوشته است. این تابع هیچ‌وقت استثنا نمی‌دهد و هیچ مقدارِ غیرقابل‌اعتمادی را
    دست‌نخورده رد نمی‌کند:

      * عناصر ناآشنا (عدد، رشته، None) دور ریخته می‌شوند — وگرنه `r.get` در
        evaluate_cancellation می‌ترکد و کاربر سرِ «لغو نوبت» صفحهٔ ۵۰۰ می‌بیند.
      * قانونِ ساعتِ بی‌معنا (منفی، یا بیشتر از یک سال) دور ریخته می‌شود. «-۵ ساعت»
        همیشه برقرار است، پس یک بدفرمی = بازپرداخت کاملِ تضمینی برای هر لغوِ دیررس.
      * refund_percent فقط ۰..۱۰۰ می‌ماند (بازپرداختِ بیش از پرداختی معنا ندارد).

    بازگرداندن: [{'min_hours': int, 'refund_percent': int}] از بزرگ‌ترین بازه به
    کوچک‌ترین؛ همان ترتیبی که evaluate_cancellation برای «اولِ
    سخاوتمندانه‌ترینِ برقرار» می‌خواهد.
    """
    if isinstance(value, str):
        try:
            rules = json.loads(value or "[]")
        except (ValueError, TypeError) as exc:
            # قانونِ ناخوانا یعنی بازپرداخت صفر؛ باید در لاگ ثبت شود، نه بی‌صدا.
            _warn("cancel policy unreadable (%s): %.60s", exc, value)
            rules = []
    else:
        rules = value
    if isinstance(rules, dict):
        rules = [rules]
    if not isinstance(rules, list):
        _warn("cancel policy is not a list: %s", type(rules).__name__)
        rules = []
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        try:
            hours = int(r.get("min_hours") or 0)
            percent = int(r.get("refund_percent") or 0)
        except (ValueError, TypeError):
            continue
        if not 0 <= hours <= 24 * 365:
            continue                      # ساعتِ بی‌معنا قانون نیست؛ کل ردیف حذف می‌شود
        out.append({"min_hours": hours, "refund_percent": max(0, min(percent, 100))})
    return sorted(out, key=lambda r: -r["min_hours"])


def evaluate_cancellation(policy, start_dt, now=None):
    """policy = فهرست یا JSONِ قانون‌ها.
    بازگرداندن dict(refund_percent, rule, label, remaining)."""
    rules = parse_policy(policy)
    remaining = hours_until(start_dt, now)
    chosen = None
    for r in rules:
        if remaining >= (r.get("min_hours") or 0):
            chosen = r
            break
    if chosen is None:
        # هیچ قانونی برقرار نیست = داخل بازه غیرقابل کنسلی
        return {"refund_percent": 0, "rule": None,
                "label": "در بازهٔ غیرقابل کنسلی؛ امکان بازپرداخت وجود ندارد",
                "remaining": max(remaining, 0)}
    return {
        "refund_percent": int(chosen.get("refund_percent") or 0),
        "rule": chosen,
        "label": rule_label(chosen),
        "remaining": max(remaining, 0),
    }


def rule_label(rule):
    h = int(rule.get("min_hours") or 0)
    p = int(rule.get("refund_percent") or 0)
    if h >= 24 and h % 24 == 0:
        return f"حداقل {fa(h // 24)} روز تا شروع نوبت → {fa(p)}٪ بازپرداخت"
    if h == 0:
        return f"همیشه → {fa(p)}٪ بازپرداخت"
    return f"حداقل {fa(h)} ساعت تا شروع نوبت → {fa(p)}٪ بازپرداخت"


def is_valid_mobile(value):
    return bool(re.match(r"^09\d{9}$", (value or "").strip()))


def normalize_mobile(value):
    v = (value or "").strip().replace("٬", "").replace(" ", "")
    v = v.translate(str.maketrans(DIGITS, "0123456789"))
    if v.startswith("۹") or v.startswith("9"):
        v = "0" + v
    if v.startswith("+98"):
        v = "0" + v[3:]
    return v


def norm_place(name):
    """کلیدِ یک‌شکلِ نام مکان، برای اینکه املاهای مختلف یک محله را دو تکه نکنند.

    «سعادت آباد» = «سعادت‌آباد» = «سعادت  آباد» → همه یک چیز. نیم‌فاصله به فاصله
    تبدیل می‌شود، حرکات عربی و ی/کِ عربی با معادل فارسی جانشین می‌شوند.
    """
    s = unicodedata.normalize("NFKC", (name or "").strip())
    s = s.replace("\u200c", " ").replace("\u200d", "")
    s = re.sub(r"[\u064b-\u0655\u0670\u0640]", "", s)          # حرکت‌ها، همزه‌های ریز و کشیده
    s = s.translate(str.maketrans("يکك", "یکک"))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def like_pattern(term):
    """الگوی LIKE برای جست‌وجو؛ املاهای مختلفِ یک واژه را هم پیدا می‌کند.

    «كلينيك» (عربی) = «کلینیک»، و «کسب وکار» با فاصله = «کسب‌وکار» با نیم‌فاصله
    (فاصله جای خود را به % می‌دهد). کاراکترهای ویژهٔ LIKE یعنی ! % _ فرار می‌شوند،
    پس کوئری باید `ESCAPE '!'` داشته باشد. برای ورودیِ بی‌محتوا None برمی‌گرداند.
    """
    s = norm_place(term)
    if not s:
        return None
    s = re.sub(r"([!%_])", r"!\1", s)
    return "%" + s.replace(" ", "%") + "%"


def otp_code():
    return f"{secrets.randbelow(90000) + 10000}"


def reference(prefix="TRX"):
    """کد پیگیری یکتا: پیش‌وند + زمان + ۶ رقم هگز تصادفی (۱۶.۷ میلیون حالت در هر ثانیه).

    ستون reference روی تراکنش‌ها UNIQUE است؛ پس هیچ‌گاه نباید دو کد یکسان ساخت.
    """
    return f"{prefix}{datetime.now().strftime('%y%m%d%H%M%S')}{secrets.token_hex(3).upper()}"
