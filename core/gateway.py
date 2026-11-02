# -*- coding: utf-8 -*-
"""درگاه پرداخت شبیه‌سازی‌شده (sandbox).

برای اتصال به درگاه واقعی فقط همین فایل عوض می‌شود:
  request_payment() -> هدایت کاربر به بانک
  verify()          -> بررسی تأیید پس از بازگشت
وضعیت مالی در جدول transactions نگهداری می‌شود و نوبت تنها پس از
verify موفق «confirmed» می‌گردد (پرداخت ناموفق = هیچ نوبت قطعی).
"""
import secrets

from core.db import query_d, execute, now_str


def new_token():
    return secrets.token_hex(8)


def sandbox_page(token):
    """صفحهٔ بانک آزمایشی: کاربر نتیجه را انتخاب می‌کند."""
    return query_d("SELECT t.*, a.code AS appt_code, a.amount, a.payable_now, b.name AS biz "
                   "FROM transactions t JOIN appointments a ON a.id=t.appointment_id "
                   "JOIN businesses b ON b.id=t.business_id "
                   "WHERE t.auth_code=? AND t.kind='payment' AND t.status IN ('created','pending')",
                   (token,), one=True)


def complete(token, outcome, auth_code="", trace="", message=""):
    """outcome: success | failed | cancelled"""
    tx = query_d("SELECT * FROM transactions WHERE auth_code=? AND kind='payment'", (token,), one=True)
    if not tx:
        return None
    status = {"success": "success", "failed": "failed", "cancelled": "cancelled"}[outcome]
    execute("UPDATE transactions SET status=?, auth_code=?, trace=?, message=?, done_at=? WHERE id=?",
            (status, auth_code or token, trace, message, now_str(), tx["id"]))
    return query_d("SELECT * FROM transactions WHERE id=?", (tx["id"],), one=True)
