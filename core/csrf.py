# -*- coding: utf-8 -*-
"""محافظت CSRF — بدون کتابخانهٔ خارجی.

توکن در نشست (session) می‌نشیند و با {{ csrf() }} به‌صورت فیلد مخفی در فرم می‌آید.
هر درخواست نوشتن (POST/PUT/PATCH/DELETE) باید همان توکن را در فیلد `csrf` یا سرصفحهٔ
`X-CSRF-Token` بفرستد؛ اگر نباشد یا فرق کند، پاسخ ۴۰۰ است.GET‌ها دست‌نخورده‌اند.
"""
import hmac
import secrets

from flask import abort, request, session
from markupsafe import Markup

KEY = "csrf_token"
FIELD = "csrf"
HEADER = "X-CSRF-Token"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def token():
    """توکن نشست؛ در اولین فراخوان ساخته و در همان نشست می‌ماند."""
    tok = session.get(KEY)
    if not tok:
        tok = secrets.token_urlsafe(24)
        session[KEY] = tok
    return tok


def field():
    """فیلد مخفی آمادهٔ درج در فرم. توکن فقط حروف امن دارد، پس فرار دادن لازم نیست."""
    return Markup('<input type="hidden" name="{}" value="{}" autocomplete="off">'
                  .format(FIELD, token()))


def validate():
    sent = request.form.get(FIELD) or request.headers.get(HEADER) or ""
    real = session.get(KEY) or ""
    if not real or not hmac.compare_digest(str(sent), str(real)):
        abort(400)
