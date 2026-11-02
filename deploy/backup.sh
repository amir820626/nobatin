#!/usr/bin/env bash
# نوبتین — نسخهٔ پشتیبانِ «سالم» از دیتابیس SQLite.
#
#   bash deploy/backup.sh                 # پیش‌فرض: /var/backups/nobatin، نگهداشتن ۱۴ روز
#   DEST=/data/backup bash deploy/backup.sh
#   bash deploy/backup.sh --keep 60
#
# چرا کپی کردنِ خود فایل کافی نیست: دیتابیس در حالت WAL است و نوشتن‌ها فعلاً در
# -wal می‌مانند؛ cp می‌تواند نسخه‌ای نصفه‌نیمه بدهد. اینجا از backup API پایتون
# استفاده می‌شود (همان چیزی که «آنلاین بکاپ» می‌گیرد)، سپس با integrity_check
# تأیید می‌شود و در صورت خطا کد خروجی ۱ می‌دهد تا timer/cron alarm بزند.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/nobatin}"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
DEST="${DEST:-/var/backups/nobatin}"
KEEP=14

while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    *) echo "گزینهٔ ناشناخته: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$DEST"
DB="$("$PY" - "$APP_DIR" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("NOBATIN_ENV", os.environ.get("NOBATIN_ENV", "production"))
from core import db as D
print(D.db_path())
PY
)"

[ -f "$DB" ] || { echo "دیتابیس پیدا نشد: $DB" >&2; exit 1; }

STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="$DEST/nobatin-$STAMP.db"
echo "  پشتیبان‌گیری: $DB → $OUT"

"$PY" - "$DB" "$OUT" <<'PY'
import sqlite3, sys, os
src, dst = sys.argv[1], sys.argv[2]
# اتصال به منبع readonly؛ مقصد با backup() ساخته می‌شود، پس تکه‌تکه نمی‌شود.
s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d, pages=1024)
d.close(); s.close()
# سلامت نسخه
c = sqlite3.connect(dst)
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
n = {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
     for t in ("users", "businesses", "appointments", "transactions")}
c.close()
if ok != "ok":
    print("  ✖ integrity_check: %s" % ok, file=sys.stderr); sys.exit(1)
print("  ✔ integrity_check ok | " + "، ".join("%s: %s" % (k, v) for k, v in n.items()))
print("  حجم: %.1f MB" % (os.path.getsize(dst) / 1024 / 1024))
PY

gzip -f "$OUT"
# نسخه‌های قدیمی‌تر از KEEP روز حذف می‌شوند (فقط فایل‌های پشتیبانِ خودِ این اسکریپت)
find "$DEST" -maxdepth 1 -name 'nobatin-*.db.gz' -mtime +"$KEEP" -delete -print |
  sed 's/^/  حذف شده: /' || true

# یک نشانگر برای --selfcheck (retention) و برای «آخرین پشتیبان کِی بود»
date +%Y-%m-%d\ %H:%M:%S > "$DEST/latest.txt"
ln -sfn "$(basename "$OUT").gz" "$DEST/latest.db.gz"
echo "  ✔ آماده: $(ls -1 "$DEST" | tail -1)"
