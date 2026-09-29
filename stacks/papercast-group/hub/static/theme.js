// papercast-group: how the site looks in this browser, applied in <head> (loaded without defer)
// so a page is drawn that way from the first paint.
// - The theme, "pcg-theme": light | dark | paper | latte | dimmed | forest (the last four in
//   themes.css); none: the system's light or dark. :root[data-theme] carries it and
//   :root[data-scheme] its family, light or dark (the sign-in page's logo and its sun or moon).
//   A change fires "pcg-theme" on document with the family (the map redraws its colours on it).
// - The size, "pcg-size": 100 | 125 | 150 (per cent); none: 125 on a computer or tablet, 100 on
//   a phone. CSS zoom on :root, as if the browser were zoomed, in browsers with standard CSS zoom
//   (Element.currentCSSZoom: Chrome 128, Firefox 126; elsewhere 100%). Capped so the page keeps
//   the width its layout needs (721 CSS px for the two panes, 340 for the phone's one, whose
//   player needs 326): a narrow window gets less. --pcg-zoom holds the zoom for CSS sized by
//   the viewport (calc(100dvh / var(--pcg-zoom, 1))); scripts divide getBoundingClientRect and
//   pointer coordinates (screen px) by it to get CSS px. A change fires "pcg-size" with the zoom.
// Both reach the other tabs through "storage".
(function () {
  "use strict";
  var KEY = "pcg-theme", SKEY = "pcg-size", root = document.documentElement;
  // its name in Settings, dark or not, and the theme the sign-in page's button switches to
  var THEMES = {
    light: { label: "Light", dark: false, other: "dark" },
    paper: { label: "Paper", dark: false, other: "forest" },
    latte: { label: "Latte", dark: false, other: "dimmed" },
    dark: { label: "Dark", dark: true, other: "light" },
    dimmed: { label: "Dimmed", dark: true, other: "latte" },
    forest: { label: "Forest", dark: true, other: "paper" },
  };
  var SIZES = [100, 125, 150];
  var CAN_ZOOM = "currentCSSZoom" in root;
  var zoomNow = 1;

  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function put(k, v) {
    try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) { /* private window: this page only */ }
  }
  function fire(name, detail) { document.dispatchEvent(new CustomEvent(name, { detail: detail })); }
  function systemDark() { return !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches); }

  /* ---------- the theme ---------- */
  function known(v) { return typeof v === "string" && Object.prototype.hasOwnProperty.call(THEMES, v); }
  function saved() { var v = get(KEY); return known(v) ? v : ""; }
  // the theme drawn now: the chosen one, else the system's light or dark
  function current() { var v = root.getAttribute("data-theme"); return known(v) ? v : (systemDark() ? "dark" : "light"); }
  function effective() { return THEMES[current()].dark ? "dark" : "light"; }
  function apply(v) {
    if (v) root.setAttribute("data-theme", v); else root.removeAttribute("data-theme");
    root.setAttribute("data-scheme", effective());
    fire("pcg-theme", effective());
  }
  function set(v) {
    v = known(v) ? v : "";
    put(KEY, v);
    apply(v);
  }

  /* ---------- the size ---------- */
  function savedSize() { var v = Number(get(SKEY)); return SIZES.indexOf(v) >= 0 ? v : 0; }
  function phone() { return Math.min(screen.width || 0, screen.height || 0) < 600; }
  function defaultSize() { return phone() ? 100 : 125; }
  function size() { return savedSize() || defaultSize(); }
  function fits(z) {
    var w = window.innerWidth || 0;
    return w ? Math.max(1, Math.min(z, w / (w > 720 ? 721 : 340))) : z;
  }
  function applySize() {
    var z = CAN_ZOOM ? Math.floor(fits(size() / 100) * 1000) / 1000 : 1;
    if (z === zoomNow) return;
    zoomNow = z;
    if (z === 1) { root.style.removeProperty("zoom"); root.style.removeProperty("--pcg-zoom"); }
    else { root.style.setProperty("zoom", String(z)); root.style.setProperty("--pcg-zoom", String(z)); }
    fire("pcg-size", z);
  }
  function setSize(v) {
    v = Number(v);
    put(SKEY, SIZES.indexOf(v) >= 0 ? String(v) : "");
    applySize();
  }

  apply(saved());
  applySize();
  window.addEventListener("storage", function (e) {
    if (e.key === KEY || e.key === null) apply(saved());
    if (e.key === SKEY || e.key === null) applySize();
  });
  window.addEventListener("resize", applySize);
  if (window.matchMedia) {
    var mq = matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", function () { if (!saved()) apply(""); });
  }
  window.pcgTheme = {
    saved: saved, set: set, current: current, effective: effective,
    // the sign-in page's button: the other family's counterpart of the theme drawn now
    toggle: function () { set(THEMES[current()].other); },
    themes: Object.keys(THEMES).map(function (k) { return { v: k, label: THEMES[k].label, dark: THEMES[k].dark }; }),
    sizes: SIZES.slice(), savedSize: savedSize, size: size, defaultSize: defaultSize, setSize: setSize,
    zoom: function () { return zoomNow; },
  };
})();
