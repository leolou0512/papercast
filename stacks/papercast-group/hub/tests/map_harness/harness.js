/* The test page for test_map.py: mounts the map as the group's page (app.js, A4) will, with the
   papers from the (fake) library and the live events through opts.subscribe. The query string
   picks the rest: ?webgl=0 (2D) or ?webgl=software, ?sub=0 (no events: the map refetches on
   show()), ?editable=0. window.H lets the test send events and see Open and Close. */
(function () {
  "use strict";
  var q = new URLSearchParams(location.search), subs = {};
  var H = window.H = {
    opened: [], closed: 0,
    emit: function (kind, data) { (subs[kind] || []).forEach(function (fn) { fn(data); }); },
    subscribed: function () { return Object.keys(subs).sort(); }
  };
  Promise.all([fetch("/api/me").then(function (r) { return r.json(); }), fetch("/api/library").then(function (r) { return r.json(); })]).then(function (res) {
    var me = res[0], lib = res[1];
    H.papers = new Map(lib.papers.map(function (p) { return [p.id, p]; }));
    var opts = {
      api: "", graphs: true, editable: q.get("editable") !== "0", me: { id: me.id, name: me.name, role: me.role }, papers: H.papers, title: "Map",
      onOpen: function (id) { H.opened.push(id); }, onClose: function () { H.closed++; }
    };
    if (q.get("sub") !== "0") opts.subscribe = function (kind, fn) { (subs[kind] = subs[kind] || []).push(fn); };
    if (q.has("webgl")) opts.webgl = q.get("webgl") === "0" ? false : q.get("webgl");
    H.map = window.PaperMap.mount(document.getElementById("map"), opts);
  });
})();
