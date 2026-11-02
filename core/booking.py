# -*- coding: utf-8 -*-
"""منطق رزرو نوبتین: تولید زمان‌های آزاد، قفل ضد هم‌زمانی، پرداخت، کنسلی و بازپرداخت."""
import json
import secrets
import sqlite3
from datetime import datetime, date, timedelta

from flask import current_app

from core.db import (execute, changed, get_db, query, query_d, scalar, now_str, send_sms,
                     setting, business_owner_mobile, sms_sent, tx, Abort)
from core.jalali import (evaluate_cancellation, fa, fa_date, fa_money, hours_until,
                         parse_dt, reference, rule_label, to_jdate,
                         jweekday, jalali_month_length, jalali_to_date)

WEEKDAY_ORDER = "شنبه یکشنبه دوشنبه سه‌شنبه چهارشنبه پنج‌شنبه جمعه".split()


def hm_to_min(hm):
    h, m = str(hm).split(":")
    return int(h) * 60 + int(m)


def min_to_hm(v):
    v = max(0, int(v))
    return f"{v // 60 % 24:02d}:{v % 60:02d}"


def working_hours(business_id, weekday):
    return query_d("SELECT * FROM working_hours WHERE business_id=? AND weekday=? AND is_active=1",
                   (business_id, weekday))


def is_blocked(business_id, iso_date):
    return bool(scalar("SELECT COUNT(*) FROM day_blocks WHERE business_id=? AND date=?",
                       (business_id, iso_date), 0))


def slot_summary(business_id, iso_date, duration_min=30):
    """خلاصهٔ روز: تعداد آزاد، فهرست زمان‌های آزاد، تعطیل/پر بودن."""
    slots = slot_times(business_id, iso_date, duration_min)
    free = [s["hm"] for s in slots if not s.get("past") and not s.get("taken")]
    return {"free": len(free), "first": free[0] if free else None,
            "last": free[-1] if free else None,
            "closed": bool(slots) is False and is_blocked(business_id, iso_date),
            "no_hours": not slots}


def month_availability(business_id, jyear, jmonth, duration_min=30):
    """تعداد زمان‌های آزاد هر روز یک ماه شمسی — با کوئری toplu و محاسبه در پایتون."""
    from datetime import time as _t
    first_g = jalali_to_date(int(jyear), int(jmonth), 1)
    n_days = jalali_month_length(jyear, jmonth)
    last_g = first_g + timedelta(days=n_days - 1)
    frm, to = first_g.isoformat(), last_g.isoformat()

    hours = {}
    for r in query_d("SELECT * FROM working_hours WHERE business_id=? AND is_active=1", (business_id,)):
        hours.setdefault(r["weekday"], []).append(r)
    blocked = {r["date"] for r in query_d(
        "SELECT date FROM day_blocks WHERE business_id=? AND date BETWEEN ? AND ?",
        (business_id, frm, to))}
    taken = set()
    for r in query_d("SELECT date, start_hm FROM appointments WHERE business_id=? AND date BETWEEN ? AND ? "
                     "AND status IN ('pending_payment','confirmed')", (business_id, frm, to)):
        taken.add((r["date"], r["start_hm"]))
    expire_locks()
    for r in query_d("SELECT date, start_hm FROM slot_locks WHERE business_id=? AND date BETWEEN ? AND ? "
                     "AND status IN ('hold','booked')", (business_id, frm, to)):
        taken.add((r["date"], r["start_hm"]))

    now = datetime.now()
    today_iso = now.date().isoformat()
    out = {}
    for i in range(n_days):
        g = first_g + timedelta(days=i)
        iso = g.isoformat()
        count = 0
        if iso not in blocked and jweekday(g) in hours:
            for rule in hours[jweekday(g)]:
                t = hm_to_min(rule["start_hm"])
                end = hm_to_min(rule["end_hm"])
                step = int(rule["slot_min"] or 30)
                length = max(int(duration_min or step), step)
                while t + length <= end:
                    hm = min_to_hm(t)
                    is_past = iso < today_iso or (iso == today_iso and t <= now.hour * 60 + now.minute + 30)
                    if not is_past and (iso, hm) not in taken:
                        count += 1
                    t += step
        out[iso] = count
    return out


def expire_locks():
    """آزادسازی قفل‌های بی‌زمان شده (سبد رهاشده)."""
    execute("UPDATE slot_locks SET status='released' WHERE status='hold' AND expires_at < ?",
            (now_str(),))


