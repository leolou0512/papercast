// papercast-group: the Listening page (hub/listening.py): how much you listened, per day, as
// GitHub's contribution heatmap (the last 52 weeks, a column a week, Monday on top), your totals
// and streaks, your last 12 weeks as bars, and the group's heatmaps, most minutes in the last 30
// days first. The five shades come from the theme's accent (listening.css), so every theme has
// them. Hover or tap a day: its date and "N min · M episodes". app.js opens it (#listening) and
// gives it the page's api() and pictures; everything here is drawn as text or SVG, no HTML from
// the hub.
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const LEVEL_MIN = [10, 25, 50];            // minutes: under 10 is shade 1, under 25 shade 2, under 50 shade 3, else 4
  const phone = () => window.matchMedia("(max-width: 720px)").matches;

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c);
    return e;
  }
  function svg(tag, attrs, ...kids) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) e.setAttribute(k, String(v));
    for (const c of kids.flat()) if (c) e.append(c);
    return e;
  }

  // ---- days: "YYYY-MM-DD" strings, counted in UTC so no daylight saving shifts them
  const parse = (d) => new Date(`${d}T00:00:00Z`);
  const iso = (t) => t.toISOString().slice(0, 10);
  const plus = (d, n) => iso(new Date(parse(d).getTime() + n * 86400000));
  const dow = (d) => (parse(d).getUTCDay() + 6) % 7;          // Monday 0 .. Sunday 6
  const longDate = (d) => { const t = parse(d); return `${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]} ${t.getUTCFullYear()}`; };
  const shortDate = (d) => { const t = parse(d); return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`; };

  const mins = (s) => (s > 0 && s < 30 ? "<1 min" : `${Math.round((s || 0) / 60)} min`);
  function dur(s) {
    const m = Math.round((s || 0) / 60);
    if (s > 0 && s < 30) return "<1 min";
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `${h} h ${r} min` : `${h} h`;
  }
  const eps = (n) => `${n} episode${n === 1 ? "" : "s"}`;
  function level(v) {
    if (!v) return 0;
    const [s, e] = v, m = s / 60;
    if (s < 30) return e ? 1 : 0;
    return m < LEVEL_MIN[0] ? 1 : m < LEVEL_MIN[1] ? 2 : m < LEVEL_MIN[2] ? 3 : 4;
  }

  // ---- the heatmap: `weeks` columns ending with the one holding `today`
  // opts: labels (the months and Mon, Wed, Fri), cell and gap in the SVG's own units
  function heatmap(days, today, weeks, opts) {
    const o = Object.assign({ labels: true, cell: 11, gap: 3 }, opts || {});
    const pitch = o.cell + o.gap, left = o.labels ? 30 : 0, top = o.labels ? 16 : 0;
    const start = plus(today, -dow(today) - (weeks - 1) * 7);
    const w = left + weeks * pitch - o.gap, h = top + 7 * pitch - o.gap;
    const g = svg("svg", { class: `hm${o.labels ? "" : " small"}`, viewBox: `0 0 ${w} ${h}`, role: "img",
      "aria-label": o.label || "Listening per day" });
    for (let c = 0; c < weeks; c++) {
      for (let r = 0; r < 7; r++) {
        const d = plus(start, c * 7 + r);
        if (d > today) break;
        const v = days[d];
        g.append(svg("rect", { x: left + c * pitch, y: top + r * pitch, width: o.cell, height: o.cell, rx: 2,
          class: `lv${level(v)}`, "data-d": d, "data-s": v ? v[0] : 0, "data-e": v ? v[1] : 0 }));
      }
    }
    if (o.labels) {
      // a month's name over the first column whose Monday is in it (the first column's own
      // month only when the next is at least three columns on), none over the last column
      const month = (c) => parse(plus(start, c * 7)).getUTCMonth();
      const at = [];
      for (let c = 0; c < weeks - 1; c++) if (c === 0 || month(c) !== month(c - 1)) at.push(c);
      if (at.length > 1 && at[1] - at[0] < 3) at.shift();
      for (const c of at) g.append(svg("text", { x: left + c * pitch, y: 10, class: "hm-t" }, document.createTextNode(MONTHS[month(c)])));
    }
    if (o.labels) for (const r of [0, 2, 4]) g.append(svg("text", { x: 0, y: top + r * pitch + o.cell - 1.5, class: "hm-t" }, document.createTextNode(DOW[r])));
    return g;
  }

  function legend() {
    return el("div", { class: "hm-legend", "aria-hidden": "true" }, el("span", { text: "Less" }),
      [0, 1, 2, 3, 4].map((i) => el("span", { class: `hm-key lv${i}` })), el("span", { text: "More" }));
  }

  // ---- the 12 weeks as bars, minutes over each (narrower bars on a phone, so its text stays readable)
  function weekBars(weeks, narrow) {
    const n = weeks.length, bw = narrow ? 20 : 24, gap = narrow ? 9 : 14, H = 64, top = 16, bottom = 18;
    const W = n * (bw + gap) - gap, max = Math.max(1, ...weeks.map((x) => x.s));
    const g = svg("svg", { class: "wk", viewBox: `0 0 ${W} ${top + H + bottom}`, role: "img", "aria-label": "Minutes per week" });
    weeks.forEach((x, i) => {
      const hgt = x.s > 0 ? Math.max(2, (x.s / max) * H) : 0, xx = i * (bw + gap);
      g.append(svg("rect", { x: xx, y: top + H - 1, width: bw, height: 1, class: "wk-base" }));
      if (hgt) g.append(svg("rect", { x: xx, y: top + H - hgt, width: bw, height: hgt, rx: 3, class: "wk-bar", "data-w": x.start, "data-s": x.s, "data-e": x.episodes }));
      if (x.s > 0) g.append(svg("text", { x: xx + bw / 2, y: top + H - hgt - 4, class: "wk-n" }, document.createTextNode(String(Math.round(x.s / 60)))));
      if (i % 3 === 0 || i === n - 1) {
        // the first label starts at its bar and the last ends at its bar: neither leaves the chart
        const [tx, anchor] = i === 0 ? [xx, "start"] : i === n - 1 ? [xx + bw, "end"] : [xx + bw / 2, "middle"];
        g.append(svg("text", { x: tx, y: top + H + 13, class: "wk-t", "text-anchor": anchor }, document.createTextNode(shortDate(x.start))));
      }
    });
    return g;
  }

  function mount(ctx) {
    let tip = null, tipFor = null, gen = 0, wired = false;
    const zoom = () => document.documentElement.currentCSSZoom || 1;

    function hideTip() { if (tip) tip.hidden = true; tipFor = null; }
    function showTip(target) {
      if (!tip) { tip = el("div", { class: "hm-tip", role: "status", hidden: true }); document.body.append(tip); }
      const d = target.dataset.d, w = target.dataset.w;
      const s = Number(target.dataset.s) || 0, e = Number(target.dataset.e) || 0;
      tip.replaceChildren(el("b", { text: d ? longDate(d) : `Week of ${shortDate(w)}` }), el("span", { class: "t", text: `${mins(s)} · ${eps(e)}` }));
      tip.hidden = false;
      tipFor = target;
      const r = target.getBoundingClientRect(), z = zoom();
      const tw = tip.offsetWidth, th = tip.offsetHeight, vw = window.innerWidth / z;
      const cx = (r.left + r.width / 2) / z, topY = r.top / z;
      tip.style.left = `${Math.max(8, Math.min(vw - tw - 8, cx - tw / 2))}px`;
      tip.style.top = `${topY - th - 6 < 8 ? r.bottom / z + 6 : topY - th - 6}px`;
    }
    function wire(host) {
      if (wired) return;
      wired = true;
      const cellOf = (e) => (e.target && e.target.closest ? e.target.closest("rect[data-d], rect[data-w]") : null);
      host.addEventListener("pointerover", (e) => { if (e.pointerType === "mouse") { const c = cellOf(e); if (c) showTip(c); } });
      host.addEventListener("pointerout", (e) => { if (e.pointerType === "mouse" && cellOf(e) && !(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest("rect[data-d], rect[data-w]"))) hideTip(); });
      host.addEventListener("click", (e) => { const c = cellOf(e); if (c) showTip(c); else hideTip(); });
      document.addEventListener("pointerdown", (e) => { if (tipFor && !(e.target.closest && e.target.closest("rect[data-d], rect[data-w]"))) hideTip(); });
      host.addEventListener("scroll", hideTip, true);
      const win = document.getElementById("win");
      if (win) win.addEventListener("scroll", hideTip);
      window.matchMedia("(max-width: 720px)").addEventListener("change", () => { if (S.group && S.host && !S.host.closest("[hidden]")) redraw(); });
    }

    const S = { host: null, me: null, group: null };
    function stat(label, value, sub, id) {
      return el("div", { class: "ls-stat", id }, el("span", { class: "ls-l", text: label }),
        el("span", { class: "ls-v t", text: value }), el("span", { class: "ls-s t", text: sub || "" }));
    }
    function drawMe() {
      const j = S.me, t = j.totals, st = j.streak;
      const box = el("div", { class: "hm-box", id: "ls-year" }, heatmap(j.days, j.today, 53, { label: "Your listening, the last 52 weeks" }));
      const days = (n) => `${n} day${n === 1 ? "" : "s"}`;
      const mine = el("div", { class: "ls-me", id: "ls-me" },
        el("div", { class: "ls-stats" },
          stat("Today", dur(t.today.s), eps(t.today.episodes), "ls-today"),
          stat("This week", dur(t.week.s), eps(t.week.episodes), "ls-week"),
          stat("This month", dur(t.month.s), eps(t.month.episodes), "ls-month"),
          stat("All time", dur(t.all.s), eps(t.all.episodes), "ls-all"),
          stat("Streak", days(st.current), "", "ls-streak"),
          stat("Longest streak", days(st.longest), "", "ls-longest")),
        el("h3", { class: "sec-h", text: "Past year" }), box, legend(),
        el("h3", { class: "sec-h", text: "Weeks" }), el("div", { class: "wk-box", id: "ls-weeks" }, weekBars(j.weeks, phone())));
      return [mine, box];
    }
    function groupList() {
      const g = S.group, small = phone();
      const weeks = small ? 26 : 53;
      if (!g.people.length) return el("p", { class: "muted", id: "ls-nobody", text: "Nobody" });
      return el("ul", { class: "gp", id: "ls-group" }, g.people.map((p) => el("li", { class: "gp-row", "data-uid": String(p.user.id) },
        el("div", { class: "gp-who" }, ctx.avatar(p.user, "m"), el("span", { class: "gp-name", text: p.user.name || "?" }),
          el("span", { class: "gp-n t", text: dur(p.last30_s) })),
        el("div", { class: "gp-hm" }, heatmap(p.days, g.today, weeks, { labels: false, cell: 10, gap: 2, label: `${p.user.name}’s listening` })))));
    }
    // a phone turned, or a window narrowed past 720 px: the group's weeks and the bars change
    function redraw() {
      const old = document.getElementById("ls-group") || document.getElementById("ls-nobody");
      if (old) old.replaceWith(groupList());
      const wk = document.getElementById("ls-weeks");
      if (wk && S.me) wk.replaceChildren(weekBars(S.me.weeks, phone()));
    }
    async function render(host) {
      const my = ++gen;
      S.host = host;
      wire(host);
      hideTip();
      if (!host.firstChild) host.append(el("p", { class: "muted", id: "ls-loading", text: "Loading…" }));
      const tz = new Date().getTimezoneOffset();
      let me, group;
      try {
        [me, group] = await Promise.all([ctx.api("GET", `/api/listening/me?tz=${tz}`), ctx.api("GET", `/api/listening/group?tz=${tz}`)]);
      } catch (e) {
        if (my === gen) host.replaceChildren(el("p", { class: "err", text: e.message }));
        return;
      }
      if (my !== gen) return;
      S.me = me; S.group = group;
      const [mine, box] = drawMe();
      host.replaceChildren(mine,
        el("div", { class: "gp-head" }, el("h3", { class: "sec-h", text: "Group" }), el("span", { class: "gp-cap", text: "30 days" })),
        groupList());
      box.scrollLeft = box.scrollWidth;        // a phone's box scrolls: the recent weeks first
    }
    return { render, hide: hideTip };
  }

  window.PaperListening = { mount, heatmap, level };
})();
