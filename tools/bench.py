# -*- coding: utf-8 -*-
"""سنجهٔ بار نوبتین — فقط کتابخانهٔ استاندارد پایتون، بدون وابستگی بیرونی.

چند کاربر/چند کسب‌وکار هم‌زمان که صفحه می‌بینند و رزرو می‌کنند، پرسش اصلی است:
این ابزار همان را اندازه می‌گیرد تا «تحمل می‌کند» یک عدد باشد نه یک حدس.

    python tools/bench.py --base http://127.0.0.1:5077 --threads 16 --seconds 30
    python tools/bench.py --base http://127.0.0.1:8000 --pages "/ /k/arasteh-beauty"
    python tools/bench.py --base http://127.0.0.1:5077 --login 09301234567 \
                          --pages "/pankack /kasbkar/arasteh-beauty"

چه چیزی می‌سنجد:
  * GETهای صفحه‌های عمومی (پیش‌فرض)؛ با --login همان درخواست‌ها با کوکی نشستِ
    واردشده (پنل‌ها، جاروب سبدها، کوئری‌های مالی) — یعنی سنگین‌ترین مسیرها.
  * اتصال‌ها keep-alive و به ازای هر ترد یکی‌اند، تا هزینهٔ برقراری TCP در عدد حل نشود.
  * در پایان: RPS، میانگین، p50/p90/p95/p99، بیشینه، کدهای وضعیت و شمار خطا.

صحتِ «دو نفر هم‌زمان یک ساعت را رزرو کنند» را این ابزار نمی‌سنجد؛ آن در بخش ۳۳
smoke_test با تردهای واقعی روی همان پایگاه داده آزموده می‌شود (برندهٔ یکتا).

خروجی: ۰ اگر بدون خطا و زیر حد آستانه؛ ۱ اگر هر پاسخ ۵xx یا خطای اتصال؛
۲ اگر p95 از --max-p95 بیشتر شد (بار زیاد، سرویس کند).
"""
import argparse
import http.client
import json
import os
import re
import ssl
import statistics
import sys
import threading
import time
import urllib.parse

DEFAULT_PAGES = ["/", "/jostojoo?q=%D8%AF%D8%A7%DB%8C%D8%AA%DB%8C", "/darbare", "/qavaed",
                 "/pooshhe"]


def fa(n):
    return str(n)


class Target:
    """یک مقصد http(s): میزبان، پورت و اتصالِ به‌ازای‌ترد با keep-alive."""

    def __init__(self, base):
        u = urllib.parse.urlparse(base if "//" in base else "//" + base, "http")
        self.scheme = u.scheme or "http"
        self.host = u.hostname
        self.port = u.port or (443 if self.scheme == "https" else 80)
        self.prefix = (u.path or "").rstrip("/")
        self.tls = None
        if self.scheme == "https":
            self.tls = ssl.create_default_context()
            if os.environ.get("BENCH_INSECURE") == "1":
                self.tls.check_hostname = False
                self.tls.verify_mode = ssl.CERT_NONE

    def connect(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, context=self.tls, timeout=15)
        return http.client.HTTPConnection(self.host, self.port, timeout=15)

    def url(self, path):
        return self.prefix + (path if path.startswith("/") else "/" + path)


LOCAL = threading.local()


def request_once(t, path, headers=None, method="GET", body=None):
    """یک درخواست روی اتصال همان ترد؛ اگر اتصال مرده بود یک‌بار تازه می‌شود."""
    conn = getattr(LOCAL, "conn", None)
    if conn is None:
        conn = LOCAL.conn = t.connect()
    sent = {"User-Agent": "nobatin-bench/1.0", "Accept": "text/html,application/json",
            "Connection": "keep-alive"}
    sent.update(headers or {})
    if body is not None:
        sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
    for attempt in (0, 1):
        try:
            conn.request(method, t.url(path), body=body, headers=sent)
            resp = conn.getresponse()
            return resp.status, resp.read(), {
                "set-cookie": resp.msg.get_all("Set-Cookie") or []}
        except (http.client.HTTPException, ConnectionError, OSError):
            try:
                conn.close()
            except Exception:
                pass
            if attempt:
                raise
            LOCAL.conn = conn = t.connect()