def slot_times(business_id, iso_date, duration_min=30, buffer_min=0, now=None):
    """لیست زمان‌های آزادِ آن روز. هر خانه: dict(hm, label, taken_by, last_slot)."""
    d = parse_dt(iso_date)
    if not d:
        return []
    gdate = d.date()
    out = []
    if is_blocked(business_id, iso_date):
        return out
    wd = jweekday(gdate)
    rules = working_hours(business_id, wd)
    if not rules:
        return out
    now = now or datetime.now()
    today = now.date()
    expire_locks()
    locks = {r["start_hm"]: r for r in query_d(
        "SELECT * FROM slot_locks WHERE business_id=? AND date=? AND status IN ('hold','booked')",
        (business_id, iso_date))}
    taken = {r["start_hm"] for r in query_d(
        "SELECT start_hm FROM appointments WHERE business_id=? AND date=? "
        "AND status IN ('pending_payment','confirmed')", (business_id, iso_date))}
    taken |= {k for k, v in locks.items() if v["status"] == "booked"}
    for rule in rules:
        start = hm_to_min(rule["start_hm"])
        end = hm_to_min(rule["end_hm"])
        step = int(rule["slot_min"] or 30)
        length = max(int(duration_min or step), step)
        t = start
        while t + length <= end:
            hm = min_to_hm(t)
            info = {"hm": hm, "label": fa(hm), "end_label": fa(min_to_hm(t + length)),
                    "min": t}
            if gdate < today or (gdate == today and t <= now.hour * 60 + now.minute + 30):
                info["past"] = True
            elif hm in taken:
                info["taken"] = True
            elif hm in locks and locks[hm]["status"] == "hold":
                lk = locks[hm]
                if (lk["holder_user_id"] or 0) == current_user_id():
                    info["mine"] = True
                else:
                    info["taken"] = True
            else:
                try:
                    j = to_jdate(gdate)
                    info["jlabel"] = f"{fa(j.day)} {j.month}"
                except Exception:
                    pass
            out.append(info)
            t += step
    # آخرین زمان قابل رزرو در هر بازه
    for i, s in enumerate(out):
        if s.get("past") or s.get("taken"):
            continue
        nxt = out[i + 1] if i + 1 < len(out) else None
        s["is_last"] = (nxt is None or nxt.get("past") or nxt.get("taken")
                        or (nxt["min"] - s["min"]) > 120)
    return out


def current_user_id():
    try:
        from core.helpers import current_user
        u = current_user()
        return u["id"] if u else None
    except Exception:
        return None


def hold_slot(user_id, business_id, service_id, iso_date, hm, minutes=10):
    """قفل موقت زمان. برمی‌گرداند (ok, message|lock).

    «خواندن وضعیت ساعت» و «نوشتن قفل» در یک تراکنش BEGIN IMMEDIATE انجام می‌شود:
    تا آن تراکنش باز است هیچ نویسندهٔ دیگری نمی‌تواند همان ساعت را عوض کند، پس دو
    کاربر هم‌زمان روی یک ساعت، برندهٔ متفاوتی ندارند. در هر زمان فقط یک رکورد قفل
    وجود دارد (UNIQUE کسب‌وکار/روز/ساعت)؛ رکورد «released» (سبد رهاشده یا پرداخت
    ناموفق) بازنشانی می‌شود تا ساعت قفل نماند.
    """
    expire_locks()
    exp = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with tx() as db:
            row = db.execute("SELECT * FROM slot_locks WHERE business_id=? AND date=? AND start_hm=?",
                             (business_id, iso_date, hm)).fetchone()
            lk = dict(row) if row else {}
            if lk.get("status") == "booked":
                raise Abort("این زمان رزرو شده است.")
            if lk.get("status") == "hold" and (lk.get("expires_at") or "") > now_str():
                if lk["holder_user_id"] == user_id:
                    db.execute("UPDATE slot_locks SET expires_at=?, service_id=? WHERE id=?",
                               (exp, service_id, lk["id"]))
                    lk["expires_at"] = exp
                    lk["service_id"] = service_id
                    return True, lk
                raise Abort("این زمان همین حالا در سبد کاربر دیگری است؛ چند لحظه دیگر دوباره تلاش کنید.")
            if lk:  # رکورد released را دوباره فعال می‌کنیم
                db.execute("UPDATE slot_locks SET service_id=?, holder_user_id=?, status='hold', "
                           "expires_at=?, created_at=? WHERE id=?",
                           (service_id, user_id, exp, now_str(), lk["id"]))
                lock_id = lk["id"]
            else:
                lock_id = db.execute(
                    "INSERT INTO slot_locks(business_id,service_id,date,start_hm,holder_user_id,status,"
                    "expires_at,created_at) VALUES(?,?,?,?,?,'hold',?,?)",
                    (business_id, service_id, iso_date, hm, user_id, exp, now_str())).lastrowid
        return True, {"id": lock_id, "expires_at": exp}
    except Abort as bad:  # nothing was written in this transaction
        return False, bad.message
    except sqlite3.IntegrityError as exc:  # رقابت هم‌زمان واقعی
        current_app.logger.warning("hold conflict: %s", exc)
        return False, "این زمان دقیقاً هم‌زمان رزرو شد. لطفاً زمان دیگری انتخاب کنید."
    except Exception as exc:
        current_app.logger.exception("hold failed: %s", exc)
        return False, "امکان ثبت زمان انتخابی نیست؛ لطفاً دوباره تلاش کنید."


