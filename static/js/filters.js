// فیلترهای آب‌وهواییِ «استان ← شهر ← محله» برای هر فرمی که data-cascade دارد.
// گزینه‌ها با data-prov و data-city برچسب می‌خورند؛ انتخابِ خارج از محدوده
// پاک می‌شود تا کاربر ترکیب مسخرهٔ «تهران + کاشان» را ارسال نکند.
// بدون جاوااسکریپت هم همه‌چیز کار می‌کند؛ فقط گزینه‌ها فیلتر دیده نمی‌شوند.
(function () {
  "use strict";

  function opts(sel) { return Array.prototype.slice.call(sel.options); }

  function scope(sel, keep) {
    if (!sel) { return; }
    opts(sel).forEach(function (o) {
      var ok = !o.dataset.prov || keep(o.dataset);
      o.hidden = !ok;
      o.disabled = !ok;
    });
  }

  function dropHidden(sel) {
    if (sel && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex].disabled) { sel.value = ""; }
  }

  function wire(form) {
    var prov = form.querySelector("[name=prov]"),
        city = form.querySelector("[name=city]"),
        dist = form.querySelector("[name=district]");
    if (!prov || !city) { return; }

    function apply() {
      var p = prov.value;
      scope(city, function (d) { return !p || d.prov === p; });
      dropHidden(city);
      var c = city.value;
      scope(dist, function (d) {
        return (!p || d.prov === p) && (!c || d.city === c);
      });
      dropHidden(dist);
    }

    form.addEventListener("submit", apply);
    prov.addEventListener("change", apply);
    city.addEventListener("change", apply);
    apply();
  }

  function run() {
    Array.prototype.forEach.call(document.querySelectorAll("form[data-cascade]"), wire);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
