# -*- coding: utf-8 -*-
"""جریان رزرو: انتخاب زمان، قفل ضد هم‌زمانی، ساخت نوبت در انتظار پرداخت."""
from datetime import date, datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   session, abort)

from core import db as D
from core import booking as B
from core.jalali import parse_policy, to_jdate
from core.helpers import (current_user, business_or_404, appointment_or_404, decorate_biz,
                          require_login, limit_hit, cfg_limit)

bp = Blueprint("booking", __name__)


@bp.route("/k/<slug>/rezerv")
def pick(slug):
    biz = business_or_404(slug)
    u = current_user()
    # صفحهٔ رزرو هم مثل صفحهٔ عمومی فقط برای کسب‌وکارِ فعال باز است؛ وگرنه ساعت‌های
    # کاری، قیمت‌ها و پیش‌پرداختِ مجموعه‌ای که مدیر آن را غیرفعال کرده عمومی می‌ماند.
    if biz["status"] != "active" and not (u and (u["role"] == "admin" or u["id"] == biz["owner_id"])):
        abort(404)
    decorate_biz(biz)
    iso = request.args.get("date") or date.today().isoformat()
    try:
        day = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        day = datetime.combine(date.today(), datetime.min.time())   # تاریخ بی‌معنی → امروز
    if request.args.get("shift"):          # ± روز؛ مقدار بی‌معنی تقویم را نمی‌ترکاند
        try:
            day = day + timedelta(days=int(request.args["shift"]))
        except (TypeError, ValueError):
            pass
    iso = day.date().isoformat()
    services = D.query_d("SELECT * FROM services WHERE business_id=? AND is_active=1 ORDER BY sort",
                         (biz["id"],))
    for s in services:
        s["pay_now"] = (min(biz["deposit_amount"] or 0, s["price"])
                        if biz["payment_mode"] == "deposit" else
                        (0 if biz["payment_mode"] == "free" else s["price"]))
    sid = request.args.get("service", type=int)
    svc = next((s for s in services if s["id"] == sid), services[0] if services else None)
    jiso = to_jdate(iso)
    avail = B.month_availability(biz["id"], jiso.year, jiso.month,
                                 svc["duration_min"] if svc else 30)
    slots = B.slot_times(biz["id"], iso, svc["duration_min"] if svc else 30) if svc else []
    free = [s for s in slots if not s.get("past") and not s.get("taken")]
    return render_template("booking.html", biz=biz, services=services, svc=svc, iso=iso,
                           slots=slots, free=free, avail=avail,
                           blocked=B.is_blocked(biz["id"], iso),
                           no_hours=svc is not None and not slots,
                           policy=parse_policy(biz["cancel_policy"]),
                           hours=D.query_d("SELECT * FROM working_hours WHERE business_id=? "
                                           "AND is_active=1 ORDER BY weekday", (biz["id"],)),
                           cart=cart_state())


@bp.route("/k/<slug>/zan", methods=["POST"])
def take(slug):
    """انتخاب زمان: ورود لازم است؛ پس از آن نوبت «در انتظار پرداخت» ساخته می‌شود."""
    biz = business_or_404(slug)
    iso = request.form.get("date") or date.today().isoformat()
    sid = request.form.get("service", type=int)
    hm = (request.form.get("time") or "").strip()
    if not hm:
        flash("یک ساعت را انتخاب کنید.", "error")
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=sid))
    u = current_user()
    # سقف تعداد قفل ساعت برای هر کاربر (سبد): جلوی رباتی که همهٔ ساعت‌ها را
    # «نگه می‌دارد» را می‌گیرد تا بقیه بتوانند رزرو کنند.
    if u and limit_hit("hold:%s" % u["id"], cfg_limit("LIMIT_HOLDS_USER"), 3600,
                       "در یک ساعت %s بار می‌توانید ساعت قفل کنید؛ کمی بعد تلاش کنید."):
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=sid))
    if not current_user():
        session["pending_pick"] = {"slug": biz["slug"], "date": iso, "service": sid, "time": hm}
        return redirect(url_for("auth.login", next=url_for("booking.confirm", slug=biz["slug"])))
    return confirm_run(biz, iso, sid, hm)


