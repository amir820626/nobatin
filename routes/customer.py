# -*- coding: utf-8 -*-
"""پنل مشتری: نوبت‌ها، جزئیات، لغو با قانون بازپرداخت، جابه‌جایی زمان."""
from datetime import datetime, date

from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort,
                       current_app)

from core import db as D
from core import booking as B
from core.helpers import (current_user, require_login, appointment_or_404, business_or_404,
                          decorate_biz, human_hours, safe_next)
from core.jalali import fa, fa_money, fa_date

bp = Blueprint("customer", __name__)


def decorate_appt(a, with_cancel=True):
    a["svc"] = a.get("svc") or "سرویس"
    a["cancel"] = B.cancellation_preview(a) if with_cancel else None
    a["is_past"] = f"{a['date']} {a['start_hm']}" < datetime.now().strftime("%Y-%m-%d %H:%M")
    a["remaining_label"] = human_hours(a["cancel"]["remaining"]) if a.get("cancel") else ""
    return a


@bp.route("/pankack")
def panel():
    blocked = require_login()
    if blocked:
        return blocked
    u = current_user()
    common = ("FROM appointments a JOIN businesses b ON b.id=a.business_id "
              "LEFT JOIN services s ON s.id=a.service_id "
              "LEFT JOIN cities c ON c.id=b.city_id WHERE a.user_id=?")
    upcoming = [decorate_appt(a) for a in D.query_d(
        f"SELECT a.*, b.name AS biz, b.slug, b.hue, b.phone AS biz_phone, b.address, "
        f"s.title AS svc, s.duration_min, c.name AS city {common} "
        "AND a.status IN ('confirmed','pending_payment') ORDER BY a.date, a.start_hm", (u["id"],))]
    history = [decorate_appt(a, False) for a in D.query_d(
        f"SELECT a.*, b.name AS biz, b.slug, b.hue, s.title AS svc, "
        f"(SELECT r.amount FROM refunds r WHERE r.appointment_id=a.id ORDER BY r.id DESC LIMIT 1) AS refund_amt "
        f"{common} AND a.status IN ('cancelled','completed','expired','no_show') "
        "ORDER BY a.date DESC, a.id DESC LIMIT 40", (u["id"],))]
    txs = D.query_d("SELECT t.*, a.code, b.name AS biz FROM transactions t "
                    "LEFT JOIN appointments a ON a.id=t.appointment_id "
                    "LEFT JOIN businesses b ON b.id=t.business_id "
                    "WHERE t.user_id=? ORDER BY t.id DESC LIMIT 25", (u["id"],))
    refunds = D.query_d("SELECT r.*, a.code, b.name AS biz FROM refunds r "
                        "LEFT JOIN appointments a ON a.id=r.appointment_id "
                        "LEFT JOIN businesses b ON b.id=a.business_id "
                        "WHERE a.user_id=? ORDER BY r.id DESC LIMIT 12", (u["id"],))
    active = [a for a in upcoming if not a["is_past"]]
    return render_template("customer/panel.html", upcoming=upcoming, active=active,
                           past_upcoming=[a for a in upcoming if a["is_past"]], history=history,
                           txs=txs, refunds=refunds,
                           stats={"n": len(active),
                                  "waiting": len([a for a in upcoming if a["status"] == "pending_payment"]),
                                  "spent": sum(t["amount"] for t in txs if t["status"] == "success"),
                                  "refunded": sum(r["amount"] for r in refunds if r["status"] == "done")})


@bp.route("/nobat/<int:aid>")
def detail(aid):
    blocked = require_login()
    if blocked:
        return blocked
    a = appointment_or_404(aid)
    u = current_user()
    if a["user_id"] != u["id"] and u["role"] != "admin":
        abort(403)
    decorate_appt(a)
    biz = business_or_404(a["business_id"])
    decorate_biz(biz)
    svc = D.query_d("SELECT * FROM services WHERE id=?", (a["service_id"],), one=True)
    slots = B.slot_times(biz["id"], a["date"], svc["duration_min"] if svc else 30)
    return render_template("customer/appointment.html", a=a, biz=biz, svc=svc,
                           txs=D.query_d("SELECT * FROM transactions WHERE appointment_id=? ORDER BY id",
                                         (aid,)),
                           refunds=D.query_d("SELECT * FROM refunds WHERE appointment_id=?", (aid,)),
                           sms=D.query_d("SELECT * FROM sms_log WHERE mobile=? ORDER BY id DESC LIMIT 8",
                                         (u["mobile"],)),
                           reschedule_slots=[s for s in slots if not s.get("past") and not s.get("taken")],
                           days=D.query_d("SELECT * FROM day_blocks WHERE business_id=? AND date>=?",
                                          (biz["id"], date.today().isoformat())))


