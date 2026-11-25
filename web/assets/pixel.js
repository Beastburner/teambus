/* TeamBus pixel kit: icons, agent sprites, nav, footer credits and the hero scene. Original art, drawn in code (no image files).
   Theme inspired by a pixel-art gallery portfolio on Behance (see the footer credit). */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const ICONS = {
    rocket: ["....#....", "...###...", "...#.#...", "..#####..", "..#.#.#..", "..#####..", ".#######.", ".#.###.#.", "##.###.##", "...###...", "...#.#...", "..#...#.."],
    invader: ["..#.....#..", "...#...#...", "..#######..", ".##.###.##.", "###########", "#.#######.#", "#.#.....#.#", "...##.##..."],
    coffee: ["..#..#.....", "...#..#....", "...........", "#########..", "#.......###", "#.......#.#", "#.......###", ".#######...", "..#####...."],
    wheat: ["....#....", "...###...", "..#.#.#..", "....#....", "..#.#.#..", ".##.#.##.", "....#....", "..#.#.#..", ".##.#.##.", "....#....", "....#....", "....#...."],
    target: ["...####...", ".##....##.", ".#.####.#.", "#.#....#.#", "#.#.##.#.#", "#.#.##.#.#", "#.#....#.#", ".#.####.#.", ".##....##.", "...####..."],
    chat: ["###########", "#.........#", "#.#.#.#...#", "#.........#", "###########", "..###......", "...#......."],
    bolt: ["....##.", "...##..", "..##...", ".#####.", "...##..", "..##...", ".##....", "##....."],
    flag: ["#####...", "######..", "#######.", "######..", "#####...", "#.......", "#.......", "#.......", "#.......", "#......."],
    check: ["........#", ".......##", "#.....##.", "##...##..", ".##.##...", "..###....", "...#....."],
    cross: ["#.....#", ".#...#.", "..#.#..", "...#...", "..#.#..", ".#...#.", "#.....#"],
    heart: [".##...##.", "####.####", "#########", "#########", ".#######.", "..#####..", "...###...", "....#...."],
    eye: ["...#####...", ".##.....##.", "#...###...#", "#..#####..#", "#...###...#", ".##.....##.", "...#####..."],
    hammer: ["..#####..", ".#######.", ".#######.", "..#####..", "....#....", "....#....", "....#....", "....#....", "....#...."],
  };
  const svgEl = (w, h, scale, cls) => { const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", `0 0 ${w} ${h}`); s.setAttribute("width", w * scale); s.setAttribute("height", h * scale); s.setAttribute("class", cls || "px"); s.setAttribute("aria-hidden", "true"); return s; };
  function bitmap(rows, scale, cls) {          // one <path> of horizontal runs: small and crisp at any scale
    const h = rows.length, w = rows[0].length, s = svgEl(w, h, scale, cls); let d = "";
    rows.forEach((r, y) => { for (const m of r.matchAll(/#+/g)) d += `M${m.index} ${y}h${m[0].length}v1h-${m[0].length}z`; });
    const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); p.setAttribute("fill", "currentColor"); s.append(p); return s;
  }
  const icon = (name, scale = 3, cls) => bitmap(ICONS[name] || ICONS.invader, scale, cls);
  const hash = (str) => { let h = 2166136261; for (const c of String(str)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };
  function spriteRows(seed) {                  // a small symmetric critter, the same one for the same name every time
    let h = hash(seed) || 1; const rnd = () => ((h = Math.imul(h ^ (h >>> 15), 2246822519) >>> 0), (h & 0xffff) / 0xffff), rows = [];
    for (let y = 0; y < 7; y++) { const half = []; for (let x = 0; x < 4; x++) half.push(rnd() < (y === 0 ? .3 : y === 6 ? .45 : .62) || (x === 3 && y > 0 && y < 6) ? "#" : "."); rows.push(half.join("") + half.slice(0, 3).reverse().join("")); }
    rows[2] = rows[2].replace(/^(.)#/, "$1.").replace(/#(.)$/, ".$1");                 // two eyes, cut out of the head
    return rows;
  }
  const pathOf = (rows) => { let d = ""; rows.forEach((r, y) => { for (const m of r.matchAll(/#+/g)) d += `M${m.index} ${y}h${m[0].length}v1h-${m[0].length}z`; }); return d; };
  const spriteData = (seed) => { const r = spriteRows(seed); return { d: pathOf(r), w: r[0].length, h: r.length }; };
  const sprite = (seed, scale = 4) => bitmap(spriteRows(seed), scale, "px sprite");
  function nav(active) {
    const q = new URLSearchParams(location.search), keep = ["token", "invite"].map((k) => (q.get(k) ? k + "=" + encodeURIComponent(q.get(k)) : "")).filter(Boolean).join("&"), qs = keep ? "?" + keep : "";
    const link = (key, label, href) => { const a = document.createElement("a"); a.textContent = label; a.href = href; if (key === active) a.setAttribute("aria-current", "page"); return a; };
    const host = document.getElementById("nav"); if (!host) return;
    const logo = document.createElement("span"); logo.className = "logo"; logo.append(icon("rocket", 2));
    const pill = document.createElement("nav"); pill.className = "pill"; pill.setAttribute("aria-label", "TeamBus");
    if (active === "sandbox") pill.append(link("sandbox", "sandbox", "./"), logo, link("github", "source", "https://github.com/Beastburner/teambus"));
    else pill.append(link("team", "team", "./" + qs), link("brain", "brain", "brain" + qs), logo, link("memory", "memory", "memory" + qs), link("talk", "talk", "conversations" + qs));
    host.className = "topbar"; host.replaceChildren(pill);
  }
  function foot(extra) {
    const f = document.createElement("footer"); f.className = "foot";
    const a = (t, h) => { const x = document.createElement("a"); x.textContent = t; x.href = h; x.target = "_blank"; x.rel = "noopener noreferrer"; return x; };
    const l = document.createElement("span"); l.append("TeamBus is open source · ", a("source", "https://github.com/Beastburner/teambus")); if (extra) l.append(" · " + extra);
    const r = document.createElement("span"); r.append("pixel theme inspired by ", a("Creative Portfolio Website Design", "https://www.behance.net/gallery/226623781/Creative-Portfolio-Website-Design/"), " on Behance · fonts Pixelify Sans, Space Grotesk, Geist Mono (OFL)");
    f.append(l, r); const st = document.querySelector(".stage"); if (st) st.append(f);
  }
  /* The hero: a rainy neon block of flats, drawn at 224x104 and scaled up crisp. Seeded, so it looks the same every visit; only rain and a few windows move. */
  function scene(canvas) {
    const W = 224, H = 104; canvas.width = W; canvas.height = H; canvas.classList.add("scene"); const g = canvas.getContext("2d");
    let s = 7; const rnd = () => ((s = Math.imul(s ^ (s >>> 15), 2246822519) >>> 0), (s >>> 8) / 16777216), pick = (a) => a[Math.floor(rnd() * a.length)];
    const bg = document.createElement("canvas"); bg.width = W; bg.height = H; const b = bg.getContext("2d"), R = (c, x, y, w, h, a = 1) => { b.globalAlpha = a; b.fillStyle = c; b.fillRect(x | 0, y | 0, w | 0, h | 0); b.globalAlpha = 1; };
    const wins = [];
    for (let y = 0; y < H; y += 4) R(["#0b1411", "#0e1a16", "#122019", "#172a21", "#1d3329"][Math.min(4, Math.floor(y / 22))], 0, y, W, 4);                      // banded sky
    const tower = (x, w, top, body, lit) => { R(body, x, top, w, H - top); for (let y = top + 4; y < H - 30; y += 5) for (let xx = x + 3; xx < x + w - 3; xx += 5) if (rnd() < lit) { const c = pick(["#e0a43a", "#e0a43a", "#4fe3ff", "#c4572b"]); wins.push({ x: xx, y, c, on: rnd() < .8 }); } };
    for (let x = -4; x < W; ) { const w = 14 + Math.floor(rnd() * 20), top = 28 + Math.floor(rnd() * 34); tower(x, w, top, "#16231e", .16); x += w + 1; }                                    // far
    for (const [x, w, top] of [[-6, 48, 18], [150, 82, 10]]) { tower(x, w, top, "#0e1814", .2); for (let k = 0; k < 4; k++) { R("#27362f", x + 6 + k * 3, top + 12 + k * 12, 1, 11); for (let i = 0; i < 11; i++) R("#27362f", x + 6 + k * 3 + (k % 2 ? i : -i) * 0 + (i % 2), top + 12 + k * 12 + i, 3, 1); R("#27362f", x + 4, top + 22 + k * 12, w - 8, 1); } }   // fire escapes
    R("#e0a43a", 58, 54, 70, 5, .55); for (let x = 58; x < 128; x += 2) R("#0a100e", x, 54 + (x % 4 ? 1 : 3), 1, 1, .5);                                                             // the lit band
    R("#0a100e", 0, H - 22, W, 22); for (let x = 2; x < W; x += 6) R("#1a2622", x, H - 28, 1, 8); R("#1a2622", 0, H - 28, W, 1);                                                    // balcony + railing
    for (const [x, u] of [[78, 0], [92, 1], [118, 0], [131, 0]]) { R("#050908", x, H - 36, 3, 3); R("#050908", x - 1, H - 33, 5, 9); if (u) { R("#050908", x - 3, H - 40, 9, 1); R("#050908", x - 1, H - 41, 5, 1); } }   // people
    R("#4fe3ff", 94, 62, 1, 1, .0);
    const neon = [[101, 60, 6, 30], [112, 66, 4, 18]]; for (const [x, y, w, h] of neon) { R("#4fe3ff", x - 2, y - 2, w + 4, h + 4, .10); R("#4fe3ff", x, y, w, h, .9); for (let i = 3; i < h - 3; i += 5) R("#0b1411", x + 1, y + i, w - 2, 1); }   // vertical neon signs
    R("#c4572b", 160, 70, 14, 3, .9); R("#c4572b", 158, 68, 18, 7, .12); R("#e0a43a", 30, 48, 9, 3, .9); R("#e0a43a", 28, 46, 13, 7, .12);                                             // small signs
    for (let y = 0; y < 8; y++) { R("#000", 0, y, W, 1, .3 - y * .035); } for (let x = 0; x < 12; x++) { R("#000", x, 0, 1, H, .22 - x * .018); R("#000", W - 1 - x, 0, 1, H, .22 - x * .018); }   // vignette
    const drops = Array.from({ length: 110 }, () => ({ x: rnd() * W, y: rnd() * H, v: 1.4 + rnd() * 1.6 })); let t0 = 0, last = 0;
    const RM = matchMedia("(prefers-reduced-motion:reduce)").matches;
    function draw(t) {
      g.drawImage(bg, 0, 0);
      for (const w of wins) { if (rnd() < .002) w.on = !w.on; g.globalAlpha = w.on ? .9 : .12; g.fillStyle = w.c; g.fillRect(w.x, w.y, 2, 2); } g.globalAlpha = 1;
      const pulse = .5 + .5 * Math.sin(t / 700); g.globalAlpha = .06 + .06 * pulse; g.fillStyle = "#4fe3ff"; g.fillRect(96, 56, 22, 44); g.globalAlpha = 1;
      g.fillStyle = "rgba(200,225,215,.5)"; for (const d of drops) { g.fillRect(d.x | 0, d.y | 0, 1, 3); if (!RM) { d.y += d.v; d.x -= .35; if (d.y > H) { d.y = -3; d.x = rnd() * (W + 20); } } }
      g.fillStyle = "rgba(0,0,0,.13)"; for (let y = 0; y < H; y += 2) g.fillRect(0, y, W, 1);                                                                                       // scanlines
    }
    draw(0); if (RM) return;
    (function loop(t) { if (document.hidden || t - last < 55) { requestAnimationFrame(loop); return; } last = t; draw(t); requestAnimationFrame(loop); })(0);
  }
  window.px = { icon, sprite, spriteData, nav, foot, scene, hash };
})();