def release_slot(business_id, iso_date, hm, user_id=None, include_booked=False):
    """آزاد کردن زمان: رکورد می‌ماند و فقط وضعیت به released تغییر می‌کند تا
    رکورد UNIQUE برای همان ساعت دوباره قابل استفاده باشد.

    دو حالتِ «وضعیت مجاز» دارد، چون دو کارِ متفاوت از این تابع درآمد می‌کند:
      * رهاکردن سبد خرید (پیش‌فرض): فقط قفل `hold` خودِ مشتری. قفل `booked` کاربر
        دیگر هرگز نباید با یک فراخوانِ بی‌دقت آزاد شود.
      * جابه‌جایی نوبت قطعی (`include_booked=True`): قفل همان نوبت status='booked'
        است، پس فیلتر `hold` هیچ ردیفی را آزاد نمی‌کرد و ساعت کهنه تا ابد «پر»
        می‌ماند. هنوز هم فقط قفلِ همان holder جابه‌جا می‌شود.
    """
    keep = "status<>'released'" if include_booked else "status='hold'"
    if user_id is None:
        execute("UPDATE slot_locks SET status='released', expires_at=NULL "
                "WHERE business_id=? AND date=? AND start_hm=? AND status<>'released'",
                (business_id, iso_date, hm))
    else:
        execute("UPDATE slot_locks SET status='released', expires_at=NULL "
                "WHERE business_id=? AND date=? AND start_hm=? AND %s AND holder_user_id=?"
                % keep, (business_id, iso_date, hm, user_id))


# ---------------------------------------------------------------- .create / pay
def new_appointment_code():
    """کد کوتاه یکتای نوبت (NB-۱۲۳۴۵۶) — با بررسی تکرار در دیتابیس."""
    for _ in range(25):
        code = f"NB-{secrets.randbelow(900000) + 100000}"
        if not query("SELECT id FROM appointments WHERE code=?", (code,), one=True):
            return code
    return f"NB-{reference('')[2:]}"