CSRF_RE = re.compile(rb'name="csrf" value="([^"]+)"')


def cookie_of(raw_cookies):
    """فقط نام=مقدار کوکی‌ها را نگه می‌داریم (سرصفحهٔ Cookie قوانین path را نمی‌فهمد،
    ولی کوکی نشست سراسری است و همین کافی است)."""
    return "; ".join(x.split(";")[0].strip() for x in raw_cookies if x.strip())


def dev_login(t, mobile):
    """ورود در حالت توسعه (کد مستر ۱۲۳۴۵) تا کوکی نشستِ واردشده در دست ترد اصلی باشد.

    روی سرور واقعی این کار ممکن نیست و نباید ممکن باشد؛ آنجا --login را نگذارید.
    """
    st, body, hd = request_once(t, "/vorood")
    jar = cookie_of(hd["set-cookie"])
    m = CSRF_RE.search(body or b"")
    if not m:
        raise SystemExit("توکن CSRF در صفحهٔ ورود پیدا نشد؛ --base درست است و اپ روشن؟")
    tok = m.group(1).decode()
    hdr = {"Cookie": jar}
    st, body, hd = request_once(t, "/vorood", headers=hdr, method="POST",
                                body="csrf=%s&mobile=%s" % (tok, urllib.parse.quote(mobile)))
    jar = cookie_of(hd["set-cookie"]) or jar
    st, body, hd = request_once(t, "/tayid", headers={"Cookie": jar})
    jar = cookie_of(hd["set-cookie"]) or jar
    m2 = CSRF_RE.search(body or b"")
    tok2 = m2.group(1).decode() if m2 else tok
    st, body, hd = request_once(t, "/tayid", headers={"Cookie": jar}, method="POST",
                                body="csrf=%s&mobile=%s&code=12345"
                                     % (tok2, urllib.parse.quote(mobile)))
    jar = cookie_of(hd["set-cookie"]) or jar
    if not jar:
        raise SystemExit("کوکی نشست داده نشد؛ ورود نشد (اپ در حالت توسعه با کد مستر روشن است؟)")
    st, body, _ = request_once(t, "/pankack", headers={"Cookie": jar})
    if st != 200 or "خروج" not in (body or b"").decode("utf-8", "ignore"):
        raise SystemExit("ورود آزمایشی موفق نشد (پاسخ %s). برای bench روی production "
                        "بی‌لاگین کار کنید." % st)
    return jar


def percentile(values, p):
    if not values:
        return 0.0
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(round(p / 100.0 * (len(vs) - 1)))))
    return vs[k]


def run(t, pages, threads, seconds, hdr):
    results, errs = [], []
    stop = time.time() + seconds

    def worker():
        lat, codes, local_err = [], {}, []
        try:
            while time.time() < stop:
                for p in pages:
                    if time.time() >= stop:
                        break
                    t0 = time.perf_counter()
                    try:
                        st, _body, _ = request_once(t, p, headers=hdr)
                    except Exception as exc:
                        codes["err"] = codes.get("err", 0) + 1
                        local_err.append("%s: %s" % (p, type(exc).__name__))
                        continue
                    codes[st] = codes.get(st, 0) + 1
                    lat.append((time.perf_counter() - t0) * 1000.0)
        finally:
            results.append((lat, codes))
            errs.extend(local_err)

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    t0 = time.perf_counter()
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    wall = time.perf_counter() - t0
    return results, errs, wall


