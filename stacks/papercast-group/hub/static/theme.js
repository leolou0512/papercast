// papercast-group: light or dark, chosen in this browser ("pcg-theme": light | dark; none: the
// system's). Loaded in <head> without defer, so a page is drawn in the chosen theme from the
// start. The CSS already follows :root[data-theme]; a change fires "pcg-theme" on document
// (the map redraws its colours on it) and reaches the other tabs through "storage".
(function () {
  "use strict";
  var KEY = "pcg-theme", root = document.documentElement;
  function saved() {
    try { var v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : ""; } catch (e) { return ""; }
  }
  function apply(v) {
    if (v) root.setAttribute("data-theme", v); else root.removeAttribute("data-theme");
    document.dispatchEvent(new CustomEvent("pcg-theme", { detail: effective() }));
  }
  function effective() {
    var v = root.getAttribute("data-theme");
    return v === "light" || v === "dark" ? v : (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function set(v) {
    v = v === "light" || v === "dark" ? v : "";
    try { if (v) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY); } catch (e) { /* private window: this page only */ }
    apply(v);
  }
  apply(saved());
  window.addEventListener("storage", function (e) { if (e.key === KEY) apply(saved()); });
  if (window.matchMedia) {
    var mq = matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", function () { if (!saved()) apply(""); });
  }
  window.pcgTheme = { saved: saved, set: set, effective: effective,
    toggle: function () { set(effective() === "dark" ? "light" : "dark"); } };
})();
