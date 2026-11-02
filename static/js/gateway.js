// درگاه پرداخت: شمارش معکوسِ مهلت قفل ساعت.
// زمانِ باقی‌مانده از data-left روی خودِ element خوانده می‌شود (تعداد دقیقه) و
// مقصد محاسبه بر پایهٔ ساعت سیستم است، نه کم‌کردن یک‌به‌یک — پس اگر تبخیه‌ای
// Sleep/background شد، دوباره درست می‌شود.
// وقتی مهلت تمام می‌شود، دکمه‌ها غیرفعال و پیام روشن می‌شود: پرداخت پس از انقضا
// در سروِر هم رد می‌شود و نوبتی قطعی نمی‌شود.
(function () {
  "use strict";

  var el = document.getElementById("cd");
  if (!el) { return; }

  function fa(s) {
    return String(s).replace(/[0-9]/g, function (d) { return "۰۱۲۳۴۵۶۷۸۹"[d]; });
  }

  var minutes = parseInt(el.getAttribute("data-left") || "0", 10);
  var deadline = Date.now() + Math.max(0, minutes) * 60000;
  var out = document.getElementById("expired");
  var form = document.getElementById("payform");

  function stop() {
    clearInterval(timer);
    el.textContent = "زمان قفل تمام شد";
    el.style.color = "#a4234a";
    if (out) { out.hidden = false; }
    if (form) {
      Array.prototype.forEach.call(form.querySelectorAll("button"), function (b) {
        b.disabled = true;
      });
    }
  }

  function tick() {
    var left = Math.round((deadline - Date.now()) / 1000);
    if (left <= 0) { stop(); return; }
    el.textContent = fa(Math.ceil(left / 60)) + " دقیقه";
    if (left <= 60) { el.style.color = "#a4234a"; }
  }

  var timer = setInterval(tick, 10000);
  tick();
})();