def create_pending_appointment(user, business, service, iso_date, hm):
    """ساخت نوبت در وضعیت «در انتظار پرداخت» — هنوز قطعی نیست.

    قفل ساعت (سبد) جدا و کوتاه گرفته می‌شود و بعد نوبت با همان تراکنش اتمیک
    ثبت می‌گردد؛ اگر هر چیزی در ثبت نوبت بخورد، نه نوبتی می‌ماند نه پیامکی.
    """
    hold = hold_slot(user["id"], business["id"], service["id"], iso_date, hm,
                     business.get("hold_minutes") or 10)
    if not hold[0]:
        return None, hold[1]
    length = service["duration_min"] or 30
    amount = service["price"] or 0
    mode = business["payment_mode"]
    if mode == "free":
        payable = 0
    elif mode == "deposit":
        payable = min(int(business["deposit_amount"] or 0), amount)
    else:
        payable = amount
    code = new_appointment_code()
    try:
        with tx():
            try:
                appt_id = execute(
                    "INSERT INTO appointments(code,user_id,business_id,service_id,date,start_hm,end_hm,amount,"
                    "paid_amount,payable_now,status,pay_status,cancel_policy,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, user["id"], business["id"], service["id"], iso_date, hm,
                     min_to_hm(hm_to_min(hm) + length), amount, 0, payable,
                     "pending_payment" if payable else "confirmed", "unpaid" if payable else "paid",
                     business["cancel_policy"] or "[]", now_str()))
            except sqlite3.IntegrityError as exc:  # گارد دیتابیس در برابر رزرو هم‌زمان
                current_app.logger.warning("appointment clash: %s", exc)
                raise Abort("clash")
            if not payable:   # پرداخت در محل: نوبت از همین لحظه قطعی است
                execute("UPDATE appointments SET confirmed_at=? WHERE id=?", (now_str(), appt_id))
                execute("UPDATE slot_locks SET status='booked', expires_at=NULL "
                        "WHERE business_id=? AND date=? AND start_hm=? AND holder_user_id=?",
                        (business["id"], iso_date, hm, user["id"]))
                send_sms(user["mobile"], "booking_ok", _appt_id=appt_id, biz=business["name"],
                         date=fa_date(iso_date), time=fa(hm), code=code)
                send_sms(business_owner_mobile(business["id"]), "new_booking", _appt_id=appt_id,
                         biz=business["name"], service=service["title"],
                         date=fa_date(iso_date), time=fa(hm))
    except Abort:
        release_slot(business["id"], iso_date, hm, user["id"])
        return None, "این زمان دقیقاً هم‌زمان رزرو شد. لطفاً زمان دیگری انتخاب کنید."
    except Exception as exc:
        current_app.logger.exception("appointment create failed: %s", exc)
        release_slot(business["id"], iso_date, hm, user["id"])
        return None, "امکان ثبت نوبت نیست؛ لطفاً دوباره تلاش کنید."
    return appt_id, None


def transaction_for(appt_id, kind="payment", amount=0, status="created"):
    appt = query_d("SELECT * FROM appointments WHERE id=?", (appt_id,), one=True)
    ref = reference("RTX" if kind == "refund" else "TRX")
    tid = execute(
        "INSERT INTO transactions(reference,appointment_id,user_id,business_id,kind,amount,status,"
        "gateway,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (ref, appt_id, appt["user_id"], appt["business_id"], kind, amount, status, "sandbox",
         now_str()))
    return tid, ref


def new_token_and_tx(appt_id, amount):
    """تراکنش درگاه تازه (وضعیت created) با توکن sandbox برمی‌گرداند.
    اگر تراکنش بازِ همان نوبت وجود داشته باشد، همان توکن برگردانده می‌شود."""
    from core.gateway import new_token
    open_tx = query_d("SELECT * FROM transactions WHERE appointment_id=? AND kind='payment' "
                      "AND status IN ('created','pending')", (appt_id,), one=True)
    if open_tx:
        return open_tx["auth_code"]
    token = new_token()
    appt = query_d("SELECT * FROM appointments WHERE id=?", (appt_id,), one=True)
    execute("INSERT INTO transactions(reference,appointment_id,user_id,business_id,kind,amount,"
            "status,gateway,auth_code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (reference("TRX"), appt_id, appt["user_id"], appt["business_id"], "payment", amount,
             "created", "sandbox", token, now_str()))
    return token


