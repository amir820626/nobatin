# -*- coding: utf-8 -*-
"""لایهٔ دادهٔ نوبتین — SQLite با جدول‌های مستقل برای نوبت، تراکنش و بازپرداخت.

این فایل تنها جایی است که با پایگاه داده حرف می‌زند؛ برای همین نکات «چند کاربر
هم‌زمان» اینجا حل می‌شوند:
* حالت `WAL` تا خواننده‌ها نویسنده را نبندند و بالعکس؛
* `busy_timeout` و تلاش دوباره وقتی قفل نوشتن موقتاً شلوغ است؛
* `tx()` — یک یا چند نوشتن را اتمیک می‌کند (یا همه، یا هیچ) تا نوبت نیمه‌کاره،
  قفل نیمه‌کاره یا تسویهٔ بدون رکورد دفتر کل باقی نماند؛
* `isolation_level=None` (autocommit) تا خودمان مالک شروع و پایان تراکنش باشیم.
"""
import json
import os
import random
import re
import sqlite3
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, date, timedelta

from flask import current_app, g, flash, session

from core import cache
from core.jalali import (evaluate_cancellation, fa, is_valid_mobile, norm_place,
                        normalize_mobile)

DB_NAME = os.environ.get("NOBATIN_DB", "nobatin.db")
_PRAGMA_FAILED = set()      # هر شکست pragma فقط یک بار در پروسه لاگ می‌شود
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# مسیر instance را می‌توان بیرون از کد برد (دیск سریع‌تر، بکاپ‌گیری ساده‌تر)
INSTANCE_DIR = os.environ.get("NOBATIN_INSTANCE_DIR") or os.path.join(APP_ROOT, "instance")


def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


# تنظیمات قابل‌تنظیم SQLite (برای سرور؛ در حالت پیش‌فرض local بی‌تغییر می‌ماند)
SQLITE_BUSY_MS = _env_int("NOBATIN_SQLITE_BUSY_MS", 5000)
SQLITE_SYNCHRONOUS = (os.environ.get("NOBATIN_SQLITE_SYNCHRONOUS") or "normal").strip().lower()
SQLITE_CACHE_KB = _env_int("NOBATIN_SQLITE_CACHE_KB", 32000)      # ۳۲ مگابایت صفحه‌حافظه
SQLITE_MMAP_MB = _env_int("NOBATIN_SQLITE_MMAP_MB", 128)
SQLITE_WAL = (os.environ.get("NOBATIN_SQLITE_JOURNAL") or "wal").strip().lower() != "delete"
if SQLITE_SYNCHRONOUS not in ("normal", "full", "off"):
    SQLITE_SYNCHRONOUS = "normal"
