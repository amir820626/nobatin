// رفتارهای کوچک مشترک نوبتین — همه با خصیصه‌های data-*، تا هیچ <script> یا onclick
// درون‌خطی در قالب‌ها نماند و سیاست امنیتی «اسکریپت فقط از خودِ سایت» قابل اجرا باشد.
// هیچ‌کدام لازم نیستند: بدون جاوااسکریپت هم فرم‌ها ارسال می‌شوند و ذخیره می‌گردند؛
// این‌ها فقط تجربه را راحت‌تر می‌کنند (پیش‌نمایش رنگ، تأیید حذف، ردیف قانون تازه…).
(function () {
  "use strict";

  function all(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  // ۱) تأیید قبل از ارسال فرم یا کلیک روی دکمهٔ خطرناک
  function confirmText(el) { return el.getAttribute("data-confirm"); }

  document.addEventListener("submit", function (e) {
    var f = e.target.closest("form[data-confirm]");
    if (f && confirmText(f) && !window.confirm(confirmText(f))) { e.preventDefault(); return; }
  }, true);

  document.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-confirm], a[data-confirm]");
    if (b && confirmText(b) && !window.confirm(confirmText(b))) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);

  // ۲) ارسال خودکار فرم فیلترها هنگام تغییر گزینه (بدون onchange درون‌خطی)
  document.addEventListener("change", function (e) {
    var s = e.target.closest("select[data-autosubmit]");
    if (s && s.form) { s.form.submit(); }
  });

  // ۳) آب‌وهوای «استان ← شهر» در فرم ثبت کسب‌وکار (option‌ها با data-p)
  function wireCity(form) {
    var prov = form.querySelector("[name=province]"), city = form.querySelector("#city");
    if (!prov || !city) { return; }
    function apply() {
      var p = prov.value;
      all(city.options).forEach(function (o) {
        var ok = !p || String(o.dataset.p || "") === p;
        o.hidden = !ok;
        o.disabled = !ok;
      });
      if (city.selectedIndex >= 0 && city.options[city.selectedIndex].disabled) { city.value = ""; }
    }
    prov.addEventListener("change", apply);
    apply();
  }
  all("form[data-city-cascade]").forEach(wireCity);

  // ۴) نمایش/پنهان یک بخش بر اساس مقدار select (مثلاً مبلغ پیش‌پرداخت)
  function wireToggle(sel) {
    var target = document.querySelector(sel.getAttribute("data-toggle-target"));
    if (!target) { return; }
    var want = sel.getAttribute("data-toggle-value") || "deposit";
    function apply() {
      var show = sel.value === want;
      target.hidden = !show;
      target.style.display = show ? "" : "none";
    }
    sel.addEventListener("change", apply);
    apply();
  }
  all("select[data-toggle-target]").forEach(wireToggle);

  // ۵) لغزندهٔ رنگ برند: پیش‌نمایش و متن زنده
  all("input[type=range][data-live-var], input[type=range][data-live-text]").forEach(function (r) {
    var vTarget = r.getAttribute("data-live-var") ? document.querySelector(r.getAttribute("data-live-var")) : null;
    var tTarget = r.getAttribute("data-live-text") ? document.querySelector(r.getAttribute("data-live-text")) : null;
    function apply() {
      if (vTarget) { vTarget.style.setProperty("--biz-hue", r.value); }
      if (tTarget) { tTarget.textContent = r.value; }
    }
    r.addEventListener("input", apply);
    apply();
  });

  // ۶) ردیف‌های قانون کنسلی: حذف، افزودن و هم‌نام‌سازی فیلد درصد
  function syncRule(row) {
    var hr = row.querySelector("[name=rule_hours]"), pc = row.querySelector("[data-refund]");
    if (hr && pc) {
      var v = String(hr.value || "0").replace(/[^0-9]/g, "") || "0";
      pc.name = "refund_" + v;
    }
  }
  document.addEventListener("input", function (e) {
    if (e.target.matches("input[name=rule_hours]")) {
      var row = e.target.closest(".rule");
      if (row) { syncRule(row); }
    }
  });
  document.addEventListener("click", function (e) {
    var rm = e.target.closest("button[data-rule-remove]");
    if (rm) {
      var row = rm.closest(".rule");
      if (row) { row.remove(); }
      return;
    }
    var add = e.target.closest("button[data-rule-add]");
    if (!add) { return; }
    var box = document.querySelector(add.getAttribute("data-rule-add"));
    var tpl = document.querySelector("template[data-rule-tpl]");
    if (!box || !tpl) { return; }
    var node = tpl.content.firstElementChild.cloneNode(true);
    box.appendChild(node);
    syncRule(node);
  });
  all(".rule").forEach(syncRule);
})();