def finalize_payment(appt_id, auth_code="", trace=""):
    """فقط پس از تأیید درگاه: نوبت قطعی و قفل زمان ماندگار می‌شود.

    «آیا این ساعت هم‌زمان قطعی شده؟» داخل همان تراکنش نوشتن پرسیده می‌شود، پس
    پاسخ تا پایان commit معتبر است — دو برندهٔ هم‌زمان نداریم.
    """
    appt = query_d("SELECT * FROM appointments WHERE id=?", (appt_id,), one=True)
    if not appt:
        return False, "نوبت یافت نشد."
    if appt["status"] == "confirmed":
        return True, "نوبت قبلاً تأیید شده است."
    if appt["status"] in ("expired", "cancelled"):
        return False, "این رزرو منقضی شده است؛ زمان دیگری انتخاب کنید."
    try:
        with tx() as db:
            if db.execute(
                    "SELECT id FROM appointments WHERE business_id=? AND date=? AND start_hm=? "
                    "AND status IN ('pending_payment','confirmed') AND id<>?",
                    (appt["business_id"], appt["date"], appt["start_hm"], appt_id)).fetchall():
                raise Abort("clash")
            # قطعی‌کردن فقط از «در انتظار پرداخت» و با بررسی rowcount: دو فراخوان تأیید
            # (یا دو callback درگاه) هم‌زمان → فقط اولی می‌نویسد و پیامک می‌فرستد.
            # «status<>confirmed» کافی نبود: نوبتِ لغو/منقضی‌شده را هم زنده می‌کرد،
            # یعنی هم پول و هم بازپرداخت و هم صندلی برای یک نفر.
            cur = db.execute("UPDATE appointments SET status='confirmed', pay_status='paid', "
                             "paid_amount=payable_now, confirmed_at=? "
                             "WHERE id=? AND status='pending_payment'", (now_str(), appt_id))
            if not cur.rowcount:
                raise Abort("already")
            # فقط قفل همان مشتری برای همان ساعت ماندگار می‌شود (نه ردیف‌های جابه‌جاشده)
            db.execute("UPDATE slot_locks SET status='booked', expires_at=NULL "
                       "WHERE business_id=? AND date=? AND start_hm=? AND holder_user_id=?",
                       (appt["business_id"], appt["date"], appt["start_hm"], appt["user_id"]))
            db.execute("UPDATE transactions SET status='success', auth_code=?, trace=?, done_at=? "
                       "WHERE appointment_id=? AND kind='payment' AND status IN ('created','pending')",
                       (auth_code, trace, now_str(), appt_id))
    except Abort as bad:
        if bad.message == "already":
            now = scalar("SELECT status FROM appointments WHERE id=?", (appt_id,), "")
            if now == "cancelled":
                return False, "این نوبت پیش از تأیید پرداخت لغو شده بود؛ پول بازگردانده می‌شود."
            return True, "نوبت قبلاً تأیید شده است."
        mark_payment_failed(appt_id, "این زمان هم‌زمان رزرو قطعی شده بود.")
        return False, "متأسفانه این زمان در لحظهٔ پرداخت توسط کاربر دیگری قطعی شد."
    except Exception as exc:
        current_app.logger.error("finalize failed: %s", exc)
        return False, "خطا در ثبت نهایی نوبت."
    # نوبت قطعی و commit شده است: هیچ‌چیز در این بخشِ پیامکی نباید خطا بدهد، وگرنه
    # مشتری «۵۰۰» می‌بیند درحالی‌که نوبتش برده است (و پیامک هم نمی‌رود).
    try:
        user = query_d("SELECT * FROM users WHERE id=?", (appt["user_id"],), one=True)
        biz = query_d("SELECT * FROM businesses WHERE id=?", (appt["business_id"],), one=True)
        svc = query_d("SELECT * FROM services WHERE id=?", (appt["service_id"],), one=True)
        if user:
            send_sms(user["mobile"], "booking_ok", _appt_id=appt_id, biz=biz["name"],
                     date=fa_date(appt["date"]), time=fa(appt["start_hm"]), code=appt["code"])
        send_sms(business_owner_mobile(appt["business_id"]), "new_booking", _appt_id=appt_id,
                 biz=biz["name"], service=svc["title"] if svc else "", date=fa_date(appt["date"]),
                 time=fa(appt["start_hm"]))
    except Exception as exc:
        current_app.logger.error("post-confirm sms failed (appt %s): %s", appt_id, exc)
    return True, "نوبت شما قطعی شد."


