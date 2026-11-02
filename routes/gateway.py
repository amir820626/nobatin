# -*- coding: utf-8 -*-
"""درگاه پرداخت آزمایشی. جایگزینی با درگاه واقعی فقط در همین فایل + core/gateway.py است."""
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort,
                   current_app)

from core import db as D
from core import booking as B
from core import gateway as G
from core.helpers import current_user, safe_next

bp = Blueprint("gateway", __name__)

LABELS = {"success": "موفق", "failed": "ناموفق", "cancelled": "انصراف"}


@bp.route("/gateway/<token>", methods=["GET", "POST"])
def pay(token):
    # این صفحه «دکمهٔ پرداخت موفق» است؛ اگر درگاه واقعی وصل شده باشد نباید باقی بماند،
    # وگرنه هر کاربر واردشده با یک POST رایگان نوبت قطعی می‌سازد (و درگاه واقعی هم
    # نادیده گرفته می‌شود). پس جز در حالت sandbox این مسیر ۴۰۴ است.
    if current_app.config.get("PAYMENT_PROVIDER", "sandbox") != "sandbox":
        abort(404)
    tx = G.sandbox_page(token)
    if not tx:
        abort(404)
    aid = tx["appointment_id"]
    a = D.query_d("SELECT * FROM appointments WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    u = current_user()
    if not u or (a["user_id"] != u["id"] and u["role"] != "admin"):
        abort(403)
    nxt = safe_next(request.values.get("next"), url_for("customer.detail", aid=aid))
    lock = D.query_d("SELECT * FROM slot_locks WHERE business_id=? AND date=? AND start_hm=? "
                     "AND status='hold'", (a["business_id"], a["date"], a["start_hm"]), one=True)
    left = 0
    if lock and lock["expires_at"]:
        exp = datetime.strptime(lock["expires_at"], "%Y-%m-%d %H:%M:%S")
        left = max(int((exp - datetime.now()).total_seconds() // 60), 0)
    if request.method == "POST":
        outcome = request.form.get("outcome", "success")
        if a["status"] == "confirmed":
            flash("این نوبت پیش‌تر قطعی شده است.", "ok")
            return redirect(nxt)
        if outcome == "success":
            done = G.complete(token, "success", auth_code="A" + token[:6], trace="T" + token[:6])
            if not done:
                flash("این تراکنش دیگر باز نیست؛ از نوبت خود دیدن کنید.", "error")
                return redirect(url_for("customer.detail", aid=aid))
            # مبلغِ تأییدشده باید همان مبلغِ نوبت باشد. در درگاه واقعی هم همین شرط
            # را روی پاسخ استعلام bank می‌گذاریم؛ بی آن، «پرداختِ هر چیزِ دیگر»
            # نوبت را قطعی می‌کند.
            if int(done["amount"] or 0) != int(a["payable_now"] or 0):
                G.complete(token, "failed", message="مبلغ تراکنش با مبلغ نوبت هم‌خوان نیست.")
                B.mark_payment_failed(aid, "مبلغ تراکنش با مبلغ نوبت هم‌خوان نبود.")
                flash("مبلغ تراکنش با مبلغ نوبت هم‌خوانی ندارد؛ نوبتی قطعی نشد.", "error")
                return redirect(url_for("customer.detail", aid=aid))
            ok, msg = B.finalize_payment(aid, auth_code=done["auth_code"], trace=done["trace"])
            flash(msg, "ok" if ok else "error")
            return redirect(nxt if ok else url_for("customer.detail", aid=aid))
        if outcome == "failed":
            G.complete(token, "failed", message="موجودی کارت ناکافی بود (شبیه‌سازی).")
            B.mark_payment_failed(aid, "پرداخت ناموفق از درگاه")
            flash("پرداخت ناموفق بود — نوبتی ثبت نشد و زمان آزاد شد.", "error")
        else:
            G.complete(token, "cancelled", message="کاربر درگاه را ترک کرد.")
            B.mark_payment_failed(aid, "انصراف در درگاه")
            flash("پرداخت لغو شد — نوبتی ایجاد نشده است.", "error")
        return redirect(url_for("customer.detail", aid=aid))
    biz = D.query_d("SELECT * FROM businesses WHERE id=?", (a["business_id"],), one=True)
    svc = D.query_d("SELECT * FROM services WHERE id=?", (a["service_id"],), one=True)
    return render_template("gateway.html", tx=tx, a=a, biz=biz, svc=svc, nxt=nxt, left=left,
                           prior=D.query_d("SELECT * FROM transactions WHERE appointment_id=? "
                                           "AND kind='payment' ORDER BY id", (aid,)))