TX_RETRIES = _env_int("NOBATIN_TX_RETRIES", 4)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS provinces (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  norm TEXT DEFAULT ''                -- کلید یک‌شکل برای جلوگیری از ثبت تکراری
);
CREATE TABLE IF NOT EXISTS cities (
  id INTEGER PRIMARY KEY,
  province_id INTEGER NOT NULL REFERENCES provinces(id),
  name TEXT NOT NULL,
  norm TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS districts (
  id INTEGER PRIMARY KEY,
  city_id INTEGER NOT NULL REFERENCES cities(id),
  name TEXT NOT NULL,                 -- همان‌طور که مالک نوشته (نمایش)
  norm TEXT DEFAULT ''                -- کلید یک‌شکل؛ از ساختن «سعادت آباد» تکراری جلوگیری می‌کند
);

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  icon TEXT DEFAULT 'sparkle',
  blurb TEXT DEFAULT '',
  sort INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  mobile TEXT NOT NULL UNIQUE,
  name TEXT DEFAULT '',
  role TEXT NOT NULL DEFAULT 'customer',      -- customer | business | admin
  is_active INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS otp_codes (
  id INTEGER PRIMARY KEY,
  mobile TEXT NOT NULL,
  code TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  ip TEXT,
  created_at TEXT,
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS businesses (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER REFERENCES users(id),
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  category_id INTEGER REFERENCES categories(id),
  province_id INTEGER REFERENCES provinces(id),
  city_id INTEGER REFERENCES cities(id),
  district_id INTEGER REFERENCES districts(id),
  address TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  about TEXT DEFAULT '',
  hue INTEGER DEFAULT 172,                    -- رنگ اختصاصی برند کسب‌وکار
  rating REAL DEFAULT 0,
  rating_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',               -- pending | active | suspended
  payment_mode TEXT DEFAULT 'full',           -- full | deposit | free
  deposit_amount INTEGER DEFAULT 0,
  cancel_policy TEXT DEFAULT '[]',            -- json: [{min_hours, refund_percent}]
  hold_minutes INTEGER DEFAULT 10,
  commission_percent REAL DEFAULT 0,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  title TEXT NOT NULL,
  price INTEGER DEFAULT 0,
  duration_min INTEGER DEFAULT 30,
  buffer_min INTEGER DEFAULT 0,
  description TEXT DEFAULT '',
  requires_deposit INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  sort INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS working_hours (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  weekday INTEGER NOT NULL,                   -- 0 = شنبه ... 6 = جمعه
  start_hm TEXT NOT NULL,
  end_hm TEXT NOT NULL,
  slot_min INTEGER DEFAULT 30,
  is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS day_blocks (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  date TEXT NOT NULL,
  reason TEXT DEFAULT '',
  UNIQUE(business_id, date)
);

CREATE TABLE IF NOT EXISTS slot_locks (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  service_id INTEGER REFERENCES services(id),
  date TEXT NOT NULL,
  start_hm TEXT NOT NULL,
  holder_user_id INTEGER REFERENCES users(id),
  status TEXT DEFAULT 'hold',                 -- hold | booked | released
  expires_at TEXT,
  created_at TEXT,
  UNIQUE(business_id, date, start_hm)
);

CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  service_id INTEGER NOT NULL REFERENCES services(id),
  date TEXT NOT NULL,
  start_hm TEXT NOT NULL,
  end_hm TEXT,
  amount INTEGER DEFAULT 0,                   -- مبلغ کل سرویس
  paid_amount INTEGER DEFAULT 0,              -- پرداخت‌شده
  payable_now INTEGER DEFAULT 0,              -- پرداخت آنلاین این نوبت
  status TEXT DEFAULT 'pending_payment',      -- pending_payment|confirmed|cancelled|no_show|completed|expired
  pay_status TEXT DEFAULT 'unpaid',           -- unpaid|paid|partially_paid|refunded|refund_pending
  cancel_policy TEXT DEFAULT '[]',            -- snapshot قوانین در لحظهٔ رزرو
  notes TEXT DEFAULT '',
  created_at TEXT,
  confirmed_at TEXT,
  cancelled_at TEXT,
  cancel_reason TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  reference TEXT UNIQUE,
  appointment_id INTEGER REFERENCES appointments(id),
  user_id INTEGER REFERENCES users(id),
  business_id INTEGER REFERENCES businesses(id),
  kind TEXT DEFAULT 'payment',                -- payment | refund
  amount INTEGER DEFAULT 0,
  status TEXT DEFAULT 'created',              -- created|success|failed|cancelled
  gateway TEXT DEFAULT 'sandbox',
  auth_code TEXT DEFAULT '',
  trace TEXT DEFAULT '',
  message TEXT DEFAULT '',
  created_at TEXT,
  done_at TEXT
);

CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY,
  transaction_id INTEGER REFERENCES transactions(id),
  appointment_id INTEGER REFERENCES appointments(id),
  amount INTEGER DEFAULT 0,
  percent INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',               -- pending|done|rejected
  rule_label TEXT DEFAULT '',
  created_at TEXT,
  done_at TEXT,
  reference TEXT
);

CREATE TABLE IF NOT EXISTS sms_log (
  id INTEGER PRIMARY KEY,
  mobile TEXT,
  template TEXT,                               -- otp|booking_ok|reminder|cancelled|refund|...
  body TEXT,
  status TEXT DEFAULT 'queued',                -- sent|failed|skipped
  appointment_id INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  user_id INTEGER,
  role TEXT,                                   -- مدیر/مالک/مشتری/سیستم
  action TEXT,                                 -- مثلاً biz_suspend، refund_done
  target TEXT,                                 -- روی چه چیزی اعمال شد
  detail TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
  id INTEGER PRIMARY KEY,
  business_id INTEGER REFERENCES businesses(id),
  amount INTEGER DEFAULT 0,
  period_from TEXT,
  period_to TEXT,
  status TEXT DEFAULT 'pending',               -- pending|paid
  created_at TEXT,
  paid_at TEXT
);

CREATE TABLE IF NOT EXISTS rate_events (
  id INTEGER PRIMARY KEY,
  bucket TEXT NOT NULL,                        -- مثلاً hold:user:12 یا otp:ip:1.2.3.4
  window_start TEXT NOT NULL,                  -- زمان شروع پنجره (متن sortable)
  cnt INTEGER NOT NULL DEFAULT 1,
  UNIQUE(bucket, window_start)
);

CREATE INDEX IF NOT EXISTS idx_appt_user ON appointments(user_id, status);
CREATE INDEX IF NOT EXISTS idx_appt_business ON appointments(business_id, date, status);
CREATE INDEX IF NOT EXISTS idx_biz_cat ON businesses(category_id, city_id);
-- ایندکس‌های مسیرهای داغ: صفحهٔ اصلی/جست‌وجو، تقویم، مالی، پنل‌ها و جاروی خودکار.
CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(date, status);
CREATE INDEX IF NOT EXISTS idx_appt_svc ON appointments(service_id, status);
CREATE INDEX IF NOT EXISTS idx_tx_appt ON transactions(appointment_id, kind);
CREATE INDEX IF NOT EXISTS idx_tx_biz ON transactions(business_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_ref_appt ON refunds(appointment_id, status);
CREATE INDEX IF NOT EXISTS idx_ref_tx ON refunds(transaction_id);
CREATE INDEX IF NOT EXISTS idx_sms_mobile ON sms_log(mobile, created_at);
CREATE INDEX IF NOT EXISTS idx_sms_appt ON sms_log(appointment_id, template);
CREATE INDEX IF NOT EXISTS idx_otp_mobile ON otp_codes(mobile, created_at);
CREATE INDEX IF NOT EXISTS idx_otp_ip ON otp_codes(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_lock_exp ON slot_locks(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_lock_biz ON slot_locks(business_id, date, status);
CREATE INDEX IF NOT EXISTS idx_settle_biz ON settlements(business_id, status);
CREATE INDEX IF NOT EXISTS idx_services_biz ON services(business_id, is_active);
CREATE INDEX IF NOT EXISTS idx_hours_biz ON working_hours(business_id, weekday);
CREATE INDEX IF NOT EXISTS idx_blocks_biz_date ON day_blocks(business_id, date);
CREATE INDEX IF NOT EXISTS idx_biz_owner ON businesses(owner_id);
CREATE INDEX IF NOT EXISTS idx_biz_status ON businesses(status, category_id, city_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at);
CREATE INDEX IF NOT EXISTS idx_rate_bucket ON rate_events(bucket, window_start);
-- گارد نهایی ضد رزرو هم‌زمان: فقط نوبت‌های زنده یک ساعت را قفل می‌کنند،
-- تا نوبت لغو/منقضی‌شده همان ساعت را برای همیشه نبندد.
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_slot_active
  ON appointments(business_id, date, start_hm)
  WHERE status IN ('pending_payment', 'confirmed');
"""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db


def _connect(path=None):
    """یک اتصال تازه با pragmaهای مناسبِ سرور.

    `isolation_level=None` یعنی autocommit: پایتون خودش پشت‌صحنه تراکنش باز نمی‌کند
    و ما با `tx()` دقیقاً می‌دانیم کجا یک نوشتن اتمیک شروع و تمام می‌شود.
    """
    p = path or db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=max(1.0, SQLITE_BUSY_MS / 1000.0),
                           detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None,
                           check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout روی هر اتصال لازم است؛ وگرنه اولین قفلِ نوشتن خطا می‌دهد
    conn.execute("PRAGMA busy_timeout = %d" % SQLITE_BUSY_MS)
    # هر pragma جدا سنجیده می‌شود: اگر اولین‌شان (WAL) روی NFS/برخی هاست‌ها بکوبد،
    # بقیه (synchronous/cache_size/mmap) باید اعمال شوند — و شکست باید در لاگ باشد،
    # وگرنه سرور با حالت DELETE-journal بالا می‌آید و هیچ‌کس نمی‌داند.
    pragmas = []
    if SQLITE_WAL:
        pragmas.append(("journal_mode = WAL", "WAL"))
    pragmas += [("synchronous = %s" % SQLITE_SYNCHRONOUS.upper(), "synchronous"),
                ("temp_store = MEMORY", "temp_store"),
                ("cache_size = -%d" % max(2000, SQLITE_CACHE_KB), "cache_size"),
                ("mmap_size = %d" % (max(0, SQLITE_MMAP_MB) * 1024 * 1024), "mmap_size")]
    for stmt, what in pragmas:
        try:
            conn.execute("PRAGMA " + stmt)
        except sqlite3.OperationalError as exc:
            if what not in _PRAGMA_FAILED:
                _PRAGMA_FAILED.add(what)
                _log_error("PRAGMA %s رد شد: %s", what, exc)
    return conn


def db_path():
    if os.path.isabs(DB_NAME):
        return DB_NAME
    return os.path.join(INSTANCE_DIR, DB_NAME)


def tx_depth():
    return getattr(g, "_tx_depth", 0)


def _is_busy(exc):
    m = str(exc).lower()
    return "locked" in m or "busy" in m


@contextmanager
def tx(immediate=True, retries=None):
    """بدنه را در یک تراکنش اجرا می‌کند؛ هنگام شلوغی قفل نوشتن تلاش دوباره می‌کند.

    `execute()` داخل بدنه commit نمی‌کند (عمق تراکنش را می‌بیند)، پس چند نوشتن با هم
    commit یا با هم rollback می‌شوند. بعد از commit موفق، کارهای عقب‌افتاده (مثل
    ارسال واقعی پیامک) اجرا می‌شوند تا قفلِ نوشتن وسط یک درخواست HTTP باز نماند.
    """
    db = get_db()
    if tx_depth():
        # تودرتو: تراکنش بیرونی همین حالا قفل نوشتن را دارد. اینجا فقط عمق را
        # زیاد می‌کنیم تا commit/rollback زودهنگام انجام نشود.
        g._tx_depth += 1
        try:
            yield db
        finally:
            g._tx_depth -= 1
        return
    attempts = (TX_RETRIES if retries is None else retries) + 1
    for i in range(attempts):
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or i == attempts - 1:
                raise
            time.sleep(min(0.25, 0.02 * (2 ** i)) + random.random() / 200.0)
            continue
        g._tx_depth = getattr(g, "_tx_depth", 0) + 1
        try:
            yield db
            db.commit()
        except Exception:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            g._tx_depth = max(0, getattr(g, "_tx_depth", 1) - 1)
            g._tx_after = []                      # nothing was committed: drop the queue
            raise
        g._tx_depth = max(0, getattr(g, "_tx_depth", 1) - 1)
        _run_after_commit()
        return


class Abort(Exception):
    """خطای «منطقی» درون تراکنش: هیچ نصفه‌ای نوشته نمی‌شود و پیامش به کاربر می‌رود.

    با `raise Abort("…")` داخل `with tx():` بدنه rollback می‌شود؛ فراخوانِ بیرون
    پیام را می‌گیرد و ادامهٔ جریان (مثل آزادکردن قفل ساعت) را انجام می‌دهد.
    """

    def __init__(self, message=""):
        super().__init__(message)
        self.message = message


def reap_stuck_sms(minutes=15):
    """پیامک‌هایی که هرگز نتیجه‌شان نوشته نشد (پروسه میانهٔ ارسال مُرد، یا قفل نوشتن
    نتیجه را از دست داد) از «queued/sending» به «failed» می‌روند.

    بی این جارو، یک رکورد برای همیشه «در حال ارسال» می‌ماند و در پنلِ «پیامک‌های
    بی‌نتیجه» دیده نمی‌شود — یعنی پیامکِ گم‌شدهٔ بی‌صدا.
    """
    cutoff = (datetime.now() - timedelta(minutes=max(1, int(minutes)))).strftime("%Y-%m-%d %H:%M:%S")
    return changed("UPDATE sms_log SET status='failed' "
                   "WHERE status IN ('queued','sending') AND created_at < ?", (cutoff,))


def _run_after_commit():
    """کارهای موکول‌شده (ارسال واقعی پیامک) را فقط پس از commit موفق اجرا می‌کند."""
    pend = getattr(g, "_tx_after", None)
    if not pend:
        return
    g._tx_after = []
    for fn in pend:
        try:
            fn()
        except Exception as exc:                   # پیامکِ نگرفته‌شده نباید پاسخ را بشکند
            try:
                current_app.logger.warning("after-commit hook failed: %s", exc)
            except Exception:
                print("after-commit hook failed:", exc)


def after_commit(fn):
    """کاری را به بعد از commit موفق موکول می‌کند (بیرون تراکنش، فوراً اجرا می‌شود)."""
    if tx_depth():
        g.setdefault("_tx_after", []).append(fn)
    else:
        fn()


def query(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def query_d(sql, args=(), one=False):
    rows = [dict(r) for r in query(sql, args)]
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    if not tx_depth():
        db.commit()
    t = cache.table_of(sql)
    if t:
        cache.touch(t)
    return cur.lastrowid


def changed(sql, args=()):
    """مثل execute ولی تعداد ردیف‌های واقعاً عوض‌شده را برمی‌گرداند.

    برای نوشتن‌های «شرطی» لازم است: `UPDATE … WHERE id=? AND status=?` اگر صفر
    ردیف بگیرد یعنی کسی دیگر همان کار را کرده، و صدا کردنِ «انجام شد» بعد از آن
    دروغ گفتن به کاربر است (دو بار لغو = دو بازپرداخت، ویرایش قیمت مالِ دیگر).
    """
    db = get_db()
    cur = db.execute(sql, args)
    if not tx_depth():
        db.commit()
    t = cache.table_of(sql)
    if t:
        cache.touch(t)
    return cur.rowcount


def scalar(sql, args=(), default=0):
    row = query(sql, args, one=True)
    if not row or row[0] is None:
        return default
    return row[0]


def setting(key, default=None):
    v = scalar("SELECT value FROM settings WHERE key=?", (key,), None)
    return v if v is not None else default


SETTINGS_CACHE_KEYS = ("site_name", "support_phone", "platform_fee_percent",
                       "min_deposit_percent", "reminder_hours")


def setting_cached(key, default=None):
    """همان setting ولی برای مقادیر نمایشی که هر درخواست به آن‌ها نیاز دارد.

    نوشتن روی جدول settings در `set_setting` کش را بی‌اعتبار می‌کند، پس مدیر بلافاصله
    تغییر را می‌بیند. تنظیماتِ رفتارِ سیستم (مثل last_reminder_run) عمداً این‌طور
    خوانده نمی‌شوند، چون باید همیشه تازه بمانند.
    """
    if key not in SETTINGS_CACHE_KEYS:
        return setting(key, default)
    v = cache.remember("setting:" + key, lambda: setting(key, None), tables=("settings",))
    return v if v is not None else default


def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    cache.touch("settings")


def log_audit(user_id, role, action, target="", detail=""):
    """ردپای عملیات حساس: چه کسی، چه کاری، روی چه چیزی — برای پیگیری بعدی."""
    return execute("INSERT INTO audit_log(user_id,role,action,target,detail,created_at) "
                   "VALUES(?,?,?,?,?,?)",
                   (user_id, role or "", str(action), str(target)[:120], str(detail)[:250], now_str()))


def init_schema():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    db = get_db()
    db.executescript(SCHEMA)
    ensure_columns(db)
    ensure_indexes(db)
    migrate_places(db)
    db.commit()
    cache.clear()


# ایندکس‌هایی که به SCHEMA اضافه شده‌اند؛ دیتابیس‌های قدیمی با همین لیست ارتقا
# می‌یابند (CREATE INDEX IF NOT EXISTS بی‌ضرر و قابل تکرار است).
HOT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(date, status)",
    "CREATE INDEX IF NOT EXISTS idx_appt_svc ON appointments(service_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_tx_appt ON transactions(appointment_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_tx_biz ON transactions(business_id, kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_ref_appt ON refunds(appointment_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_ref_tx ON refunds(transaction_id)",
    "CREATE INDEX IF NOT EXISTS idx_sms_mobile ON sms_log(mobile, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sms_appt ON sms_log(appointment_id, template)",
    "CREATE INDEX IF NOT EXISTS idx_otp_mobile ON otp_codes(mobile, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_otp_ip ON otp_codes(ip, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_lock_exp ON slot_locks(status, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_lock_biz ON slot_locks(business_id, date, status)",
    "CREATE INDEX IF NOT EXISTS idx_settle_biz ON settlements(business_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_services_biz ON services(business_id, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_hours_biz ON working_hours(business_id, weekday)",
    "CREATE INDEX IF NOT EXISTS idx_blocks_biz_date ON day_blocks(business_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_biz_owner ON businesses(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_biz_status ON businesses(status, category_id, city_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_rate_bucket ON rate_events(bucket, window_start)",
]


def ensure_indexes(db):
    for sql in HOT_INDEXES:
        db.execute(sql)
    # جدول سقف‌های نرخ ممکن است در دیتابیس قدیمی نباشد
    db.execute("CREATE TABLE IF NOT EXISTS rate_events ("
               "id INTEGER PRIMARY KEY, bucket TEXT NOT NULL, window_start TEXT NOT NULL,"
               "cnt INTEGER NOT NULL DEFAULT 1, UNIQUE(bucket, window_start))")


# افزونهٔ ستون برای دیتابیس‌هایی که با نسخهٔ قبلی ساخته شده‌اند (مهاجرت سبک)
NEW_COLUMNS = [
    ("sms_log", "appointment_id", "INTEGER"),
    ("otp_codes", "ip", "TEXT"),
    ("districts", "norm", "TEXT"),
    ("cities", "norm", "TEXT"),
    ("provinces", "norm", "TEXT"),
]


def ensure_columns(db):
    for table, col, typ in NEW_COLUMNS:
        have = {r[1] for r in db.execute("PRAGMA table_info(%s)" % table)}
        if have and col not in have:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))


def migrate_places(db):
    """کلید norm را برای استان/شهر/محله می‌سازد و محله‌های هم‌نام یک شهر را ادغام می‌کند.

    استان و شهر را فقط مدیر می‌سازد، پس برای آن‌ها یکتایی اجباری نمی‌کنیم (ادغام
    آن‌ها دادهٔ کسب‌وکارها را جابه‌جا می‌کند و باید دستی باشد). محله را مالک با دست
    می‌نویسد، پس یکتایی (شهر، norm) و ادغام خودکار لازم است.
    """
    for table, parent in (("provinces", None), ("cities", "province_id"), ("districts", "city_id")):
        rows = [dict(x) for x in db.execute(
            "SELECT * FROM %s ORDER BY id" % table)]
        keeper, dupes = {}, []
        for r in rows:
            key = (r[parent] if parent else 0, norm_place(r["name"]))
            if key in keeper:
                dupes.append((r["id"], keeper[key]))
            else:
                keeper[key] = r["id"]
        if table == "districts":
            for did, keep in dupes:
                db.execute("UPDATE businesses SET district_id=? WHERE district_id=?", (keep, did))
                db.execute("DELETE FROM districts WHERE id=?", (did,))
        for key, pid in keeper.items():
            db.execute("UPDATE %s SET norm=? WHERE id=?" % table, (key[1], pid))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_districts_city_norm "
               "ON districts(city_id, norm) WHERE norm <> ''")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cities_norm ON cities(province_id, norm)")


def get_or_create_district(city_id, name):
    """محله بر اساس نامِ یک‌شکل‌شده پیدا یا ساخته می‌شود؛ نام نمایشی همان چیزی است که نوشته شده.

    دو مشتری هم‌زمان در دو کسب‌وکارِ یک شهر ممکن است همان محله را برای اولین بار
    بنویسند؛ یکتایی (city_id, norm) جلوی دوباره‌سازی را می‌گیرد، پس اگر INSERT رد شد
    رکورد برنده را برمی‌گردانیم (نه خطا به کاربر).
    """
    city_id = int(city_id)
    display = re.sub(r"\s+", " ", (name or "").strip())[:60]
    key = norm_place(display)
    if not key:
        return None
    did = scalar("SELECT id FROM districts WHERE city_id=? AND norm=?", (city_id, key), None)
    if did:
        # اگر شکل نمایشی عوض شد (مثلاً نیم‌فاصله درست)، همان رکورد به‌روز می‌شود
        cur = scalar("SELECT name FROM districts WHERE id=?", (did,), "")
        if cur != display:
            execute("UPDATE districts SET name=? WHERE id=?", (display, did))
        return did
    try:
        return execute("INSERT INTO districts(city_id,name,norm) VALUES(?,?,?)",
                       (city_id, display, key))
    except sqlite3.IntegrityError:
        return scalar("SELECT id FROM districts WHERE city_id=? AND norm=?", (city_id, key), None)


# ---------------------------------------------------------------- پیامک
_DIGITS_RE = re.compile(r"\d+")
TEMPLATES = {
    "otp": "کد تأیید نوبتین: {code}",
    "booking_ok": "نوبتین | رزرو شما در {biz} برای {date} ساعت {time} تأیید شد. کد پیگیری {code}",
    "reminder": "نوبتین | یادآوری: {date} ساعت {time} در {biz} منتظر شما هستیم.",
    "cancelled": "نوبتین | رزرو {code} در {biz} لغو شد. وضعیت بازپرداخت: {refund}",
    "refund": "نوبتین | {amount} تومان به حساب شما بازگردانده شد. پیگیری {ref}",
    "new_booking": "نوبتین | رزرو جدید: {service} ساعت {time} در {date}",
    "payment_failed": "نوبتین | پرداخت رزرو {code} موفق نبود و زمان آزاد شد. می‌توانید دوباره تلاش کنید.",
    "expired": "نوبتین | مهلت پرداخت رزرو {code} تمام شد؛ زمان برای دیگران آزاد گردید.",
    "rescheduled": "نوبتین | رزرو {code} در {biz} به {date} ساعت {time} تغییر زمان یافت.",
    "completed": "نوبتین | رزرو {code} در {biz} انجام‌شده ثبت شد.",
    "no_show": "نوبتین | رزرو {code} در {biz} بدون حضور شما ثبت شد؛ برای پیگیری با مجموعه تماس بگیرید.",
    "settlement": "نوبتین | تسویهٔ {amount} تومان برای {biz} انجام شد. پیگیری {ref}",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return "—"


def _dispatch(mobile, body):
    """فرستادن واقعی به اپراتور. پیش‌فرض «log» است تا هیچ پول/شماره‌ای خرج نشود.

    برای وصل‌کردن سرویس واقعی: NOBATIN_SMS_PROVIDER=kavenegar به‌همراه
    KAVENEGAR_KEY و KAVENEGAR_SENDER در محیط (یا .env) قرار گیرد.
    """
    provider = "log"
    try:
        provider = current_app.config.get("SMS_PROVIDER", "log")
    except Exception:
        provider = os.environ.get("NOBATIN_SMS_PROVIDER", "log")
    if provider != "kavenegar":
        return "sent", ""
    key = os.environ.get("KAVENEGAR_KEY", "").strip()
    sender = os.environ.get("KAVENEGAR_SENDER", "").strip()
    if not key:
        return "failed", "KAVENEGAR_KEY تنظیم نشده است."
    try:                                              # بدون وابستگی اضافه
        from urllib.parse import urlencode
        from urllib.request import urlopen
        data = urlencode({"receptor": mobile, "sender": sender, "message": body}).encode()
        with urlopen("https://api.kavenegar.com/v1/%s/sms/send.json" % key,
                     data=data, timeout=6) as resp:
            payload = resp.read().decode("utf-8", "ignore")
        return ("sent", "") if '"effectiveApi"' in payload else ("failed", payload[:200])
    except Exception as exc:
        return "failed", str(exc)[:200]


def send_sms(mobile, template, _appt_id=None, **ctx):
    """ثبت پیامک در sms_log و ارسال از طریق provider. هیچ پیامکی بی‌نتیجه گم نمی‌شود.

    اگر داخل تراکنش باشیم، رکورد «queued» نوشته می‌شود و ارسال واقعی به بعد از
    commit موکول می‌گردد: قفل نوشتن SQLite نباید وسط یک درخواست HTTP باز بماند،
    و اگر تراکنش rollback شد، پیامکی هم که هرگز نباید می‌رفته ارسال نمی‌شود.
    """
    mob = normalize_mobile(mobile or "")
    if ctx and template in TEMPLATES:
        body = TEMPLATES[template].format_map(_SafeDict(ctx))
    elif template in TEMPLATES and not ctx:
        body = TEMPLATES[template]
    else:
        body = template
    # آنچه در sms_log می‌ماند می‌تواند «بدنهٔ کد» باشد: در production رقم‌های
    # پیامکِ کدِ ورود ماسکه می‌شوند (پنل پیامک‌ها و /sms-box کد زنده نشان نمی‌دهند)،
    # ولی ارسال واقعی به اپراتور همان متن کامل می‌رود.
    store = body
    if template == "otp" and cfg("SMS_MASK_OTP", False):
        store = _DIGITS_RE.sub("•" * 5, body)
    if not is_valid_mobile(mob):
        # شمارهٔ ثابت یا نامعتبر: پیامک نمی‌رود، ولی در لاگ با وضعیت skipped می‌ماند
        return execute("INSERT INTO sms_log(mobile,template,body,status,appointment_id,created_at) "
                       "VALUES(?,?,?,?,?,?)", (mob or (mobile or ""), template, store, "skipped",
                                               _appt_id, now_str()))
    sid = execute("INSERT INTO sms_log(mobile,template,body,status,appointment_id,created_at) "
                  "VALUES(?,?,?,?,?,?)", (mob, template, store,
                                         "queued" if tx_depth() else "sending",
                                         _appt_id, now_str()))
    after_commit(lambda: _deliver(sid, mob, body))
    return sid


def _deliver(sid, mob, body):
    """ارسال واقعی + ثبت نتیجه روی همان رکورد (خارج از تراکنش)."""
    status, err = _dispatch(mob, body)
    try:
        execute("UPDATE sms_log SET status=? WHERE id=?", (status, sid))
    except sqlite3.Error as exc:
        # نوشتنِ نتیجه شکست: رکورد در «sending» می‌ماند و جارو بعداً «failed» می‌کند،
        # پس پیامکِ بی‌نتیجه در پنل «بدون موفق» دیده می‌شود نه بی‌سروصدا.
        _log_error("sms result write failed (id=%s): %s", sid, exc)
    # هرگز متن پیامک و شمارهٔ کامل را در لاگ نمی‌نویسیم: بدنهٔ پیامکِ کدِ ورود،
    # کلیدِ حساب است و فایل لاگ (و journald) معمولاً دسترسی‌اش بازتر از دیتابیس است.
    line = "SMS[%s] id=%s to=…%s len=%s%s" % (status, sid, (mob or "")[-4:], len(body or ""),
                                              (" err=" + err[:120]) if err else "")
    try:
        current_app.logger.info(line)
    except Exception:
        print(line)


def _log_error(msg, *args):
    try:
        current_app.logger.error(msg, *args)
    except Exception:
        print(msg % args if args else msg)
    return status


def sms_sent(appt_id, template):
    """آیا این پیامک خاص برای این نوبت فرستاده شده است؟ (ضد تکرار یادآوری)"""
    return bool(scalar("SELECT id FROM sms_log WHERE appointment_id=? AND template=? AND status='sent'",
                       (appt_id, template), None))


def business_owner_mobile(business_id):
    """شمارهٔ تماس صاحب کسب‌وکار برای اطلاع‌رسانی؛ خط ثابت پیامک‌پذیر نیست."""
    return scalar("SELECT u.mobile FROM businesses b JOIN users u ON u.id=b.owner_id "
                  "WHERE b.id=?", (business_id,), "")


# ---------------------------------------------------------------- seed
def ensure_admin(mobile=None, name=None):
    """اگر هیچ مدیری در سامانه نیست، از NOBATIN_ADMIN_MOBILE یکی می‌سازد.

    بدون این، سرور تازهٔ بدون دادهٔ نمونه هیچ راه ورودی به پنل مدیریت ندارد؛ با آن
    هم فقط شماره‌ای که خودِ ناشر در محیط گذاشته مدیر می‌شود، نه یک شمارهٔ نمونهٔ
    حدس‌زدنی.
    """
    if scalar("SELECT COUNT(*) FROM users WHERE role='admin'", (), 0):
        return None
    mob = normalize_mobile(mobile or cfg("ADMIN_MOBILE", ""))
    if not mob or not is_valid_mobile(mob):
        return None
    u = get_or_create_user(mob, name or cfg("ADMIN_NAME", "مدیر نوبتین"), role="admin")
    log_audit(u["id"], "admin", "admin_bootstrap", f"user:{u['id']}", "ساخت اولین حساب مدیر")
    return u


def seed(force=None):
    """دادهٔ نمونهٔ نمایشی.

    در production عمداً ساخته **نمی‌شود**: حساب مدیر با شمارهٔ شناخته‌شدهٔ
    ۰۹۱۲۰۰۰۰۰۰۰ و پنج کسب‌وکار نمونه، روی اینترنت یک راه ورود مهمان‌ناخوانده است.
    با NOBATIN_SEED=1 می‌توان به‌صورت صریح روشنش کرد (مثلاً روی سرور نمایشی).
    """
    if force is None:
        force = bool(cfg("SEED_DEMO", True))
    if not force:
        ensure_admin()
        return
    if scalar("SELECT COUNT(*) FROM users", (), 0):
        return
    db = get_db()

    for k, v in {
        "site_name": "نوبتین",
        "platform_fee_percent": "5",
        "min_deposit_percent": "20",
        "reminder_hours": "12",
        "support_phone": "۰۲۱۹۱۰۰۰۰۰۰",
    }.items():
        set_setting(k, v)

    provs = {"تهران": ["تهران", "شهریار", "اسدآباد", "ورامین"],
             "اصفهان": ["اصفهان", "کاشان", "شاهین‌شهر"],
             "فارس": ["شیراز", "فراشبند", "کازرون"],
             "البرز": ["کرج", "فردیس", "نظرآباد"],
             "خراسان رضوی": ["مشهد", "تورجین", "نیشابور"]}
    provs["خراسان رضوی"][1] = "تربت‌حیدریه"
    for pname, cities in provs.items():
        pid = execute("INSERT INTO provinces(name,norm) VALUES(?,?)", (pname, norm_place(pname)))
        for cname in cities:
            execute("INSERT INTO cities(province_id,name,norm) VALUES(?,?,?)",
                    (pid, cname, norm_place(cname)))

    cats = [
        ("آرایش و زیبایی", "scissors", "مراکز زیبایی بانوان و آقایان"),
        ("کلینیک و سلامت", "pulse", "پزشکی، دندان‌پزشکی و فیزیوتراپی"),
        ("تعمیرات خودرو", "car", "مکانیکی، برق و صافکاری"),
        ("ورزش و اسپا", "leaf", "باشگاه، سونا و ماساژ"),
        ("آموزش و مشاوره", "cap", "کلاس، زبان و مشاوره"),
        ("خانه و خدمات", "home", "لوله‌کشی، نظافت و تعمیرات"),
    ]
    for t, icon, blurb in cats:
        execute("INSERT INTO categories(title,icon,blurb,sort) VALUES(?,?,?,?)", (t, icon, blurb, len(cats)))

    # ---------- کاربرها (ورود فقط با OTP؛ رمز ندارد)
    def mk_user(mobile, name, role):
        return execute("INSERT INTO users(mobile,name,role,created_at) VALUES(?,?,?,?)",
                       (normalize_mobile(mobile), name, role, now_str()))

    admin = mk_user("09120000000", "مدیر نوبتین", "admin")
    o1 = mk_user("09121112233", "سارا موحد", "business")
    o2 = mk_user("09123334455", "امیر رضایی", "business")
    o3 = mk_user("09126667788", "نگار کریمی", "business")
    o4 = mk_user("09128889900", "بهزاد قاسمی", "business")
    c1 = mk_user("09301234567", "مهدی نوروزی", "customer")
    c2 = mk_user("09357654321", "لیلا صادقی", "customer")

    tehran = scalar("SELECT id FROM cities WHERE name='تهران'", (), None)
    isf = scalar("SELECT id FROM cities WHERE name='اصفهان'", (), None)
    shiraz = scalar("SELECT id FROM cities WHERE name='شیراز'", (), None)
    karaj = scalar("SELECT id FROM cities WHERE name='کرج'", (), None)
    prov_tehran = scalar("SELECT id FROM provinces WHERE name='تهران'", (), None)
    prov_isf = scalar("SELECT id FROM provinces WHERE name='اصفهان'", (), None)
    prov_fars = scalar("SELECT id FROM provinces WHERE name='فارس'", (), None)
    prov_alb = scalar("SELECT id FROM provinces WHERE name='البرز'", (), None)

    def province_of(city):
        return scalar("SELECT province_id FROM cities WHERE id=?", (city,), None)

    def mk_district(city, name):
        return get_or_create_district(city, name)

    businesses = [
        dict(owner=o1, name="آرایشگاه بانوان آراسته", slug="arasteh-beauty", category=1,
             city=tehran, district="پاسداران", hue=330,
             address="تهران، پاسداران، کوچه یاسمن، پلاک ۱۴", phone="021-22233445",
             about="مرکز زیبایی بانوان با بیش از دوازده سال سابقه؛ خدمات موی طبیعی، رنگ و لایت، "
                   "میکروبلیدینگ و بستهٔ عروس. رزرو آنلاین برای همهٔ خدمات امکان‌پذیر است.",
             payment_mode="full", deposit=0,
             policy=[{"min_hours": 24, "refund_percent": 50}, {"min_hours": 2, "refund_percent": 0}],
             ),
        dict(owner=o2, name="کلینیک دندان‌پزشکی دنتال‌پرس", slug="dental-plus", category=2,
             city=tehran, district="سعادت‌آباد", hue=205,
             address="تهران، سعادت‌آباد، میدان کاج، برج سلامت، طبقه ۳", phone="021-44455667",
             about="کلینیک تخصصی دندان‌پزشکی با تجهیزات دیجیتال، ارتودنسی نامرئی و جراحی ایمپلنت. "
                   "درمان‌های اورژانسی خارج از نوبت نیز ارائه می‌شود.",
             payment_mode="deposit", deposit=120000,
             policy=[{"min_hours": 48, "refund_percent": 100}, {"min_hours": 24, "refund_percent": 50},
                     {"min_hours": 0, "refund_percent": 0}],
             ),
        dict(owner=o3, name="اسپا و ماساژ زرین", slug="zarrin-spa", category=4,
             city=isf, district="نجف‌آباد", hue=158,
             address="اصفهان، نجف‌آباد، بلوار کاشان، مجتمع زرین", phone="031-33344556",
             about="مجموعهٔ اسپا با سونای خشک و بخار، جکوزی، ماساژ سوئدی و سنگ داغ. "
                   "هر نوبت شامل دوش و لباس اختصاصی است.",
             payment_mode="full", deposit=0,
             policy=[{"min_hours": 6, "refund_percent": 50}, {"min_hours": 0, "refund_percent": 0}],
             ),
        dict(owner=o4, name="تعمیرگاه خودرو کارن‌سرویس", slug="karen-service", category=3,
             city=karaj, district="عظیمیه", hue=22,
             address="کرج، عظیمیه، بلوار میمن‌دیزج، کوچه ۱۲", phone="026-32244668",
             about="خدمات سریع تعویض روغن، دیسک و صفحه، تنظیم موتور و عیب‌یابی دیاگ. "
                   "بدون نوبت‌دهی تلفنی — همه‌چیز آنلاین.",
             payment_mode="free", deposit=0,
             policy=[{"min_hours": 1, "refund_percent": 100}],
             ),
        dict(owner=o1, name="باشگاه پیکان فیتنس", slug="peykane-fitness", category=4,
             city=shiraz, district="معدلی", hue=268,
             address="شیراز، معدلی، خیابان لاکان، پلاک ۸", phone="071-36677889",
             about="باشگاه بدنسازی و کراس‌فیت با مربی اختصاصی، کلاس‌های گروهی و بدن‌سازی تخصصی.",
             payment_mode="deposit", deposit=50000,
             policy=[{"min_hours": 12, "refund_percent": 100}, {"min_hours": 0, "refund_percent": 20}],
             ),
    ]
    biz_ids = []
    for i, b in enumerate(businesses):
        bid = execute(
            "INSERT INTO businesses(owner_id,name,slug,category_id,province_id,city_id,district_id,"
            "address,phone,about,hue,rating,rating_count,status,payment_mode,deposit_amount,"
            "cancel_policy,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (b["owner"], b["name"], b["slug"], b["category"], province_of(b["city"]), b["city"],
             mk_district(b["city"], b["district"]), b["address"], b["phone"], b["about"], b["hue"],
             0, 0, "active", b["payment_mode"], b["deposit"],
             json.dumps(b["policy"], ensure_ascii=False), now_str()))
        biz_ids.append(bid)
        for wd in range(6 if i != 3 else 7):   # کارن‌سرویس همهٔ روزها باز است
            start, end = ("09:00", "19:00") if i != 1 else ("09:00", "21:00")
            execute("INSERT INTO working_hours(business_id,weekday,start_hm,end_hm,slot_min) "
                    "VALUES(?,?,?,?,?)", (bid, wd, start, end, 30))

    services = [
        (1, "رنگ و لایت تخصصی", 950000, 120, "رنگ آمیژا با مواد ایگویال + لایت مش"),
        (1, "کراتین پروتئینی", 1450000, 150, "کراتین برازیلی، ضمانت سه‌ماهه"),
        (1, "میکروبلیدینگ ابرو", 2100000, 90, "تیتاژ + فیکس، جلسهٔ ترمیم رایگان"),
        (1, "بستهٔ مراقبت پوست", 680000, 60, "پاک‌سازی، ماسک و مزوتراپی خانگی"),
        (2, "معالیت عصب‌کشی", 780000, 60, "درمان ریشه با روتاری و سمنت بیواند"),
        (2, "جرم‌بندی و بلیچینگ", 1250000, 45, "بلیچینگ لبه‌ای با ژل ۳۵٪"),
        (2, "مشاوره ارتودنسی", 350000, 30, "اسکن ۳بعدی و طرح درمان"),
        (2, "ایمپلنت (نوبت جراحی)", 8500000, 90, "برند استرمن، گارانتی پنج‌ساله"),
        (3, "ماساژ سوئدی ۶۰ دقیقه", 520000, 60, "روغن آرگان، فشار متوسط"),
        (3, "بستهٔ سونا و جکوزی", 380000, 90, "سونای خشک، بخار و جکوزی"),
        (3, "ماساژ سنگ داغ", 690000, 75, "سبزینهٔ ۴۵ درجه"),
        (4, "تعویض روغن و فیلتر", 480000, 45, "روغن توتال ۱۰W40 + فیلتر اصلی"),
        (4, "دیاگ و عیب‌یابی", 250000, 30, "گزارش کد خطا + توصیه"),
        (4, "سرویس ترمز کامل", 1350000, 120, "لنت، دیسک و هواگیری"),
        (5, "کلاس بدنسازی با مربی", 320000, 60, "برنامهٔ اختصاصی ۳ماهه"),
        (5, "کلاس کراس‌فیت گروهی", 180000, 45, "حداکثر ۱۰ نفر"),
    ]
    for bid, title, price, dur, desc in services:
        execute("INSERT INTO services(business_id,title,price,duration_min,description,sort) "
                "VALUES(?,?,?,?,?,?)", (biz_ids[bid - 1], title, price, dur, desc, price))

    # ---------------- دادهٔ نمونهٔ مالی/نوبت: چند وضعیت مختلف، جدا و قابل پیگیری
    svc_rows = {bid: [] for bid in biz_ids}
    for r in query("SELECT id, business_id FROM services ORDER BY id"):
        svc_rows[r["business_id"]].append(r["id"])

    demo = [
        # (user, biz_index, day_offset, time, svc_index, status, pay_status, paid)
        (c1, 1, 1, "11:30", 0, "confirmed", "paid", 780000),
        (c2, 0, 2, "15:00", 1, "confirmed", "paid", 950000),
        (c1, 2, 0, "17:00", 0, "cancelled", "refunded", 520000),
        (c2, 3, -3, "10:00", 1, "completed", "paid", 250000),
    ]
    seq = 1040
    for uid, bidx, off, hm, sidx, st, pst, paid in demo:
        bid = biz_ids[bidx]
        d = (date.today() + timedelta(days=off)).isoformat()
        sid_list = svc_rows.get(bid) or []
        if not sid_list:
            continue
        svc_id = sid_list[sidx % len(sid_list)]
        svc = dict(query("SELECT * FROM services WHERE id=?", (svc_id,), one=True))
        biz = dict(query("SELECT * FROM businesses WHERE id=?", (bid,), one=True))
        end = f"{(int(hm[:2]) + svc['duration_min'] // 60) % 24:02d}:" \
              f"{(int(hm[3:]) + svc['duration_min'] % 60) % 60:02d}"
        seq += 1
        code = f"NB-{seq}"
        aid = execute(
            "INSERT INTO appointments(code,user_id,business_id,service_id,date,start_hm,end_hm,amount,"
            "paid_amount,payable_now,status,pay_status,cancel_policy,created_at,confirmed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, uid, bid, svc_id, d, hm, end, svc["price"], paid, paid, st, pst,
             biz["cancel_policy"], now_str(), now_str()))
        execute("INSERT INTO slot_locks(business_id,service_id,date,start_hm,holder_user_id,status,"
                "expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (bid, svc_id, d, hm, uid, "booked", now_str(), now_str()))
        tid = execute(
            "INSERT INTO transactions(reference,appointment_id,user_id,business_id,kind,amount,status,"
            "gateway,auth_code,trace,created_at,done_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"TRX{seq}", aid, uid, bid, "payment", paid, "success", "sandbox",
             f"A{seq}", f"T{seq}", now_str(), now_str()))
        if st == "cancelled":
            dec = evaluate_cancellation(biz["cancel_policy"], f"{d} {hm}")
            pct, label = dec["refund_percent"], dec["label"]
            amount = int(round(paid * pct / 100.0))
            rid = execute("INSERT INTO refunds(transaction_id,appointment_id,amount,percent,status,"
                          "rule_label,created_at,done_at,reference) VALUES(?,?,?,?,?,?,?,?,?)",
                          (tid, aid, amount, pct, "done" if amount else "rejected", label,
                           now_str(), now_str(), f"RTX{seq}"))
            if amount:
                execute("INSERT INTO transactions(reference,appointment_id,user_id,business_id,kind,"
                        "amount,status,gateway,created_at,done_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (f"RTX{seq}", aid, uid, bid, "refund", amount, "success", "sandbox",
                         now_str(), now_str()))
            execute("UPDATE appointments SET cancelled_at=?, cancel_reason=? WHERE id=?",
                    (now_str(), "تغییر برنامهٔ مشتری", aid))
        send_sms(uid and query("SELECT mobile FROM users WHERE id=?", (uid,), one=True)["mobile"],
                 "booking_ok", biz=biz["name"], date=d, time=hm, code=code)

    # تسویهٔ نمونه برای یک کسب‌وکار
    execute("INSERT INTO settlements(business_id,amount,period_from,period_to,status,created_at,paid_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (biz_ids[1], 702000, (date.today() - timedelta(days=20)).isoformat(),
             date.today().isoformat(), "paid", now_str(), now_str()))

    db.commit()


def get_or_create_user(mobile, name=None, role="customer"):
    mobile = normalize_mobile(mobile)
    row = query("SELECT * FROM users WHERE mobile=?", (mobile,), one=True)
    if row:
        return dict(row)
    try:
        uid = execute("INSERT INTO users(mobile,name,role,created_at) VALUES(?,?,?,?)",
                      (mobile, name or "", role, now_str()))
    except sqlite3.IntegrityError:
        # دو درخواست هم‌زمان با یک شماره: یکی برنده شده، همان را برمی‌گردانیم
        row = query("SELECT * FROM users WHERE mobile=?", (mobile,), one=True)
        if row:
            return dict(row)
        raise
    return dict(query("SELECT * FROM users WHERE id=?", (uid,), one=True))


def cfg(key, default=None):
    """مقدار تنظیمات اپ؛ بیرون از app context هم نمی‌ترکد (برای اسکریپت‌های cron)."""
    try:
        v = current_app.config.get(key, default)
    except Exception:
        v = default
    return default if v is None else v


def create_otp(mobile, ip=None):
    """کد یک‌بارمصرف تازه. (code, None) یا (None, پیام_خطا) اگر زودتر از اجازه باشد."""
    mobile = normalize_mobile(mobile)
    ok, msg = otp_throttle(mobile, ip)
    if not ok:
        return None, msg
    code = "12345" if cfg("OTP_MASTER") else f"{secrets.randbelow(90000) + 10000}"
    execute("UPDATE otp_codes SET used_at=? WHERE mobile=? AND used_at IS NULL", (now_str(), mobile))
    execute("INSERT INTO otp_codes(mobile,code,attempts,ip,created_at) VALUES(?,?,0,?,?)",
            (mobile, code, ip or "", now_str()))
    send_sms(mobile, "otp", code=code)
    return code, None


def otp_throttle(mobile, ip=None):
    """محدودیت نرخ درخواست کد: فاصلهٔ زمانی، سقف هر موبایل در ساعت، سقف هر IP در ساعت."""
    since = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    last = scalar("SELECT created_at FROM otp_codes WHERE mobile=? ORDER BY id DESC LIMIT 1",
                  (mobile,), None)
    if last:
        try:
            prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            prev = None
        if prev:
            wait = int(cfg("OTP_COOLDOWN", 60)) - int((datetime.now() - prev).total_seconds())
            if wait > 0:
                return False, f"برای هر شماره فقط هر {fa(int(cfg('OTP_COOLDOWN', 60)) // 60)} دقیقه یک کد " \
                              f"می‌شود؛ {fa(wait)} ثانیه دیگر تلاش کنید."
    if scalar("SELECT COUNT(*) FROM otp_codes WHERE mobile=? AND created_at>?", (mobile, since), 0) \
            >= int(cfg("OTP_MAX_MOBILE", 5)):
        return False, "تعداد درخواست کد برای این شماره در یک ساعت گذشته به سقف رسیده است؛ " \
                      "یک ساعت دیگر دوباره امتحان کنید."
    if ip and scalar("SELECT COUNT(*) FROM otp_codes WHERE ip=? AND created_at>?", (ip, since), 0) \
            >= int(cfg("OTP_MAX_IP", 12)):
        return False, "درخواست‌های کد از این رایانه غیرعادی است؛ کمی بعد تلاش کنید."
    return True, ""


def verify_otp(mobile, code):
    mobile = normalize_mobile(mobile)
    row = query("SELECT * FROM otp_codes WHERE mobile=? AND used_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (mobile,), one=True)
    if not row:
        return False, "کدی ارسال نشده است. دوباره درخواست کد بدهید."
    created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() - created > timedelta(minutes=3):
        return False, "کد منقضی شده است؛ کد جدید بگیرید."
    # سقف تجمعی تلاش‌های اشتباه (روی همهٔ کدهای ۳۰ دقیقهٔ اخیر) — نه فقط کد فعلی
    win = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    fails_total = scalar("SELECT COALESCE(SUM(attempts),0) FROM otp_codes "
                         "WHERE mobile=? AND created_at>?", (mobile, win), 0)
    if fails_total >= int(cfg("OTP_MAX_ATTEMPTS", 12)):
        return False, "چند بار کد اشتباه وارد شد؛ برای امنیت موقتاً مسدود شدید. " \
                      "نیم ساعت دیگر کد جدید بگیرید."
    if row["attempts"] >= 5:
        return False, "تعداد تلاش‌ها بیش از حد است؛ کد جدید بگیرید."
    # مقایسهٔ پایدار در زمان: طول پاسخ نسبت به «درست/غلط» قابل اندازه‌گیری نشود
    if not secrets.compare_digest(str(row["code"]), str(code or "").strip()):
        execute("UPDATE otp_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
        return False, "کد واردش صحیح نیست."
    execute("UPDATE otp_codes SET used_at=? WHERE id=?", (now_str(), row["id"]))
    return True, row["code"]


# ---------------------------------------------------------------- سقف نرخ (ضد هجوم)
def throttle(bucket, limit, window_seconds, cost=1):
    """شمارندهٔ سراسریِ «در این پنجره چند بار؟» — روی دیتابیس، پس بین workerها هم مشترک است.

    برمی‌گرداند (اجازه_دارد_, ثانیه_تا_باز_شدن_پنجره). بی‌خطر در برابر رقابت:
    شمارش و خواندن در **یک جمله** انجام می‌شود (UPSERT … RETURNING). اگر نوشتن و
    خواندن دو حرکت بودند، دو درخواست هم‌زمان می‌توانستند عددِ یکدیگر را ببینند و
    یکی از آن‌ها باوجود زیرِ سقف رد شود — یعنی دقیقاً همان چیزی که سقف نرخ باید
    تضمین کند: «تا سقف مجاز بگذرد، پس از آن رد کند».
    """
    limit = int(limit or 0)
    if limit <= 0:
        return True, 0
    window_seconds = max(1, int(window_seconds))
    now_dt = datetime.now()
    anchor = int(now_dt.timestamp() // window_seconds) * window_seconds
    wstart = datetime.fromtimestamp(anchor).strftime("%Y-%m-%d %H:%M:%S")
    retry = int(anchor + window_seconds - now_dt.timestamp()) + 1
    try:
        row = get_db().execute(
            "INSERT INTO rate_events(bucket, window_start, cnt) VALUES(?,?,?) "
            "ON CONFLICT(bucket, window_start) DO UPDATE SET cnt = cnt + excluded.cnt "
            "RETURNING cnt", (bucket, wstart, cost)).fetchone()
        n = int(row["cnt"]) if row is not None else cost
    except sqlite3.OperationalError as exc:
        # فقط «این SQLite جملهٔ RETURNING را نمی‌شناسد» به مسیر دو حرکتی می‌رود.
        # خطاهای دیگر (دیسک پر، database is locked) باید همان‌جا دیده شوند، نه
        # تبدیل شدن به شمارندهٔ نادرستِ دو حرکتی که سقف را بی‌صدا بی‌اثر می‌کند.
        if "returning" not in str(exc).lower():
            _log_error("throttle: شمارندهٔ نرخ خراب شد (%s) → اجازه داده شد", exc)
            return True, 0
        try:
            execute("INSERT INTO rate_events(bucket, window_start, cnt) VALUES(?,?,?)",
                    (bucket, wstart, cost))
            n = cost
        except sqlite3.IntegrityError:
            execute("UPDATE rate_events SET cnt = cnt + ? WHERE bucket=? AND window_start=?",
                    (cost, bucket, wstart))
            n = scalar("SELECT cnt FROM rate_events WHERE bucket=? AND window_start=?",
                       (bucket, wstart), 0)
    return n <= limit, max(0, retry)


def throttle_reset(bucket=None):
    """پاک کردن شمارنده‌ها (برای آزمون و هم‌چنین کارِ نگهداری)."""
    if bucket:
        execute("DELETE FROM rate_events WHERE bucket LIKE ?", (bucket + "%",))
    else:
        execute("DELETE FROM rate_events")


# ---------------------------------------------------------------- نگهداری
def prune(days_otp=1, days_sms=60, days_audit=365, days_rate=2, vacuum=False):
    """قدیمی‌ها را جمع می‌کند: کدهای مصرف‌شده، پیامک‌ها و ردپاهای خیلی کهنه.

    مهم: پیش از حذف، بدنهٔ پیامک‌های کدِ تأیید ماسک می‌شود — آن رکوردها برای
    پیگیریِ «چه پیامکی رفت» لازم‌اند، ولی نگه‌داشتن کد یک‌بارمصرف به‌صورت متنِ باز
    در دیتابیس، ریسک بی‌مصرف است.
    """
    out = {}
    cutoff = lambda d: (datetime.now() - timedelta(days=max(0, int(d)))).strftime("%Y-%m-%d %H:%M:%S")
    # شمارشِ قبل از نوشتن؛ `execute` فقط lastrowid برمی‌گرداند نه تعداد سطر.
    n = scalar("SELECT COUNT(*) FROM otp_codes WHERE (used_at IS NOT NULL OR created_at < ?) "
               "AND code <> '—'", (cutoff(days_otp),), 0)
    execute("UPDATE otp_codes SET code='—' WHERE (used_at IS NOT NULL OR created_at < ?) "
            "AND code <> '—'", (cutoff(days_otp),))
    out["otp_masked"] = n
    for key, table, col, days in (("otp_deleted", "otp_codes", "created_at", days_otp),
                                  ("sms_deleted", "sms_log", "created_at", days_sms),
                                  ("audit_deleted", "audit_log", "created_at", days_audit),
                                  ("rate_deleted", "rate_events", "window_start", days_rate)):
        n = scalar("SELECT COUNT(*) FROM %s WHERE %s < ?" % (table, col), (cutoff(days),), 0)
        execute("DELETE FROM %s WHERE %s < ?" % (table, col), (cutoff(days),))
        out[key] = n
    out["sms_masked"] = scalar(
        "SELECT COUNT(*) FROM sms_log WHERE template='otp' AND body <> '—'", (), 0)
    execute("UPDATE sms_log SET body='—' WHERE template='otp' AND body <> '—'")
    db = get_db()
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    if vacuum:
        db.vacuum()
    cache.clear()
    return out