def mark_payment_failed(appt_id, message="", notify="payment_failed"):
    """پرداخت ناموفق → هیچ نوبت قطعی ایجاد نمی‌شود و زمان آزاد می‌گردد.

    سه نوشتن (تراکنش، نوبت، قفل ساعت) یک تراکنش‌اند: رکورد مالی «ناموفق» بدون
    آزادشدن ساعت، یا نوبت «منقضی» با ساعت هنوز قفل‌شده باقی نمی‌ماند.

    وضعیت نوبت **داخل** همان تراکنش خوانده می‌شود (نه بیرونش): اگر بین خواندن و
    نوشتن، فراخوانِ درگاه دیگر نوبت را قطعی کند، بیرون‌خواندن یعنی «پرداخت
    موفقِ پول‌داده» بعداً به expired برگردد — یعنی نوبتی که مشتری برده است.
    """
    with tx() as db:
        appt = db.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
        if not appt or appt["status"] == "confirmed":
            return                     # نوبت قطعی (یا ناموجود): به هیچ‌چیز دست نمی‌زنیم
        execute("UPDATE transactions SET status='failed', message=?, done_at=? "
                "WHERE appointment_id=? AND kind='payment' AND status IN ('created','pending')",
                (message, now_str(), appt_id))
        # فقط نوبتی که همان لحظه هنوز «در انتظار پرداخت» است منقضی می‌شود
        if appt["status"] == "pending_payment" and changed(
                "UPDATE appointments SET status='expired', pay_status='unpaid' "
                "WHERE id=? AND status='pending_payment'", (appt_id,)):
            # فقط قفلِ همان مشتری آزاد می‌شود؛ قفل «booked» کاربر دیگر دست‌نخورده می‌ماند
            execute("UPDATE slot_locks SET status='released', expires_at=NULL "
                    "WHERE business_id=? AND date=? AND start_hm=? AND holder_user_id=? "
                    "AND status<>'released'",
                    (appt["business_id"], appt["date"], appt["start_hm"], appt["user_id"]))
            if notify:
                send_sms(scalar("SELECT mobile FROM users WHERE id=?", (appt["user_id"],), ""),
                         notify, _appt_id=appt_id, code=appt["code"])


def expire_pending_appointments():
    """نوبت‌های پرداخت‌نشده پس از پایان مهلت قفل، منقضی می‌شوند."""
    rows = query_d("SELECT a.* FROM appointments a JOIN slot_locks l ON "
                   "(l.business_id=a.business_id AND l.date=a.date AND l.start_hm=a.start_hm) "
                   "WHERE a.status='pending_payment' AND l.expires_at < ?", (now_str(),))
    for a in rows:
        mark_payment_failed(a["id"], "مهلت پرداخت تمام شد.", notify="expired")
    return len(rows)


# ----------------------------------------------------------------.cancel
def cancellation_preview(appt):
    policy = appt["cancel_policy"] or "[]"
    ev = evaluate_cancellation(policy, f"{appt['date']} {appt['start_hm']}")
    paid = appt["paid_amount"] or 0
    ev["refund_amount"] = int(round(paid * ev["refund_percent"] / 100.0))
    ev["paid"] = paid
    ev["hours"] = round(max(ev["remaining"], 0), 1)
    return ev


