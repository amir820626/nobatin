# -*- coding: utf-8 -*-
"""ورود با شماره موبایل و کد یک‌بارمصرف."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session

from core import db as D
from core.helpers import current_user, login_user, logout_user, safe_next
from core.jalali import is_valid_mobile, normalize_mobile

bp = Blueprint("auth", __name__)


def landing_for(user):
    return {"admin": url_for("admin.panel"), "business": url_for("business.panel")}.get(
        user["role"], url_for("customer.panel"))


@bp.route("/vorood")
def login():
    if current_user():
        return redirect(landing_for(current_user()))
    mobile = normalize_mobile(request.args.get("mobile", ""))
    nxt = request.args.get("next") or session.get("next_url") or ""
    return render_template("auth/login.html", mobile=mobile, next=nxt)


@bp.route("/vorood", methods=["POST"])
def send_otp():
    mobile = normalize_mobile(request.form.get("mobile"))
    nxt = request.form.get("next") or ""
    if not is_valid_mobile(mobile):
        flash("شمارهٔ موبایل باید ۱۱ رقم و با ۰۹ شروع شود.", "error")
        return redirect(url_for("auth.login", mobile=mobile))
    code, err = D.create_otp(mobile, request.remote_addr)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login", mobile=mobile, **({"next": nxt} if nxt else {})))
    hint = (f"حالت توسعه فعال است — کد ارسال‌شده: {code}"
            if current_app.config.get("OTP_MASTER") else "")
    user = D.query_d("SELECT * FROM users WHERE mobile=?", (mobile,), one=True)
    return render_template("auth/otp.html", mobile=mobile, hint=hint, next=nxt,
                           known=user, resends=D.scalar(
                               "SELECT COUNT(*) FROM otp_codes WHERE mobile=?", (mobile,), 0))


@bp.route("/tayid", methods=["POST"])
def verify_otp():
    mobile = normalize_mobile(request.form.get("mobile"))
    ok, msg = D.verify_otp(mobile, request.form.get("code"))
    if not ok:
        flash(msg, "error")
        return redirect(url_for("auth.login", mobile=mobile))
    user = D.get_or_create_user(mobile)
    if not user["is_active"]:
        flash("حساب شما غیرفعال است. با پشتیبانی تماس بگیرید.", "error")
        return redirect(url_for("auth.login", mobile=mobile))
    login_user(user)
    flash(f"خوش آمدید{'، ' + user['name'] if user['name'] else ''}.", "ok")
    nxt = request.form.get("next") or session.pop("next_url", "") or ""
    # فقط مسیر نسبی: `//evil`, `/\evil` (مرورگر آن را //evil می‌خواند) و هر
    # scheme/host دیگر، کاربرِ تازه‌وارد را از صفحهٔ ورود به بیرون می‌برد.
    return redirect(safe_next(nxt, landing_for(user)))


@bp.route("/khorooj")
def logout():
    logout_user()
    flash("از حساب خارج شدید.", "ok")
    return redirect(url_for("public.home"))


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    blocked = None
    u = current_user()
    if not u:
        return redirect(url_for("auth.login", next=request.path))
    if request.method == "POST":
        D.execute("UPDATE users SET name=? WHERE id=?", ((request.form.get("name") or "").strip(), u["id"]))
        flash("نام شما ذخیره شد.", "ok")
        return redirect(url_for("auth.profile"))
    return render_template("auth/profile.html", u=u)
