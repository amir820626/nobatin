#!/usr/bin/env bash
# نوبتین — بازگرداندن دیتابیس از یک نسخهٔ پشتیبان.
#
#   bash deploy/restore.sh /var/backups/nobatin/latest.db.gz --dry-run
#   systemctl stop nobatin && bash deploy/restore.sh /var/backups/nobatin/nobatin-2026-10-16-0310.db.gz
#
# تا --dry-run ندهید، فایل فعلی را هم کنار می‌گذارید (nobatin.db.before-restore-…)
# و فقط بعد از stop کردن سرویس این کار را بکنید؛ وگرنه عملیات در جریانِ write
# روی فایلی که جابه‌جا شده ادامه پیدا می‌کند.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/nobatin}"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

SRC="${1:-}"; MODE="${2:-}"
[ -n "$SRC" ] || { echo "نشانی نسخه را بدهید: restore.sh <backup.db.gz|backup.db> [--dry-run]"; exit 2; }

"$PY" - "$APP_DIR" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from core import db as D
print("  دیتابیس هدف:", D.db_path())
PY

DB="$("$PY" - "$APP_DIR" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from core import db as D
print(D.db_path())
PY
)"

TMP="$(mktemp -t nobatin-restore.XXXXXX.db)"
case "$SRC" in
  *.gz) gunzip -c "$SRC" > "$TMP" ;;
  *)    cp "$SRC" "$TMP" ;;
esac

"$PY" - "$TMP" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
n = {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
     for t in ("users", "businesses", "appointments", "transactions")}
c.close()
if ok != "ok":
    print("  ✖ نسخه سالم نیست: %s" % ok, file=sys.stderr); sys.exit(1)
print("  ✔ نسخه سالم است | " + "، ".join("%s: %s" % (k, v) for k, v in n.items()))
PY

if [ "$MODE" = "--dry-run" ]; then
  rm -f "$TMP"
  echo "  --dry-run: هیچ فایلی عوض نشد."
  exit 0
fi

if [ -f "$DB" ]; then
  KEEP="$DB.before-restore-$(date +%Y%m%d-%H%M%S)"
  cp "$DB" "$KEEP"
  echo "  نسخهٔ فعلی نگه داشته شد: $KEEP"
fi

# فایل اصلی + فایل‌های WAL/shm (وگرنه SQLite دادهٔ کهنهٔ -wal را می‌چسباند)
install -o "${USER:-nobatin}" -m 640 "$TMP" "$DB" 2>/dev/null || cp "$TMP" "$DB"
rm -f "$DB-wal" "$DB-shm" "$TMP"
echo "  ✔ بازگردانی شد. سرویس را دوباره روشن کنید: systemctl start nobatin"
