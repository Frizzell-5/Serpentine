// Plate-reader import: a 96/384-well grid of readings plus an optional plate map become an analysis-ready table.
// Uses the app's globals ($, esc, state, newDataset, renderData, autosave, toast).
"use strict";
const Plate = (() => {
  const ROWS = "ABCDEFGHIJKLMNOP";
  let view = { data: "", map: "", out: "long", value: "Signal", start: "" };

  const splitLine = (line) => {
    if (line.includes("\t")) return line.split("\t");
    if (line.includes(";")) return line.split(";");
    if (line.includes(",") && !/\d,\d{1,2}(\D|$)/.test(line.replace(/,(?=\s)/g, ""))) return line.split(",");
    return line.trim().split(/\s+/);
  };
  const num = (s) => {
    const t = String(s ?? "").trim().replace(/\s/g, "");
    if (!t) return NaN;
    const v = Number(/^-?\d+,\d+$/.test(t) ? t.replace(",", ".") : t);
    return Number.isFinite(v) ? v : NaN;
  };

  // Find the plate grid in a block of text. Returns {rows: [letter], cols: [number], cells: [[string]]} or null.
  function parseGrid(text, numeric) {
    const lines = String(text || "").split(/\r?\n/).map((l) => l.replace(/\s+$/, "")).filter((l) => l.trim());
    if (!lines.length) return null;
    const table = lines.map(splitLine).map((r) => r.map((c) => c.trim()));
    let cols = null, out = [], letters = [];
    for (const r of table) {
      const nums = r.filter((c) => c !== "").map(Number);
      if (!out.length && nums.length >= 2 && nums.every((n, i) => Number.isInteger(n) && n === nums[0] + i) && nums[0] <= 1) {
        cols = nums; // header 1 2 3 … 12
        continue;
      }
      const lead = /^[A-Pa-p]$/.test(r[0]) ? r[0].toUpperCase() : null;
      const body = lead ? r.slice(1) : r;
      const filled = body.filter((c) => c !== "");
      const okRow = numeric ? filled.filter((c) => Number.isFinite(num(c))).length >= Math.max(2, filled.length * 0.6) : (lead || filled.length >= 2);
      if (okRow && (lead || !letters.length || !letters[letters.length - 1].lead)) {
        out.push(body);
        letters.push({ lead, i: out.length - 1 });
      } else if (out.length) break; // grid ended
      if (out.length >= 16) break;
    }
    if (!out.length) return null;
    const width = Math.max(...out.map((r) => r.length));
    const rows = letters.map((l, i) => l.lead || ROWS[i]);
    cols = cols && cols.length >= width ? cols.slice(0, width) : Array.from({ length: width }, (_, i) => i + 1);
    return { rows, cols, cells: out.map((r) => Array.from({ length: width }, (_, i) => r[i] ?? "")) };
  }

  // Classify a plate-map label: standard (with concentration), blank, or sample.
  function classify(label) {
    const t = String(label || "").trim();
    if (!t || /^(empty|-|x|n\/?a|none)$/i.test(t)) return null;
    if (/^(blank|blk|bl|b|buffer|zero|0)$/i.test(t)) return { kind: "blank", name: "Blank", conc: 0 };
    let m = t.match(/^(?:std|standard|cal(?:ibrator)?|st)\s*[-_:#]?\s*(-?\d+(?:[.,]\d+)?(?:e-?\d+)?)\s*([a-zµμ\/%]*)$/i);
    if (m) return { kind: "std", name: t, conc: num(m[1]) };
    if (Number.isFinite(num(t))) return { kind: "std", name: "Std " + t, conc: num(t) };
    return { kind: "sample", name: t };
  }

  function build() {
    const g = parseGrid(view.data, true);
    if (!g) return { error: view.data.trim() ? "No plate grid found. Paste the block of readings, with or without the A–H row letters and 1–12 header." : "" };
    const mg = view.map.trim() ? parseGrid(view.map, false) : null;
    const wells = [];
    g.rows.forEach((r, i) => g.cols.forEach((c, j) => {
      const v = num(g.cells[i][j]);
      let lab = null;
      if (mg) {
        const mi = mg.rows.indexOf(r), mj = mg.cols.indexOf(c);
        lab = classify(mi >= 0 && mj >= 0 ? mg.cells[mi][mj] : "");
        if (!lab) { wells.push({ well: r + c, r, c, v, skip: true }); return; }
      } else lab = { kind: "sample", name: r + c };
      wells.push({ well: r + c, r, c, v, ...lab });
    }));
    const used = wells.filter((w) => !w.skip && Number.isFinite(w.v));
    const hasStd = used.some((w) => w.kind === "std" || w.kind === "blank");
    const vname = (view.value || "Signal").trim() || "Signal";
    let text;
    if (view.out === "wide") {
      const names = [...new Set(used.map((w) => w.name))];
      const colsV = names.map((n) => used.filter((w) => w.name === n).map((w) => w.v));
      const depth = Math.max(0, ...colsV.map((c) => c.length));
      text = [names.join("\t")].concat(Array.from({ length: depth }, (_, k) => colsV.map((c) => (k < c.length ? c[k] : "")).join("\t"))).join("\n");
    } else {
      const head = ["Well", "Sample"].concat(hasStd ? ["Concentration"] : [], [vname]);
      text = [head.join("\t")].concat(used.map((w) => [w.well, w.name].concat(hasStd ? [w.kind === "sample" ? "" : w.conc] : [], [w.v]).join("\t"))).join("\n");
    }
    const count = (k) => used.filter((w) => w.kind === k).length;
    return { g, mg, wells, used, text, hasStd, stats: { wells: used.length, std: count("std"), blank: count("blank"), sample: count("sample"), samples: new Set(used.filter((w) => w.kind === "sample").map((w) => w.name)).size } };
  }

  const hue = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; };

  function renderPreview() {
    const box = $("#pl-preview");
    if (!box) return;
    const b = build();
    if (!b.g) { box.innerHTML = b.error ? `<p class="err-text">${esc(b.error)}</p>` : '<p class="hint">The plate preview appears here.</p>'; $("#pl-make").disabled = true; return; }
    const vals = b.used.map((w) => w.v);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const byWell = Object.fromEntries(b.wells.map((w) => [w.well, w]));
    const small = b.g.cols.length > 12;
    const cell = (r, c) => {
      const w = byWell[r + c];
      if (!w) return "<td></td>";
      if (w.skip || !Number.isFinite(w.v)) return `<td class="pl-off" title="${esc(w.well)}: not used"></td>`;
      const shade = hi > lo ? (w.v - lo) / (hi - lo) : 0.5;
      const style = b.mg
        ? (w.kind === "blank" ? "background:var(--line)" : w.kind === "std" ? `background:color-mix(in srgb, var(--accent) ${Math.round(25 + shade * 60)}%, transparent)` : `background:hsl(${hue(w.name)} 55% 60% / ${(0.25 + shade * 0.6).toFixed(2)})`)
        : `background:color-mix(in srgb, var(--accent) ${Math.round(8 + shade * 80)}%, transparent)`;
      const tip = `${w.well}${b.mg ? " · " + w.name : ""}: ${w.v}`;
      return `<td style="${style}" title="${esc(tip)}">${small ? "" : `<span>${esc(b.mg ? (w.kind === "std" ? w.conc : w.kind === "blank" ? "B" : w.name) : "")}</span>`}</td>`;
    };
    const s = b.stats;
    box.innerHTML = `<div class="pl-grid-wrap"><table class="pl-grid${small ? " pl-384" : ""}"><thead><tr><th></th>${b.g.cols.map((c) => `<th>${c}</th>`).join("")}</tr></thead>
      <tbody>${b.g.rows.map((r) => `<tr><th>${r}</th>${b.g.cols.map((c) => cell(r, c)).join("")}</tr>`).join("")}</tbody></table></div>
      <p class="hint">${s.wells} wells read${b.mg ? ` · ${s.std} standard wells · ${s.blank} blanks · ${s.sample} sample wells (${s.samples} samples)` : ". Add a plate map to name the wells"}${b.hasStd && view.out === "long" ? '. Ready for <b>Standard curve</b>.' : ""}</p>`;
    $("#pl-make").disabled = !s.wells;
  }

  function open() {
    let el = $("#plate");
    if (!el) {
      el = document.createElement("div");
      el.id = "plate";
      el.className = "board";
      el.setAttribute("role", "dialog");
      el.setAttribute("aria-modal", "true");
      el.setAttribute("aria-labelledby", "plate-title");
      document.body.appendChild(el);
    }
    view.start = state.analysis;
    el.innerHTML = `<div class="board-sheet plate-sheet">
      <div class="board-head"><div><h2 id="plate-title">Import a plate</h2><p class="fine">Paste the readings from any plate reader (96 or 384 wells), and optionally a plate map saying what is in each well.</p></div>
        <button type="button" class="icon-btn pl-close" aria-label="Close">×</button></div>
      <div class="board-body plate-body">
        <div class="plate-inputs">
          <div class="field"><label for="pl-data">Readings</label>
            <textarea id="pl-data" class="data-text" rows="7" spellcheck="false" placeholder="Copy the plate block from the reader's export (with or without A–H and 1–12)">${esc(view.data)}</textarea></div>
          <div class="field"><label for="pl-map">Plate map <span class="fine">(optional)</span></label>
            <textarea id="pl-map" class="data-text" rows="7" spellcheck="false" placeholder="Same layout, with a label per well: Std 1000, Std 500, Blank, Control, Drug A… Empty wells are skipped.">${esc(view.map)}</textarea>
            <span class="hint">Standards: “Std 250” or just the number. Blanks: “Blank” or “B”. Wells with the same label are replicates.</span></div>
          <div class="field-row">
            <div class="field"><label for="pl-value">Name the reading</label><input type="text" id="pl-value" value="${esc(view.value)}" placeholder="e.g. OD 450" spellcheck="false"></div>
            <div class="field"><label for="pl-out">Make</label><select id="pl-out">
              <option value="long"${view.out === "long" ? " selected" : ""}>One row per well (standard curves, values + group)</option>
              <option value="wide"${view.out === "wide" ? " selected" : ""}>One column per sample (compare groups)</option></select></div>
          </div>
          <button type="button" class="link-btn" id="pl-demo">Try an example ELISA plate</button>
        </div>
        <div id="pl-preview" class="plate-preview"></div>
        <div class="btn-row"><button type="button" class="btn btn-primary" id="pl-make" disabled>Add as a new table</button><button type="button" class="btn pl-close">Cancel</button></div>
      </div></div>`;
    el.hidden = false;
    renderPreview();
    setTimeout(() => $("#pl-data")?.focus(), 30);
  }
  const close = () => { const el = $("#plate"); if (el) el.hidden = true; };

  function demo() {
    const concs = [1000, 500, 250, 125, 62.5, 31.25, 15.6, 0];
    const od = (c) => 0.06 + (2.4 - 0.06) / (1 + Math.pow(180 / Math.max(c, 1e-9), 1.15));
    const rnd = (k) => { const x = Math.sin(k * 99.13) * 43758.5453; return x - Math.floor(x) - 0.5; };
    const map = [], data = [];
    const samples = ["Control 1", "Control 2", "Control 3", "Treated 1", "Treated 2", "Treated 3"];
    const truth = [60, 75, 52, 240, 310, 205];
    for (let i = 0; i < 8; i++) {
      const mr = [], dr = [];
      for (let j = 0; j < 12; j++) {
        let lab = "", v = 0.045 + rnd(i * 12 + j) * 0.01;
        if (j < 2) { const c = concs[i]; lab = c ? "Std " + c : "Blank"; v = od(c) * (1 + rnd(i * 7 + j) * 0.05); }
        else if ((j === 3 || j === 4) && i < 6) { lab = samples[i]; v = od(truth[i]) * (1 + rnd(i * 13 + j) * 0.06); }
        mr.push(lab); dr.push(v.toFixed(3));
      }
      map.push(ROWS[i] + "\t" + mr.join("\t")); data.push(ROWS[i] + "\t" + dr.join("\t"));
    }
    const head = "\t" + Array.from({ length: 12 }, (_, i) => i + 1).join("\t");
    view.data = head + "\n" + data.join("\n");
    view.map = head + "\n" + map.join("\n");
    view.value = "OD 450";
    $("#pl-data").value = view.data; $("#pl-map").value = view.map; $("#pl-value").value = view.value;
    renderPreview();
  }

  function make() {
    const b = build();
    if (!b.used || !b.used.length) return;
    if (/^Example · /.test(state.name || "")) {
      state.name = "Plate " + (view.value || "reading").trim();
      const nm = $("#project-name");
      if (nm) nm.value = state.name;
    }
    const d = newDataset("Plate " + (view.value || "reading").trim(), b.text);
    if (b.hasStd && view.out === "long") {
      state.analysis = "stdcurve";
      Object.assign(state.settings.stdcurve, { dataset: d.id, conc_col: "Concentration", name_col: "Sample", signal_cols: [], blank: b.stats.blank ? "subtract" : "none" });
    }
    close();
    renderAnalysis();
    renderData();
    renderSettings();
    renderStyle();
    changed({ clearExample: true });
    toast(`Added ${b.stats.wells} wells as “${d.name}”${b.hasStd && view.out === "long" ? ", ready for a standard curve" : ""}.`);
  }

  document.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("#btn-plate")) return open();
    if (!t.closest("#plate")) return;
    if (t.closest(".pl-close") || t.id === "plate") return close();
    if (t.closest("#pl-demo")) return demo();
    if (t.closest("#pl-make")) return make();
  });
  document.addEventListener("input", (e) => {
    const id = e.target.id;
    if (id === "pl-data") view.data = e.target.value;
    else if (id === "pl-map") view.map = e.target.value;
    else if (id === "pl-value") view.value = e.target.value;
    else if (id === "pl-out") view.out = e.target.value;
    else return;
    renderPreview();
  });
  document.addEventListener("change", (e) => { if (e.target.id === "pl-out") { view.out = e.target.value; renderPreview(); } });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#plate") && !$("#plate").hidden) { e.stopPropagation(); close(); } }, true);
  return { open, parseGrid, classify };
})();