@bp.route("/nobat/<int:aid>/laghv", methods=["POST"])
def cancel(aid):
    blocked = require_login()
    if blocked:
        return blocked
    a = appointment_or_404(aid)
    u = current_user()
    if a["user_id"] != u["id"] and u["role"] != "admin":
        abort(403)
    ok, info = B.cancel_appointment(aid, request.form.get("reason") or "لغو توسط مشتری")
    if not ok:
        flash(info, "error")
    elif isinstance(info, dict):
        flash(f"نوبت لغو شد. طبق قانون «{info['label']}» مبلغ {fa_money(info['amount'])} تومان "
              f"({fa(info['percent'])}٪) بازپرداخت شد. کد پیگیری: {info['reference']}", "ok")
    else:
        flash(info, "ok")
    return redirect(safe_next(request.form.get("back"), url_for("customer.detail", aid=aid)))


@bp.route("/nobat/<int:aid>/javabeja", methods=["POST"])
def reschedule(aid):
    blocked = require_login()
    if blocked:
        return blocked
    a = appointment_or_404(aid)
    u = current_user()
    if a["user_id"] != u["id"]:
        abort(403)
    if a["status"] != "confirmed":
        flash("فقط نوبت‌های قطعی قابل جابه‌جایی‌اند.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    iso = request.form.get("date")
    hm = request.form.get("time")
    if not iso or not hm:
        flash("روز و ساعت جدید را انتخاب کنید.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    if f"{iso} {hm}" < datetime.now().strftime("%Y-%m-%d %H:%M"):
        flash("زمان جدید در گذشته است.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    if iso == a["date"] and hm == a["start_hm"]:
        flash("نوبت همین حالا در همین روز و ساعت رزرو شده است.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    biz = business_or_404(a["business_id"])
    svc = D.query_d("SELECT * FROM services WHERE id=? AND business_id=? AND is_active=1",
                    (a["service_id"], biz["id"]), one=True)
    if not svc:
        flash("سرویس این نوبت دیگر فعال نیست؛ با کسب‌وکار تماس بگیرید.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    # همان بررسیِ صفحهٔ رزرو: روز/ساعت باید واقعاً در تقویم آن مجموعه باشد.
    # بی این، POST دستی می‌تواند نوبت قطعی را به جمعهٔ تعطیل یا «99:99» برد.
    if B.is_blocked(biz["id"], iso):
        flash("آن روز برای این مجموعه تعطیل است.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    slots = {s["hm"]: s for s in B.slot_times(biz["id"], iso, svc["duration_min"] or 30)}
    sel = slots.get(hm)
    if not sel:
        flash("آن روز و ساعت در تقویم این مجموعه وجود ندارد؛ دیگری انتخاب کنید.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    if sel.get("taken"):
        flash("این زمان رزرو شده است؛ زمان دیگری انتخاب کنید.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    new_end = B.min_to_hm(B.hm_to_min(hm) + (svc["duration_min"] or 30))
    try:
        # چهار نوشتن (قفل تازه، آزادکردن قفل کهنه، نوبت، ارتقای قفل به booked) یک
        # تراکنش‌اند: halfway = نوبتی که ساعتش را دو نفر دارند، یا نوبتی که به
        # ساعتِ قفل‌شدهٔ دیگری اشاره می‌کند.
        with D.tx():
            ok, msg = B.hold_slot(u["id"], biz["id"], svc["id"], iso, hm,
                                  biz["hold_minutes"] or 10)
            if not ok:
                raise D.Abort(msg)
            if not D.changed("UPDATE appointments SET date=?, start_hm=?, end_hm=? "
                             "WHERE id=? AND status='confirmed'", (iso, hm, new_end, aid)):
                raise D.Abort("نوبت در همین لحظه تغییر وضعیت داد؛ جابه‌جا نشد.")
            # قفل این نوبت بعد از قطعی‌شدن «booked» است، پس include_booked لازم است
            B.release_slot(biz["id"], a["date"], a["start_hm"], u["id"], include_booked=True)
            D.execute("UPDATE slot_locks SET status='booked', expires_at=NULL "
                      "WHERE business_id=? AND date=? AND start_hm=? AND holder_user_id=?",
                      (biz["id"], iso, hm, u["id"]))
    except D.Abort as bad:
        flash(str(bad.message or "جابه‌جایی انجام نشد؛ زمان دیگری انتخاب کنید."), "error")
        return redirect(url_for("customer.detail", aid=aid))
    except Exception as exc:                # ایندکس یکتایی/شلوغی قفل: نوبت دست‌نخورده می‌ماند
        current_app.logger.exception("reschedule failed (appt %s): %s", aid, exc)
        flash("جابه‌جایی انجام نشد؛ لطفاً چند لحظه دیگر دوباره تلاش کنید.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    D.send_sms(u["mobile"], "rescheduled", _appt_id=aid, biz=biz["name"], date=fa_date(iso),
               time=fa(hm), code=a["code"])
    D.send_sms(D.business_owner_mobile(biz["id"]), "rescheduled", _appt_id=aid, biz=biz["name"],
               date=fa_date(iso), time=fa(hm), code=a["code"])
    flash(f"نوبت به {fa_date(iso)} ساعت {fa(hm)} جابه‌جا شد و پیامک جدید ارسال گردید.", "ok")
    return redirect(url_for("customer.detail", aid=aid))
