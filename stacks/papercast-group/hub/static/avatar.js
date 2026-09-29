// papercast-group: profile pictures next to names (hub/avatars.py). window.PcgAvatar draws a
// person's small round picture, or their initials on a coloured circle when they have none;
// app.js and social.js use it. What this page knows of everyone's picture comes from
// /api/config (`avatars`, the whole list) and then from the `avatar` event, and every circle it
// drew carries the person's id, so a new picture redraws each copy of it on the page in place.
// The initials are drawn by CSS from data-i, not as text, so a line reads the same to a script,
// a test or a screen reader with or without its circle (which is aria-hidden: the name says who).
"use strict";
(() => {
  const known = new Map();      // user id (a string) -> version, or null for none
  let complete = false;         // known holds everyone: someone missing from it has no picture
  const COLOURS = 8;
  const key = (id) => (id === null || id === undefined ? "" : String(id));

  function initials(name) {
    const words = String(name || "").trim().split(/\s+/).filter(Boolean);
    if (!words.length) return "?";
    const first = (w) => { for (const ch of w) if (/\p{L}|\p{N}/u.test(ch)) return ch; return ""; };
    const a = first(words[0]), b = words.length > 1 ? first(words[words.length - 1]) : "";
    return ((a + b) || "?").toUpperCase();
  }
  function colour(u) {
    const s = key(u && u.id) || String((u && u.name) || "");
    let h = 0;
    for (const ch of s) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return h % COLOURS;
  }
  // The picture's version: what the page last heard, else what the object itself says.
  function version(u) {
    if (!u) return null;
    const k = key(u.id);
    if (k && known.has(k)) return known.get(k);
    if (k && complete) return null;
    return u.avatar || null;
  }
  const url = (id, v) => `/api/avatars/${encodeURIComponent(key(id))}.jpg?v=${encodeURIComponent(v)}`;

  function draw(n, v) {
    const img = n.firstElementChild;
    if (v && img && img.dataset.v === v) return;
    if (!v || !n.dataset.uid) { n.replaceChildren(); n.classList.remove("av-pic"); return; }
    const x = document.createElement("img");
    x.alt = ""; x.decoding = "async"; x.draggable = false; x.dataset.v = v;
    // gone meanwhile (removed, or an old version): the initials again
    x.addEventListener("error", () => { if (x.parentNode === n) { x.remove(); n.classList.remove("av-pic"); } });
    x.src = url(n.dataset.uid, v);
    n.replaceChildren(x);
    n.classList.add("av-pic");
  }
  // A circle for user u ({id, name, avatar?}). size: "s" (20 px, lists), "m" (24 px, comments),
  // "l" (88 px, Settings).
  function node(u, size) {
    const n = document.createElement("span");
    n.className = `av av-${size || "s"} av-c${colour(u)}`;
    n.setAttribute("aria-hidden", "true");
    const k = key(u && u.id);
    if (k) n.dataset.uid = k;
    n.dataset.i = initials(u && u.name);
    draw(n, version(u));
    return n;
  }
  // Someone's picture changed (the `avatar` event, or this person's own upload).
  function set(id, v) {
    const k = key(id);
    if (!k) return;
    known.set(k, v || null);
    for (const n of document.querySelectorAll(`.av[data-uid="${CSS.escape(k)}"]`)) draw(n, v || null);
  }
  // Everyone's pictures ({user id: version}, /api/config's `avatars`): the whole list, so the
  // circles already drawn are brought up to date too.
  function seed(all) {
    known.clear();
    for (const [k, v] of Object.entries(all || {})) if (v) known.set(key(k), v);
    complete = true;
    for (const n of document.querySelectorAll(".av[data-uid]")) draw(n, known.get(n.dataset.uid) || null);
  }

  window.PcgAvatar = { node, set, seed, version, initials, url };
})();