def cancel_appointment(appt_id, reason="", by="customer"):
    """لغو نوبت + ثبت مستقلِ بازپرداخت. همه‌چیز یا هیچ‌چیز: نوبتِ لغوشده بدون
    رکورد بازپرداخت (یا بازپرداخت بدون تراکنش) نمی‌ماند."""
    appt = query_d("SELECT * FROM appointments WHERE id=?", (appt_id,), one=True)
    if not appt:
        return False, "نوبت یافت نشد."
    if appt["status"] == "cancelled":
        return False, "این نوبت قبلاً لغو شده است."
    if appt["status"] == "pending_payment":
        # پرداخت‌نشده: پولی گرفته نشده، پس بازپرداختی هم در کار نیست. سه نوشتن با
        # هم اتمیک می‌شوند: نوبت «لغوشده»، تراکنش «کنسل‌شده»، ساعت آزاد.
        # چرا mark_payment_failed اینجا صدا زده نمی‌شود: آن وضعیت را به «expired»
        # می‌برد، و UPDATE بعدی که «pending_payment» می‌خواست دیگر ردیفی پیدا نمی‌کرد
        # — یعنی لغویِ مشتری در تاریخچه «منقضی» ثبت می‌شد، دلیلِ لغو می‌افتاد و
        # پیامکِ «پرداخت ناموفق» برای کسی که خودش دکمهٔ لغو را زده می‌رفت.
        try:
            with tx() as db:
                row = db.execute("SELECT status FROM appointments WHERE id=?", (appt_id,)).fetchone()
                if not row or row["status"] != "pending_payment":
                    raise Abort("confirmed" if row and row["status"] == "confirmed" else "gone")
                if not changed("UPDATE appointments SET status='cancelled', cancelled_at=?, "
                               "cancel_reason=?, pay_status='unpaid' "
                               "WHERE id=? AND status='pending_payment'", (now_str(), reason, appt_id)):
                    raise Abort("gone")
                execute("UPDATE transactions SET status='cancelled', message=?, done_at=? "
                        "WHERE appointment_id=? AND kind='payment' AND status IN ('created','pending')",
                        ("لغو توسط کاربر پیش از پرداخت.", now_str(), appt_id))
                execute("UPDATE slot_locks SET status='released', expires_at=NULL "
                        "WHERE business_id=? AND date=? AND start_hm=? AND holder_user_id=? "
                        "AND status<>'released'",
                        (appt["business_id"], appt["date"], appt["start_hm"], appt["user_id"]))
        except Abort as bad:
            if bad.message == "confirmed":
                return False, ("این رزرو در همان لحظه قطعی شد؛ برای لغو با بازپرداخت از "
                               "بخش «نوبت‌هایم» استفاده کنید.")
            return False, "این نوبت هم‌زمان لغو یا حذف شده بود."
        try:                                  # نوبت لغو و commit شده؛ پیامک نباید ۵۰۰ بدهد
            biz = query_d("SELECT * FROM businesses WHERE id=?", (appt["business_id"],), one=True)
            send_sms(scalar("SELECT mobile FROM users WHERE id=?", (appt["user_id"],), ""),
                     "cancelled", _appt_id=appt_id, code=appt["code"],
                     biz=biz["name"] if biz else "", refund="مبلغی پرداخت نشده بود")
        except Exception as exc:
            current_app.logger.error("post-cancel sms failed (appt %s): %s", appt_id, exc)
        return True, "رزرو در انتظار پرداخت لغو و زمان آزاد شد."
    ev = cancellation_preview(appt)
    refundable = ev["refund_amount"] > 0 and (appt["paid_amount"] or 0) > 0
    refund_info = None
    try:
        with tx() as db:
            # وضعیت دوباره **داخل** تراکنش خوانده می‌شود: دو لغوی هم‌زمان (دوبار کلیک،
            # یا مشتری و کسب‌وکار با هم) وگرنه دو رکورد بازپرداخت می‌ساختند — یعنی
            # دو برابر پول از جیب پلتفرم، با دو ردیف مالی که هیچ‌کدام واقعی نیست.
            now_status = db.execute("SELECT status FROM appointments WHERE id=?",
                                    (appt_id,)).fetchone()
            if not now_status or now_status["status"] == "cancelled":
                raise Abort("already")
            if not changed("UPDATE appointments SET status='cancelled', cancelled_at=?, "
                           "cancel_reason=?, pay_status=? WHERE id=? AND status<>'cancelled'",
                           (now_str(), reason,
                            "refund_pending" if refundable else appt["pay_status"], appt_id)):
                raise Abort("already")
            execute("UPDATE slot_locks SET status='released', expires_at=NULL "
                    "WHERE business_id=? AND date=? AND start_hm=? AND holder_user_id=? "
                    "AND status<>'released'",
                    (appt["business_id"], appt["date"], appt["start_hm"], appt["user_id"]))
            if refundable:
                tid, ref = transaction_for(appt_id, "refund", ev["refund_amount"], "created")
                rid = execute("INSERT INTO refunds(transaction_id,appointment_id,amount,percent,"
                              "status,rule_label,created_at,reference) VALUES(?,?,?,?,?,?,?,?)",
                              (tid, appt_id, ev["refund_amount"], ev["refund_percent"], "pending",
                               ev["label"], now_str(), ref))
                # در حالت sandbox پول بلافاصله برمی‌گردد؛ فقط وقتی درصدی قابل پرداخت است
                if ev["refund_percent"] > 0:
                    execute("UPDATE refunds SET status='done', done_at=? WHERE id=?", (now_str(), rid))
                    execute("UPDATE transactions SET status='success', done_at=?, trace=? WHERE id=?",
                            (now_str(), ref, tid))
                    execute("UPDATE appointments SET pay_status='refunded' WHERE id=?", (appt_id,))
                refund_info = {"amount": ev["refund_amount"], "percent": ev["refund_percent"],
                               "label": ev["label"], "reference": ref}
    except Abort:
        return False, "این نوبت هم‌زمان لغو شده بود."
    user = query_d("SELECT * FROM users WHERE id=?", (appt["user_id"],), one=True)
    biz = query_d("SELECT * FROM businesses WHERE id=?", (appt["business_id"],), one=True)
    txt = (f"{fa_money(refund_info['amount'])} تومان ({fa(refund_info['percent'])}٪) بازپرداخت می‌شود"
           if refund_info else "طبق قوانین کنسلی، مبلغی قابل بازپرداخت نیست")
    try:                                  # لغو commit شده؛ پیامکِ نگرفته نباید ۵۰۰ بدهد
        send_sms(user["mobile"], "cancelled", _appt_id=appt_id, code=appt["code"], biz=biz["name"],
                 refund=txt)
        if refund_info:
            send_sms(user["mobile"], "refund", _appt_id=appt_id, amount=fa_money(refund_info["amount"]),
                     ref=refund_info["reference"])
        send_sms(business_owner_mobile(appt["business_id"]), "cancelled", _appt_id=appt_id,
                 code=appt["code"], biz=biz["name"], refund="—")
    except Exception as exc:
        current_app.logger.error("post-cancel sms failed (appt %s): %s", appt_id, exc)
    return True, (refund_info or f"نوبت لغو شد؛ طبق قانون «{ev['label']}» مبلغی بازپرداخت نمی‌شود.")


