# -*- coding: utf-8 -*-
"""کش کوچک درون‌روشی برای داده‌های تقریباً ثابت.

فقط چیزهایی کش می‌شوند که نوشتن روی‌شان نادر است: فهرست دسته‌ها، استان/شهر/محله،
تنظیمات نمایشی. هدف این است که هر درخواست (که در نوبتین ده‌ها کوئری می‌زند) همان
کوئری‌های تکراری را به پایگاه داده نفرستد.

چند نکته که عمداً رعایت شده:
* عمر کش کوتاه است (پیش‌فرض ۱۵ ثانیه) تا اگر مدیر چیزی را عوض کرد، زیاد کهنه نماند.
* هر نوشتن روی یک جدول، کلیدهای برچسب‌خورده با همان جدول را بی‌اعتبار می‌کند
  (`touch`)، پس در یک پروسه بلافاصله دادهٔ تازه دیده می‌شود.
* اگر چند worker هم‌زمان اجرا شود (gunicorn)، بی‌اعتبارسازی فقط در پروسهٔ نویسنده
  قطعی است؛ پروسه‌های دیگر حداکثر تا هم همان عمر کش دادهٔ کهنه می‌بینند. برای همین
  عدد پایین انتخاب شده و با NOBATIN_CACHE_TTL=0 می‌توان کش را کلاً خاموش کرد.
"""
import os
import re
import threading
import time

_TTL = int((os.environ.get("NOBATIN_CACHE_TTL") or "15").strip() or 0)
_LOCK = threading.Lock()
_STORE = {}                 # key -> (expires_at, value)
_TAGS = {}                  # key -> set(table)
_HITS = [0, 0]              # [hit, miss]


def ttl():
    return _TTL


def remember(key, loader, tables=(), seconds=None):
    """مقدار کلید را برمی‌گرداند؛ اگر در کش نبود، از loader خوانده و می‌نویسد."""
    if _TTL <= 0 or seconds == 0:
        return loader()
    now = time.monotonic()
    with _LOCK:
        item = _STORE.get(key)
        if item and item[0] > now:
            _HITS[0] += 1
            return item[1]
        _HITS[1] += 1
    value = loader()
    life = _TTL if seconds is None else seconds
    with _LOCK:
        _STORE[key] = (time.monotonic() + life, value)
        if tables:
            _TAGS[key] = set(tables) | _TAGS.get(key, set())
    return value


def touch(table):
    """بعد از نوشتن روی یک جدول: کلیدهای مرتبط را دور می‌ریزد."""
    if _TTL <= 0 or not table:
        return
    t = table.lower().strip()
    with _LOCK:
        dead = [k for k, tags in _TAGS.items() if t in tags]
        for k in dead:
            _STORE.pop(k, None)
            _TAGS.pop(k, None)


def clear():
    with _LOCK:
        _STORE.clear()
        _TAGS.clear()


def stats():
    now = time.monotonic()
    with _LOCK:
        live = sum(1 for exp, _ in _STORE.values() if exp > now)
        h, m = _HITS
    return {"keys": live, "hits": h, "misses": m, "ttl": _TTL,
            "hit_rate": round(h / (h + m) * 100, 1) if (h + m) else 0.0}


_TABLE_RE = re.compile(
    r"\b(?:insert(?:\s+or\s+[a-z]+)?\s+into|update(?:\s+or\s+[a-z]+)?|delete\s+from|"
    r"replace\s+into)\s+[\"`']?([a-z_][a-z0-9_]*)",
    re.IGNORECASE)


def table_of(sql):
    """نام جدولِ هدفِ یک نوشتن؛ برای بی‌اعتبارسازی خودکار کش."""
    m = _TABLE_RE.search(sql or "")
    return m.group(1) if m else None
