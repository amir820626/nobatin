// صفحهٔ رزرو: انتخاب ساعت از تقویم و تکمیل کارت بلیت.
// هیچ داده‌ای درون این فایل نیست؛ مدت سرویس از data-duration روی فرم خوانده می‌شود،
// پس سیاست امنیتی «اسکریپت فقط از خودِ سایت» بدون <script> درون‌خطی ممکن می‌ماند.
// بدون جاوااسکریپت هم فرم کار می‌کند: دکمهٔ «ادامه» فعال است و اگر ساعتی انتخاب
// نشده باشد، سمت سروِر پیام «یک ساعت را انتخاب کنید» را می‌دهد.
(function () {
  "use strict";

  function fa(s) {
    return String(s).replace(/[0-9]/g, function (d) { return "۰۱۲۳۴۵۶۷۸۹"[d]; });
  }
  function toMin(t) { var p = String(t).split(":"); return (+p[0]) * 60 + (+p[1]); }
  function toHm(m) {
    var h = Math.floor(m / 60) % 24, mm = m % 60;
    return (h < 10 ? "0" : "") + h + ":" + (mm < 10 ? "0" : "") + mm;
  }

  var form = document.getElementById("pickform");
  if (!form) { return; }

  var dur = parseInt(form.getAttribute("data-duration") || "0", 10);
  var picked = document.getElementById("picked");
  var clock = document.getElementById("bigclock");
  var endT = document.getElementById("endtime");
  var btn = document.getElementById("cont");
  var hint = document.getElementById("pickhint");

  function paint(hm) {
    if (clock) { clock.textContent = fa(hm); }
    if (endT) { endT.textContent = dur ? fa(toHm(toMin(hm) + dur)) : "—"; }
    if (hint) { hint.hidden = true; }
  }

  Array.prototype.forEach.call(document.querySelectorAll(".slot"), function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(document.querySelectorAll(".slot.on"), function (x) {
        x.classList.remove("on");
      });
      b.classList.add("on");
      var hm = b.getAttribute("data-hm");
      picked.value = hm;
      paint(hm);
      if (btn) { btn.disabled = false; }
    });
  });

  // اگر ساعتی از قبل انتخاب/تکمیل شده (بازگشت از ورود)، کارت را از همان ابتدا پر کن
  if (picked && picked.value) {
    var pre = document.querySelector('.slot[data-hm="' + picked.value + '"]');
    if (pre) { pre.classList.add("on"); }
    paint(picked.value);
    if (btn) { btn.disabled = false; }
  }

  form.addEventListener("submit", function (e) {
    if (!picked || !picked.value) {
      e.preventDefault();
      if (hint) {
        hint.hidden = false;
        hint.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      var first = document.querySelector(".slot:not([disabled])");
      if (first) { first.focus(); }
    }
  });
})();