def main():
    ap = argparse.ArgumentParser(description="سنجهٔ بار نوبتین")
    ap.add_argument("--base", default="http://127.0.0.1:5077", help="نشانی اپ در حال اجرا")
    ap.add_argument("--threads", type=int, default=8, help="شمار تردهای هم‌زمان (پیش‌فرض ۸)")
    ap.add_argument("--seconds", type=int, default=20, help="مدت آزمون (پیش‌فرض ۲۰ ثانیه)")
    ap.add_argument("--pages", default="", help="نشانی‌ها با فاصله: \"/ /k/arasteh /pankack\"")
    ap.add_argument("--login", default="", help="شمارهٔ موبایل برای ورود (فقط حالت توسعه)")
    ap.add_argument("--max-p95", type=float, default=1500.0,
                    help="آستانهٔ هشدار برای p95 بر حسب میلی‌ثانیه (پیش‌فرض ۱۵۰۰)")
    ap.add_argument("--json", action="store_true", help="خروجی JSON برای CI")
    args = ap.parse_args()

    t = Target(args.base)
    pages = (args.pages.split() if args.pages else list(DEFAULT_PAGES))
    pages = [p if p.startswith("/") else "/" + p for p in pages]
    hdr = None
    if args.login:
        jar = dev_login(t, args.login)
        hdr = {"Cookie": jar}
        print("  ورود توسعه انجام شد (%s) → پنل‌ها هم سنجیده می‌شوند." % args.login)

    probe = request_once(t, "/")
    if probe[0] in (0, 500):
        raise SystemExit("اپ در %s پاسخ نمی‌دهد (کد %s)." % (args.base, probe[0]))

    results, errs, wall = run(t, pages, args.threads, args.seconds, hdr)
    all_lat = [x for lat, _ in results for x in lat]
    codes = {}
    for _, c in results:
        for k, v in c.items():
            codes[k] = codes.get(k, 0) + v
    total = len(all_lat)
    bad = sum(v for k, v in codes.items() if k == "err" or (isinstance(k, int) and k >= 500))
    slow = sum(1 for x in all_lat if x > args.max_p95)
    p = {q: percentile(all_lat, q) for q in (50, 90, 95, 99)}
    report = {
        "base": args.base, "threads": args.threads, "seconds": round(wall, 2),
        "requests": total, "rps": round(total / wall, 1) if wall else 0,
        "avg_ms": round(statistics.fmean(all_lat), 1) if all_lat else 0,
        "p50": round(p[50], 1), "p90": round(p[90], 1), "p95": round(p[95], 1),
        "p99": round(p[99], 1), "max_ms": round(max(all_lat), 1) if all_lat else 0,
        "over_threshold": slow, "errors": bad,
        "status_codes": {str(k): v for k, v in codes.items()},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        width = 22
        print("\n  سنجهٔ بار نوبتین — %s" % args.base)
        print("  %-22s %s" % ("ترد / duration", "%s ترد در %s ثانیه"
                              % (fa(args.threads), fa(report["seconds"]))))
        for label, key in (("درخواست‌ها", "requests"), ("نرخ (RPS)", "rps"),
                           ("میانگین (ms)", "avg_ms"), ("p50 (ms)", "p50"),
                           ("p90 (ms)", "p90"), ("p95 (ms)", "p95"), ("p99 (ms)", "p99"),
                           ("بیشینه (ms)", "max_ms"), ("خطا (5xx/اتصال)", "errors"),
                           ("کندتر از آستانه", "over_threshold")):
            print("  %-22s %s" % (label, fa(report[key])))
        print("  کدهای وضعیت: " + ", ".join("%s:%s" % (k, v) for k, v in sorted(
            codes.items(), key=lambda x: str(x[0]))))
        if errs[:3]:
            print("  نمونهٔ خطا: " + " | ".join(errs[:3]))

    if bad:
        print("  ❌ پاسخ خطادار یا اتصال شکسته بود؛ لاگ سرور (NOBATIN_LOG_FILE) را ببینید.")
        sys.exit(1)
    if total == 0:
        print("  ❌ هیچ درخواستی انجام نشد؛ اپ روشن است؟")
        sys.exit(1)
    if report["p95"] > args.max_p95:
        print("  ⚠ p95 از %s ms بیشتر است؛ ترد/workerها را زیاد کنید (deploy/README) "
              "یا کش و ایندکس را بررسی کنید." % fa(args.max_p95))
        sys.exit(2)
    print("  ✅ زیر بار، پاسخ پایدار بود.")


if __name__ == "__main__":
    main()