@bp.route("/k/<slug>/takmil")
def confirm(slug):
    """مسیر بازگشت پس از ورود موفق — فقط سبدِ خودِ نشست را کامل می‌کند.

    date/service/time از کوئری خوانده نمی‌شود: این مسیر GET است (بازگشت از login)
    و کوئریِ آن را مهاجم می‌تواند در لینک بگذارد. بی این قاعده، یک لینک کلیک‌شده
    به «ساعتی که مهاجم انتخاب کرده» برای خودِ کاربر نوبت می‌ساخت (CSRF روی GET).
    """
    biz = business_or_404(slug)
    blocked = require_login()
    if blocked:
        return blocked
    pick_data = session.pop("pending_pick", None) or {}
    iso = pick_data.get("date") or date.today().isoformat()
    sid = pick_data.get("service")
    hm = (pick_data.get("time") or "").strip()
    if not hm:
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=sid))
    return confirm_run(biz, iso, sid, hm)


def confirm_run(biz, iso, sid, hm):
    u = current_user()
    # سقف ساخت نوبت برای هر کاربر در ساعت: ربات و «تستِ اشغال همهٔ ساعت‌ها» را
    # می‌گیرد، ولی کاربر عادی (حداکثر ۳۰ نوبت در ساعت) را هرگز نمی‌بیند.
    if limit_hit("book:%s" % u["id"], cfg_limit("LIMIT_BOOKS_USER"), 3600,
                 "برای هر حساب در یک ساعت فقط %s نوبت می‌توان ساخت؛ کمی بعد ادامه دهید."):
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=sid))
    svc = D.query_d("SELECT * FROM services WHERE id=? AND business_id=? AND is_active=1",
                    (sid, biz["id"]), one=True)
    if not svc:
        flash("سرویس نامعتبر است.", "error")
        return redirect(url_for("public.business_page", slug=biz["slug"]))
    if biz["status"] != "active":
        flash("این کسب‌وکار فعلاً نوبت آنلاین نمی‌گیرد.", "error")
        return redirect(url_for("public.business_page", slug=biz["slug"]))
    slots = {s["hm"]: s for s in B.slot_times(biz["id"], iso, svc["duration_min"])}
    sel = slots.get(hm)
    if not sel:
        flash("این ساعت در تقویم آن روز وجود ندارد.", "error")
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=svc["id"]))
    if sel.get("past"):
        flash("آن ساعت گذشته است؛ زمان دیگری انتخاب کنید.", "error")
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=svc["id"]))
    if sel.get("taken"):
        flash("این زمان دقیقاً هم‌زمان رزرو شد. زمان دیگری انتخاب کنید.", "error")
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=svc["id"]))
    aid, err = B.create_pending_appointment(u, biz, svc, iso, hm)
    if not aid:
        flash(err or "رزرو انجام نشد؛ دوباره تلاش کنید.", "error")
        return redirect(url_for("booking.pick", slug=biz["slug"], date=iso, service=svc["id"]))
    a = appointment_or_404(aid)
    if a["status"] == "confirmed":
        flash("نوبت شما بدون نیاز به پرداخت آنلاین قطعی شد.", "ok")
        return redirect(url_for("customer.detail", aid=aid))
    token = B.new_token_and_tx(aid, a["payable_now"])
    return redirect(url_for("gateway.pay", token=token, next=url_for("customer.detail", aid=aid)))


@bp.route("/nobat/<int:aid>/pardakht-dobare")
def retry_payment(aid):
    blocked = require_login()
    if blocked:
        return blocked
    a = appointment_or_404(aid)
    if a["user_id"] != current_user()["id"]:
        abort(403)
    if a["status"] != "pending_payment":
        return redirect(url_for("customer.detail", aid=aid))
    token = B.new_token_and_tx(aid, a["payable_now"])
    return redirect(url_for("gateway.pay", token=token, next=url_for("customer.detail", aid=aid)))


def cart_state():
    u = current_user()
    if not u:
        return []
    return D.query_d("SELECT * FROM slot_locks WHERE holder_user_id=? AND status='hold' "
                     "AND expires_at > ?", (u["id"], D.now_str()))
