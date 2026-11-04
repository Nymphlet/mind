(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const { React } = SDK;
  const h = React.createElement;

  const API = "/api/plugins/mind-hermes";
  const TOKEN = () => window.__HERMES_SESSION_TOKEN__ ?? "";

  const QUICK_PRESETS = [
    { id: "solar_peak", label: "☀️ Solar Surplus" },
    { id: "gpu_crunch", label: "🔥 GPU + Waterloop Crunch" },
    { id: "lcm_compaction", label: "🌀 LCM Compaction" },
    { id: "praise_success", label: "✨ Warm Praise" },
    { id: "flirt_intimate", label: "🔥 Flirt Spark" },
    { id: "intimate_afterglow", label: "💞 Intimate Afterglow" },
    { id: "battery_evening_low", label: "🔋 Evening Cuddle Wind-Down" },
    { id: "conflict_critique", label: "⚡ Friction / Critique" }
  ];

  // Raw-quantity display metadata (suffix after "sensor_real.").
  const RAW_LABELS = {
    temp_c: ["Outside temp", "°C"],
    cloud_pct: ["Cloud cover", "%"],
    precip_mmh: ["Precipitation", "mm/h"],
    pressure_hpa: ["Pressure", "hPa"],
    coolant_temp_c: ["Coolant loop", "°C"],
    cpu_load_pct: ["CPU load", "%"],
    gpu_load_pct: ["GPU load", "%"],
    solar_kwp: ["Solar surplus", "kW"],
    battery_pct: ["Battery SoC", "%"],
    vault_inbox_notes: ["Vault inbox", "notes"],
    vault_orphan_notes: ["Vault orphans", "notes"],
    vault_open_checkboxes: ["Open loops", "tasks"],
    vault_total_notes: ["Vault size", "notes"],
    honcho_pending: ["Honcho pending", "items"]
  };

  const S = {
    btn: {
      background: "#1c2230", color: "#cbd5e1", border: "1px solid #2e384d",
      borderRadius: "4px", padding: "3px 8px", fontSize: "11px", cursor: "pointer"
    },
    btnActive: {
      background: "#1e3a8a", color: "#fff", border: "1px solid #3b82f6",
      borderRadius: "4px", padding: "3px 8px", fontSize: "11px", cursor: "pointer"
    },
    pill: {
      background: "#10141d", color: "#64748b", border: "1px solid #2e384d",
      borderRadius: "10px", padding: "2px 9px", fontSize: "11px", cursor: "pointer"
    },
    pillOn: {
      background: "#0b2a24", color: "#34d399", border: "1px solid #14b8a6",
      borderRadius: "10px", padding: "2px 9px", fontSize: "11px", cursor: "pointer"
    },
    card: {
      background: "#10141d", border: "1px solid #222938", borderRadius: "6px",
      padding: "10px 12px", display: "flex", flexDirection: "column", gap: "6px"
    },
    cardTitle: { fontSize: "12px", fontWeight: 700, color: "#60a5fa" },
    mono: { fontFamily: "ui-monospace, Menlo, monospace", fontSize: "11px", color: "#34d399", whiteSpace: "pre-wrap" },
    key: { fontSize: "11px", color: "#94a3b8", minWidth: "160px" },
    val: { fontSize: "11px", color: "#e2e8f0", fontFamily: "ui-monospace, Menlo, monospace" },
    input: {
      background: "#0b0e14", color: "#e2e8f0", border: "1px solid #2e384d",
      borderRadius: "4px", padding: "2px 6px", fontSize: "11px",
      fontFamily: "ui-monospace, Menlo, monospace", width: "170px"
    }
  };

  function kv(k, v) {
    return h("div", { key: k, style: { display: "flex", gap: "10px", alignItems: "baseline" } },
      h("span", { style: S.key }, k),
      h("span", { style: S.val }, typeof v === "object" && v !== null
        ? (v.configured ? "✓ set (hidden)" : "✗ unset")
        : String(v ?? "")));
  }

  function section(title, children) {
    return h("div", { style: S.card }, h("div", { style: S.cardTitle }, title), ...children);
  }

  // ---- Sparkline chart (per-series, auto-scaled to its own value range) ----
  function hexA(hex, a) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
    return m ? `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})` : hex;
  }
  function Spark({ vals, w = 300, hh = 74, color = "#34d399", unit = "", label = "" }) {
    const ref = React.useRef(null);
    React.useEffect(() => {
      try {
      const cv = ref.current;
      if (!cv) return;
      const dpr = window.devicePixelRatio || 1;
      cv.width = w * dpr;
      cv.height = hh * dpr;
      const ctx = cv.getContext("2d");
      if (!ctx) return;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, hh);
      const nums = vals.filter((v) => typeof v === "number");
      if (nums.length < 2) {
        ctx.fillStyle = "#64748b";
        ctx.font = "10px monospace";
        ctx.fillText("waiting for data…", 8, hh / 2);
        return;
      }
      let mn = Infinity, mx = -Infinity;
      for (const v of nums) { if (v < mn) mn = v; if (v > mx) mx = v; }
      if (mx - mn < 1e-9) { mn -= 0.5; mx += 0.5; }
      const pad = 15, n = vals.length;
      const fmt = (v) => (Math.abs(mx - mn) >= 10 ? v.toFixed(0) : Math.abs(mx - mn) >= 1 ? v.toFixed(1) : v.toFixed(2)) + unit;
      const X = (i) => (i / (n - 1)) * w;
      const Y = (v) => hh - pad - ((v - mn) / (mx - mn)) * (hh - 2 * pad);
      // baseline
      ctx.strokeStyle = "#222938";
      ctx.beginPath(); ctx.moveTo(0, hh - 4); ctx.lineTo(w, hh - 4); ctx.stroke();
      // area under the curve (helps the eye read magnitude, not just wiggle)
      ctx.beginPath();
      let started = false, firstX = 0, lastPt = null;
      for (let i = 0; i < n; i++) {
        const v = vals[i];
        if (typeof v !== "number") continue;
        if (!started) { ctx.moveTo(X(i), Y(v)); firstX = X(i); started = true; } else ctx.lineTo(X(i), Y(v));
        lastPt = [X(i), Y(v)];
      }
      ctx.lineTo(lastPt[0], hh - 4); ctx.lineTo(firstX, hh - 4); ctx.closePath();
      ctx.fillStyle = hexA(color, 0.12);
      ctx.fill();
      // line
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      started = false;
      for (let i = 0; i < n; i++) {
        const v = vals[i];
        if (typeof v !== "number") continue;
        if (!started) { ctx.moveTo(X(i), Y(v)); started = true; } else ctx.lineTo(X(i), Y(v));
      }
      ctx.stroke();
      // last-value marker
      if (lastPt) {
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.arc(lastPt[0], lastPt[1], 2.4, 0, Math.PI * 2); ctx.fill();
      }
      // window range labels: the scale is per-series, so show what it means
      ctx.font = "9px monospace";
      ctx.fillStyle = "#64748b";
      ctx.fillText(fmt(mx), 4, 9);
      ctx.fillText(fmt(mn), 4, hh - 5);
      } catch (e) { console.error("[mind-hermes] spark draw failed:", e); }
    }, [vals, w, hh, color, unit, label]);
    return h("canvas", {
      ref,
      style: { width: w + "px", height: hh + "px", background: "#0b0e14", border: "1px solid #222938", borderRadius: "4px" }
    });
  }

  function fmtNum(v) {
    if (typeof v !== "number") return "—";
    const a = Math.abs(v);
    return a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
  }

  // ---- History sub-tab: selectable real-value series graphs ----
  function MindHistory() {
    const [hist, setHist] = React.useState(null);
    const [err, setErr] = React.useState("");
    const [sel, setSel] = React.useState([
      "sensor_real.temp_c", "sensor_real.pressure_hpa",
      "sensor_real.coolant_temp_c", "sensor_real.gpu_load_pct",
      "sensor_real.battery_pct", "sensor_real.vault_inbox_notes"
    ]);
    const [filter, setFilter] = React.useState("");

    const refresh = React.useCallback(async () => {
      try {
        const d = await SDK.fetchJSON(API + "/history");
        setHist((prev) => {
          // skip re-render when the ring hasn't advanced (keeps canvas flicker &
          // React churn off the wall whenever the recorder is idle)
          if (prev && prev.count === d.count && Array.isArray(prev.t) && Array.isArray(d.t)
              && prev.t[prev.t.length - 1] === d.t[d.t.length - 1]) return prev;
          return d;
        });
        setErr("");
      } catch (e) {
        const msg = String(e && e.message || e);
        setErr(/401|unauthorized/i.test(msg)
          ? "auth failed — open the dashboard once with ?token=<session-token> (or log in), then reload this tab"
          : msg);
      }
    }, []);

    React.useEffect(() => {
      refresh();
      const id = setInterval(refresh, 10000);
      return () => clearInterval(id);
    }, [refresh]);

    if (err && !hist) return h("div", { style: { padding: "14px", color: "#f87171", fontSize: "12px" } }, "History load failed: " + err);
    if (!hist) return h("div", { style: { padding: "14px", color: "#64748b", fontSize: "12px" } }, "Loading History…");

    const names = Object.keys(hist.series || {});
    const meta = hist.meta || {};
    const realNames = names.filter((n) => meta[n] && meta[n].real);
    const pseudoNames = names.filter((n) => meta[n] && meta[n].pseudo);
    const engineNames = names.filter((n) => meta[n] && !meta[n].real && !meta[n].pseudo);
    const toggle = (name) => setSel((s) => s.includes(name) ? s.filter((x) => x !== name) : s.concat([name]));

    const mget = (n) => meta[n] || {};
    const flt = (list) => {
      const q = filter.trim().toLowerCase();
      if (!q) return list;
      return list.filter((n) => sel.includes(n) || (n + " " + (mget(n).label || "")).toLowerCase().includes(q));
    };
    const pillRow = (title, list) => list.length ? h(React.Fragment, null,
      h("div", { style: { fontSize: "11px", color: "#60a5fa", fontWeight: 700, marginTop: "4px" } }, title),
      h("div", { style: { display: "flex", gap: "5px", flexWrap: "wrap" } },
        flt(list).map((n) => h("button", { key: n, onClick: () => toggle(n), style: sel.includes(n) ? S.pillOn : S.pill },
          (mget(n).label || n) + (mget(n).unit ? " · " + mget(n).unit : ""))))
    ) : null;

    // engine leaves grouped by ODE block
    const blocks = {};
    for (const n of engineNames) {
      const b = meta[n].block || "state";
      (blocks[b] = blocks[b] || []).push(n);
    }

    const events = (hist.events || []).slice(-10).reverse();

    // human-readable window span from sample timestamps
    let windowTxt = hist.count + " samples";
    if (Array.isArray(hist.t) && hist.t.length > 1) {
      const span = hist.t[hist.t.length - 1] - hist.t[0];
      const hm = (s) => (s >= 3600 ? (s / 3600).toFixed(1) + " h" : s >= 60 ? Math.round(s / 60) + " min" : Math.round(s) + " s");
      windowTxt = hist.count + " samples · last " + hm(span);
    }

    const charts = sel.filter((n) => hist.series[n]).map((n) => {
      const series = hist.series[n];
      let latest = null, prev = null, found = 0;
      for (let i = series.length - 1; i >= 0 && found < 2; i--) {
        if (typeof series[i] === "number") { if (found === 0) latest = series[i]; else prev = series[i]; found++; }
      }
      const unit = mget(n).unit || "";
      let deltaTxt = "", deltaCol = "#64748b";
      if (latest !== null && prev !== null) {
        const d = latest - prev;
        if (Math.abs(d) < 1e-9) { deltaTxt = "→ stable"; }
        else if (d > 0) { deltaTxt = "▲ " + fmtNum(d) + unit; deltaCol = "#fbbf24"; }
        else { deltaTxt = "▼ " + fmtNum(-d) + unit; deltaCol = "#60a5fa"; }
      }
      return h("div", { key: n, style: S.card },
        h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "baseline" } },
          h("span", { style: S.cardTitle }, mget(n).label || n),
          h("span", {},
            h("span", { style: { ...S.val, marginRight: "8px" } }, fmtNum(latest) + " " + unit),
            h("span", { style: { fontSize: "10px", color: deltaCol, fontFamily: "monospace" } }, deltaTxt))),
        h(Spark, { vals: series, color: mget(n).real ? "#34d399" : "#fbbf24", unit, label: mget(n).label || n }),
        h("div", { style: { fontSize: "10px", color: "#64748b" } },
          mget(n).real ? "real measured quantity" : "normalized 0..1 value the ODE consumes",
          " · window min→max shown in chart corners · " + windowTxt));
    });

    return h("div", {
      style: { flex: 1, overflowY: "auto", padding: "12px", display: "flex", flexDirection: "column", gap: "10px" }
    },
      err && h("div", { style: { color: "#f87171", fontSize: "11px" } }, "stale data: " + err),
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
        h("span", { style: { ...S.cardTitle, fontSize: "13px" } },
          "Runtime history — " + hist.count + " samples · one per tick · 24 h ring · full engine state + event marks (replay tape)"),
        h("button", { onClick: refresh, style: { ...S.btn, marginLeft: "auto" } }, "↻ Refresh")),
      events.length ? h("div", { style: { fontSize: "10px", color: "#94a3b8", display: "flex", gap: "8px", flexWrap: "wrap" } },
        events.map((e, i) => h("span", { key: i, style: { color: "#fbbf24" } },
          new Date(e.t * 1000).toLocaleTimeString() + " " + e.kind + " " + e.target + " " + e.value))) : null,
      section("Series — click pills to add/remove graphs, each chart auto-scales to its own unit", [
        h("input", {
          style: { ...S.input, maxWidth: "320px" }, type: "text", placeholder: "🔍 filter series (name or label)…",
          value: filter, onChange: (e) => setFilter(e.target.value)
        }),
        pillRow("Real feeds (actual quantities)", realNames),
        pillRow("Normalized ODE inputs (pseudo)", pseudoNames),
        ...Object.keys(blocks).sort().map((b) => pillRow("Engine state · " + b, blocks[b]))
      ]),
      charts.length
        ? h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: "10px" } }, charts)
        : h("div", { style: { padding: "10px", color: "#64748b", fontSize: "12px" } },
            names.length ? "no series selected — pick some pills above" : "no samples yet — waiting for the next tick (≤60 s)")
    );
  }

  // ---- Console sub-tab: live stats + persona activation + editable settings ----
  function MindConsole() {
    const [live, setLive] = React.useState(null);
    const [persona, setPersona] = React.useState(null);
    const [settings, setSettings] = React.useState(null);
    const [busy, setBusy] = React.useState("");
    const [err, setErr] = React.useState("");
    const [drafts, setDrafts] = React.useState({});

    const refresh = React.useCallback(async () => {
      try {
        const [l, p, st] = await Promise.all([
          SDK.fetchJSON(API + "/live"),
          SDK.fetchJSON(API + "/persona"),
          SDK.fetchJSON(API + "/settings")
        ]);
        setLive(l); setPersona(p); setSettings(st); setErr("");
      } catch (e) { setErr(String(e)); }
    }, []);

    React.useEffect(() => {
      refresh();
      const id = setInterval(refresh, 5000);
      return () => clearInterval(id);
    }, [refresh]);

    const activate = async (name) => {
      setBusy(name); setErr("");
      try {
        await SDK.fetchJSON(API + "/persona/load", {
          method: "POST",
          body: JSON.stringify({ name, keep_state: name !== "bundled" })
        });
        await refresh();
      } catch (e) { setErr(String(e)); } finally { setBusy(""); }
    };

    const putSetting = async (key, value) => {
      setBusy(key); setErr("");
      try {
        await SDK.fetchJSON(API + "/settings", {
          method: "PUT",
          body: JSON.stringify({ [key]: value })
        });
        setDrafts((d) => { const n = Object.assign({}, d); delete n[key]; return n; });
        await refresh();
      } catch (e) { setErr(String(e)); } finally { setBusy(""); }
    };

    if (err && !live) return h("div", { style: { padding: "14px", color: "#f87171", fontSize: "12px" } }, "Console load failed: " + err);
    if (!live) return h("div", { style: { padding: "14px", color: "#64748b", fontSize: "12px" } }, "Loading Console…");

    const w = live.weather || {};
    const loc = live.settings && live.settings.weather_location;
    const personaRows = [];
    if (persona) {
      for (const p of persona.personas || []) {
        personaRows.push(h("div", { key: p.name, style: { display: "flex", gap: "10px", alignItems: "center" } },
          h("span", { style: S.val }, p.name + " · " + Math.round(p.size / 1024) + " KiB"),
          h("button", {
            style: busy === p.name ? S.btnActive : S.btn, disabled: busy !== "",
            onClick: () => activate(p.name)
          }, busy === p.name ? "loading…" : "activate")));
      }
      personaRows.push(h("div", { key: "__bundled", style: { display: "flex", gap: "10px", alignItems: "center" } },
        h("span", { style: S.key }, "factory model · " + persona.bundled.name),
        h("button", {
          style: busy === "bundled" ? S.btnActive : S.btn, disabled: busy !== "",
          onClick: () => activate("bundled")
        }, busy === "bundled" ? "loading…" : "reset to bundled")));
      if (!(persona.personas || []).length)
        personaRows.push(h("div", { key: "__empty", style: S.key },
          "no .mind files in plugin-data/mind-hermes/personas/ yet — drop generic .mind models there"));
    }

    const haSet = live.settings && live.settings.ha_base_url;
    const raw = live.raw || {};
    const ingest = live.ingest || {};

    const rawRows = Object.entries(raw).map(([k, v]) => {
      const suffix = k.replace("sensor_real.", "");
      const [label, unit] = RAW_LABELS[suffix] || [suffix, ""];
      return kv(label, fmtNum(v) + (unit ? " " + unit : ""));
    });

    const settingRows = settings ? Object.entries(settings.values || {}).map(([key, val]) => {
      const draft = drafts[key];
      const rowSave = (v) => putSetting(key, v);
      if (typeof val === "boolean") {
        return h("div", { key, style: { display: "flex", gap: "10px", alignItems: "center" } },
          h("span", { style: S.key }, key),
          h("input", {
            type: "checkbox", checked: !!val, disabled: busy === key,
            onChange: (e) => rowSave(e.target.checked)
          }),
          h("span", { style: S.val }, val ? "on" : "off"));
      }
      if (val && typeof val === "object" && val.configured !== undefined) {
        return h("div", { key, style: { display: "flex", gap: "8px", alignItems: "center" } },
          h("span", { style: S.key }, key),
          h("span", { style: S.val }, val.configured ? "✓ set" : "✗ unset"),
          h("input", { type: "password", style: S.input, placeholder: "••••••", value: drafts[key] || "",
            onChange: (e) => setDrafts((d) => Object.assign({}, d, { [key]: e.target.value })) }),
          h("button", { style: busy === key ? S.btnActive : S.btn, disabled: busy === key || !drafts[key],
            onClick: () => rowSave(drafts[key]) }, "set"));
      }
      const isNum = typeof val === "number";
      return h("div", { key, style: { display: "flex", gap: "8px", alignItems: "center" } },
        h("span", { style: S.key }, key),
        h("input", {
          style: S.input, type: isNum ? "number" : "text",
          value: drafts[key] !== undefined ? drafts[key] : String(val ?? ""),
          onChange: (e) => setDrafts((d) => Object.assign({}, d, { [key]: e.target.value })),
          onKeyDown: (e) => { if (e.key === "Enter") rowSave(isNum ? Number(e.target.value) : e.target.value); }
        }),
        h("button", { style: busy === key ? S.btnActive : S.btn, disabled: busy === key,
          onClick: () => rowSave(isNum ? Number(drafts[key] !== undefined ? drafts[key] : val) : (drafts[key] !== undefined ? drafts[key] : val)) },
          busy === key ? "saving…" : "save"));
    }) : [];

    return h("div", {
      style: { flex: 1, overflowY: "auto", padding: "12px", display: "flex", flexDirection: "column", gap: "10px" }
    },
      err && h("div", { style: { color: "#f87171", fontSize: "11px" } }, "stale data: " + err),
      section("Runtime", [
        h("div", { style: S.mono }, live.state_telemetry || "(no telemetry)"),
        h("div", { style: { display: "flex", gap: "10px" } },
          h("span", { style: S.key }, "sim time / engine"),
          h("span", { style: S.val }, Math.round(live.sim_time || 0) + "s · " + (live.degraded ? "degraded (no bridge)" : "bridge ok")))
      ]),
      section("Real sensor feeds (measured quantities)", [
        rawRows.length ? rawRows
          : h("div", { style: S.key }, "no readings yet — first tick collects within ≤60 s"),
        !haSet && h("div", { style: { fontSize: "11px", color: "#fbbf24" } },
          "⚠ Home Assistant not configured — battery/solar stay synthetic (nothing to measure)")
      ]),
      section("Blended into mind (normalized 0..1 ODE inputs)", [
        h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", fontSize: "10px", color: "#94a3b8" } },
          Object.entries(ingest).map(([k, v]) => h("span", {
            key: k,
            style: { background: v ? "#0b2a24" : "#2a1a10", border: "1px solid " + (v ? "#14b8a6" : "#7c4a1e"),
              borderRadius: "8px", padding: "1px 7px", color: v ? "#34d399" : "#fbbf24" }
          }, k + (v ? " ✓" : " ✗")))),
        Object.entries(live.sensors || {}).map(([k, v]) => kv(k.replace("sensors.", ""),
          typeof v === "number" ? v.toFixed(3) : v))
      ]),
      section("Weather · source", [
        kv("location", loc ? loc.latitude + ", " + loc.longitude : "—"),
        kv("last fetch", w.last_fetch_age_s == null ? "not yet fetched (first tick ≤60s, cache 15min)"
          : Math.round(w.last_fetch_age_s) + "s ago (cache 15min)")
      ]),
      section("Personas (.mind override)", [
        h("div", { style: { display: "flex", gap: "10px", alignItems: "baseline" } },
          h("span", { style: S.key }, "active"),
          h("span", { style: S.val }, persona && (persona.active.is_override
            ? persona.active.name + "  (override)" : persona.active.name + "  (bundled)"))
        ),
        ...personaRows
      ]),
      section("Runtime settings (editable — survives rebuilds via plugin-data/settings.json)", [
        ...settingRows,
        settings && h("div", { style: { fontSize: "10px", color: "#64748b" } },
          "nix config: " + (settings.config_keys || []).length + " keys · console overrides: " + ((settings.override_keys || []).length ? (settings.override_keys || []).join(", ") : "none"))
      ])
    );
  }

  // ---- Workbench sub-tab: live HUD header + interactive iframe ----
  function MindWorkbench({ status, viewMode, setViewMode, wbHtml, firePreset, forceTick }) {
    const [showEq, setShowEq] = React.useState(false);
    const [eqSrc, setEqSrc] = React.useState(null);
    React.useEffect(() => {
      if (showEq && eqSrc === null) {
        fetch(API + "/model.mind", { headers: { "X-Hermes-Session-Token": TOKEN() } })
          .then((r) => { if (!r.ok) throw new Error("model HTTP " + r.status); return r.text(); })
          .then(setEqSrc)
          .catch((e) => setEqSrc("equation source load failed: " + String(e)));
      }
    }, [showEq]);
    return h(React.Fragment, null,
      h("div", {
        style: {
          padding: "8px 14px", background: "#141821", borderBottom: "1px solid #252d3d",
          display: "flex", flexDirection: "column", gap: "6px", flexShrink: 0
        }
      },
        h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px" } },
          h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
            h("span", { style: { fontWeight: 700, color: "#60a5fa", fontSize: "13px" } }, "🧠 Live Hermes Mind State"),
            status && h("span", {
              style: { fontSize: "11px", fontFamily: "ui-monospace, monospace", background: "#1e2636", padding: "2px 8px", borderRadius: "4px", color: "#93c5fd" }
            }, "Temp Hint: " + status.sampling_temperature_hint + " · Wake(Out=" + (status.wake?.fire_outreach ?? 0).toFixed(2) + ", Silent=" + (status.wake?.fire_background ?? 0).toFixed(2) + ")")
          ),
          h("div", { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
            QUICK_PRESETS.map((p) => h("button", { key: p.id, onClick: () => firePreset(p.id), style: S.btn }, p.label)),
            h("button", { onClick: forceTick, style: S.btnActive }, "+1m Live Tick"),
            h("button", { onClick: () => setShowEq((v) => !v), style: S.btn }, showEq ? "📕 Hide Equations" : "📜 Equations"),
            h("button", { onClick: () => setViewMode(viewMode === "split" ? "workbench" : "split"), style: S.btn },
              viewMode === "split" ? "⤢ Maximize Workbench" : "⤡ Show Live HUD")
          )
        ),
        viewMode === "split" && status && h("div", {
          style: {
            fontFamily: "ui-monospace, Menlo, monospace", fontSize: "11px", color: "#34d399",
            background: "#0b0e14", border: "1px solid #222938", borderRadius: "4px", padding: "6px 9px",
            maxHeight: "68px", overflowY: "auto", whiteSpace: "pre-wrap"
          }
        }, status.ambient_mind + "\n" + status.state_telemetry),
        showEq && h("pre", {
          style: {
            fontFamily: "ui-monospace, Menlo, monospace", fontSize: "10.5px", color: "#93c5fd",
            background: "#0b0e14", border: "1px solid #222938", borderRadius: "4px", padding: "8px 10px",
            maxHeight: "45vh", overflowY: "auto", whiteSpace: "pre", margin: "0", lineHeight: "1.4"
          }
        }, eqSrc === null ? "loading active equation source…" : eqSrc)
      ),
      wbHtml != null
        ? h("iframe", {
            srcdoc: wbHtml, title: "Mind-Hermes Interactive Workbench",
            style: { flex: 1, width: "100%", border: "none", background: "#0d1017" }
          })
        : h("div", { style: { flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "#64748b", fontSize: "12px" } }, "Loading Workbench…")
    );
  }

  function MindHermesDashboardTab() {
    const [subTab, setSubTab] = React.useState("workbench");
    const [status, setStatus] = React.useState(null);
    const [viewMode, setViewMode] = React.useState("split");
    const [wbHtml, setWbHtml] = React.useState(null);

    const fetchStatus = React.useCallback(async () => {
      try {
        setStatus(await SDK.fetchJSON(API + "/status"));
      } catch (e) {
        console.error("[mind-hermes] status error:", e);
      }
    }, []);

    React.useEffect(() => {
      fetchStatus();
      const id = setInterval(fetchStatus, 5000);
      return () => clearInterval(id);
    }, [fetchStatus]);

    React.useEffect(() => {
      // Raw iframe GETs can't carry the dashboard session header, so fetch the
      // (fully self-contained) workbench HTML here and feed it via srcdoc.
      fetch(API + "/workbench", {
        credentials: "include",
        headers: { "X-Hermes-Session-Token": TOKEN() }
      })
        .then((r) => {
          if (!r.ok) throw new Error("workbench HTTP " + r.status);
          return r.text();
        })
        .then(setWbHtml)
        .catch((e) => console.error("[mind-hermes] workbench load failed:", e));
    }, []);

    const firePreset = async (presetId) => {
      try {
        setStatus(await SDK.fetchJSON(API + "/event", {
          method: "POST",
          body: JSON.stringify({ kind: "preset", target: presetId, value: 1.0 })
        }));
      } catch (e) { console.error(e); }
    };

    const forceTick = async () => {
      try {
        setStatus(await SDK.fetchJSON(API + "/tick", { method: "POST" }));
      } catch (e) { console.error(e); }
    };

    React.useEffect(() => {
      const onKey = (e) => {
        const tag = (e.target && e.target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || (e.target && e.target.isContentEditable)) return;
        if (e.key === "1") setSubTab("workbench");
        else if (e.key === "2") setSubTab("console");
        else if (e.key === "3") setSubTab("history");
      };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }, []);

    const tabBtn = (id, label) => h("button", {
      key: id, onClick: () => setSubTab(id),
      style: subTab === id ? S.btnActive : S.btn
    }, label);

    return h("div", {
      style: { display: "flex", flexDirection: "column", height: "calc(100vh - 56px)", background: "#0d1017", color: "#e2e8f0", overflow: "hidden" }
    },
      h("div", {
        style: { display: "flex", gap: "6px", padding: "6px 14px", background: "#0b0e14", borderBottom: "1px solid #252d3d", flexShrink: 0 }
      }, tabBtn("workbench", "🧠 Workbench"), tabBtn("console", "🛠 Console"), tabBtn("history", "📈 History")),
      subTab === "console"
        ? h(MindConsole)
        : subTab === "history"
          ? h(MindHistory)
          : h(MindWorkbench, { status, viewMode, setViewMode, wbHtml, firePreset, forceTick })
    );
  }

  window.__HERMES_PLUGINS__.register("mind-hermes", class MindHermesErrorBoundary extends React.Component {
    // The host SPA renders plugin tabs with no error boundary of its own: an
    // uncaught throw here used to unmount React's whole tree (white-screen the
    // entire dashboard). Catch at the plugin root and show the failure instead.
    constructor(props) {
      super(props);
      this.state = { err: null };
    }
    static getDerivedStateFromError(err) { return { err }; }
    componentDidCatch(err, info) {
      console.error("[mind-hermes] tab crashed:", err, info);
    }
    render() {
      if (this.state.err) {
        return h("div", { style: { padding: "18px", color: "#f87171", fontSize: "12px", fontFamily: "monospace" } },
          h("div", { style: { fontWeight: 700, marginBottom: "8px" } }, "🛑 mind-hermes tab crashed (dashboard is fine)"),
          h("pre", { style: { whiteSpace: "pre-wrap", color: "#fca5a5", fontSize: "11px" } },
            String(this.state.err && (this.state.err.stack || this.state.err) || "unknown error")),
          h("button", {
            onClick: () => this.setState({ err: null }),
            style: { marginTop: "10px", padding: "5px 12px", background: "#252d3d", color: "#e2e8f0", border: "1px solid #3a4660", borderRadius: "6px", cursor: "pointer" }
          }, "↺ retry"));
      }
      return h(MindHermesDashboardTab, this.props);
    }
  });
})();