def send_reminders(business_id=None, resends=False):
    """یادآوری نوبت‌های قطعی که تا «reminder_hours» ساعت آینده شروع می‌شوند.

    business_id=None → همهٔ نوبت‌ها (کار خودکار / cron)
    business_id=عدد → فقط نوبت‌های همان کسب‌وکار (دکمهٔ پنل؛ حریم داده)
    هر نوبت یک بار یادآوری می‌گیرد، مگر resends=True.
    """
    sent = 0
    horizon = (datetime.now() + timedelta(hours=int(setting("reminder_hours", 12)))).strftime("%Y-%m-%d %H:%M")
    sql = ("SELECT a.*, u.mobile, b.name AS biz FROM appointments a "
           "JOIN users u ON u.id=a.user_id JOIN businesses b ON b.id=a.business_id "
           "WHERE a.status='confirmed' AND (a.date || ' ' || a.start_hm) <= ? "
           "AND (a.date || ' ' || a.start_hm) > ?")
    args = [horizon, datetime.now().strftime("%Y-%m-%d %H:%M")]
    if business_id:
        sql += " AND a.business_id=?"
        args.append(business_id)
    for a in query_d(sql, tuple(args)):
        if not resends and sms_sent(a["id"], "reminder"):
            continue
        send_sms(a["mobile"], "reminder", _appt_id=a["id"], biz=a["biz"],
                 date=fa_date(a["date"]), time=fa(a["start_hm"]))
        sent += 1
    return sent


def notify_status(appt_id, template):
    """پیامک تغییر وضعیت از سمت کسب‌وکار (انجام شد / حضور نیافت)."""
    a = query_d("SELECT a.*, u.mobile, b.name AS biz FROM appointments a "
                "JOIN users u ON u.id=a.user_id JOIN businesses b ON b.id=a.business_id "
                "WHERE a.id=?", (appt_id,), one=True)
    if not a or template not in ("completed", "no_show"):
        return
    send_sms(a["mobile"], template, _appt_id=appt_id, code=a["code"], biz=a["biz"])


def business_balance(business_id):
    """وضعیت مالی یک کسب‌وکار؛ مستقل از جدول نوبت‌ها."""
    gross = scalar("SELECT COALESCE(SUM(t.amount),0) FROM transactions t "
                   "JOIN appointments a ON a.id=t.appointment_id "
                   "WHERE t.business_id=? AND t.kind='payment' AND t.status='success'",
                   (business_id,), 0)
    refunds = scalar("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE appointment_id IN "
                     "(SELECT id FROM appointments WHERE business_id=?) AND status='done'",
                     (business_id,), 0)
    pending_refunds = scalar("SELECT COALESCE(SUM(r.amount),0) FROM refunds r "
                             "JOIN appointments a ON a.id=r.appointment_id "
                             "WHERE a.business_id=? AND r.status='pending'", (business_id,), 0)
    paid_out = scalar("SELECT COALESCE(SUM(amount),0) FROM settlements WHERE business_id=? "
                      "AND status='paid'", (business_id,), 0)
    fee_percent = scalar("SELECT commission_percent FROM businesses WHERE id=?", (business_id,), 0) or 0
    platform_fee = int(round(gross * float(fee_percent) / 100.0))
    receivable = gross - refunds - pending_refunds - platform_fee - paid_out
    return {"gross": gross, "refunds": refunds, "pending_refunds": pending_refunds,
            "platform_fee": platform_fee, "paid_out": paid_out, "receivable": max(receivable, 0),
            "fee_percent": fee_percent}
