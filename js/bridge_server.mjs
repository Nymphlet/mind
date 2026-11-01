// Mind-Hermes bridge server: resident Node process wrapping the authoritative
// web/mind.js engine. JSON-lines RPC on stdin/stdout (one JSON object per line).
// Protocol ops: step, snapshot, render, set, nudge, setTrait, setParam, export,
// import, meta, presetNames, preset, ping.
import fs from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = (await import('node:module')).createRequire(import.meta.url);
const mc = require(path.join(ROOT, 'web', 'mind.js'));

const MODEL = process.env.MIND_MODEL_PATH || path.join(ROOT, 'model', 'model.mind');
const mind = new mc.Mind(fs.readFileSync(MODEL, 'utf8'));

// Presets: op lists mirroring mind_core.TRIGGER_PRESETS. `nudge` on algebraic
// view leaves degrades to additive set (JS engine refuses algebraic nudges).
const PRESETS = {
  solar_peak: [["set", "sensors.solar_kw", 0.95], ["nudge", "sensors.battery_soc", 0.25], ["set", "sensors.weather_gloom", 0.05]],
  weather_storm: [["set", "sensors.weather_gloom", 0.90], ["set", "sensors.baro_drop", 0.75], ["set", "sensors.solar_kw", 0.06]],
  gpu_crunch: [["set", "sensors.compute_load", 0.92], ["nudge", "sensors.coolant_temp", 0.55], ["nudge", "drives.task_commit", 0.50], ["nudge", "arousal.phasic", 0.30]],
  lcm_compaction: [["nudge", "sensors.lcm_pulse", 0.70], ["nudge", "cognition.pointer_depth", 0.28], ["nudge", "sensors.unprocessed_notes", 0.22]],
  battery_evening_low: [["set", "sensors.battery_soc", 0.20], ["set", "sensors.solar_kw", 0.0], ["nudge", "intimacy.cuddle_drive", 0.45]],
  interrupt_storm: [["set", "sensors.interrupt_rate", 0.85], ["nudge", "cognition.surprise", 0.55], ["nudge", "cognition.adversity", 0.45], ["nudge", "arousal.phasic", 0.40]],
  user_arrive: [["set", "social.user_present", 0.95], ["nudge", "social.warmth", 0.30], ["nudge", "drives.task_commit", 0.40]],
  user_leave: [["set", "social.user_present", 0.0], ["set", "drives.task_commit", 0.05]],
  praise_success: [["nudge", "mood.reaction", 0.65], ["nudge", "social.warmth", 0.55], ["nudge", "drives.standards_met", 0.50], ["nudge", "cognition.flow", 0.35]],
  flirt_intimate: [["nudge", "intimacy.salience", 0.50], ["nudge", "intimacy.receptivity", 0.35], ["nudge", "social.warmth", 0.40], ["nudge", "arousal.phasic", 0.30], ["nudge", "intimacy.inhibition", -0.25]],
  intimate_afterglow: [["set", "intimacy.satiety", 0.90], ["nudge", "social.warmth", 0.65], ["nudge", "mood.reaction", 0.55], ["nudge", "intimacy.salience", -0.40]],
  conflict_critique: [["nudge", "mood.reaction", -0.65], ["nudge", "arousal.phasic", 0.55], ["nudge", "cognition.threat", 0.55], ["nudge", "cognition.adversity", 0.40]],
  user_stressed: [["set", "sensors.user_stress_cue", 0.85], ["nudge", "sensors.open_loops", 0.30]],
};

function leafBounds() {
  const out = {};
  for (const [k, lf] of Object.entries(mind.engine.ir.leaves)) out[k] = { min: lf.min, max: lf.max, kind: lf.kind };
  return out;
}
const LEAVES = leafBounds();
const FILTER_LEAVES = new Set((mind.ir.filterSites || []).map(s => s.leaf));

function applyOp(op, pathOrName, val, scale) {
  const v = val * (scale ?? 1);
  const lf = LEAVES[pathOrName];
  if (op === "set") { mind.engine.setState(pathOrName, v); return; }
  if (lf && lf.kind !== "ode" && !FILTER_LEAVES.has(pathOrName)) {
    throw new Error("target " + pathOrName + " is an algebraic view; nudge an ODE/sensor leaf instead");
  }
  mind.engine.nudge(pathOrName, v);
}

function reply(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return reply({ ok: false, error: "bad_json: " + e.message }); }
  try {
    const E = mind.engine;
    switch (msg.op) {
      case "ping": return reply({ ok: true, pong: true });
      case "step": {
        const n = Math.max(1, Math.floor(msg.n || 1));
        E.advance(n);
        return reply({ ok: true, frame: E.snapshot(), state: { ...E.state }, filters: E.filtersSnapshot(), sim_time: E.simTime });
      }
      case "snapshot": return reply({ ok: true, frame: E.snapshot(), filters: E.filtersSnapshot() });
      case "filters": return reply({ ok: true, filters: E.filtersSnapshot(), sites: mind.ir.filterSites || [] });
      case "render": return reply({ ok: true, text: mind.render(msg.name || "ambient_mind", msg.frame ?? undefined) });
      case "set": E.setState(msg.path, Number(msg.value)); return reply({ ok: true, value: E.snapshot()[msg.path] });
      case "nudge": applyOp("nudge", msg.path, Number(msg.delta), msg.scale); return reply({ ok: true, value: E.snapshot()[msg.path] });
      case "setTrait": E.setTrait(msg.name, Number(msg.value)); return reply({ ok: true, value: E.traitValues[msg.name] });
      case "setParam": E.setParam(msg.name, Number(msg.value)); return reply({ ok: true });
      case "export": return reply({ ok: true, blob: E.exportState() });
      case "import": E.importState(msg.blob); return reply({ ok: true });
      case "meta": return reply({
        ok: true,
        leaves: LEAVES,
        traits: Object.fromEntries(Object.entries(mind.engine.ir.traits || {}).map(([k, t]) => [k, { min: t.min, max: t.max, default: t.default }])),
        params: Object.fromEntries(Object.entries(mind.engine.ir.params || {}).map(([k, p]) => [k, { min: p.min, max: p.max, default: p.default }])),
        filters: mind.ir.filterSites || [],
        dt: E.dt, solver: E.solver,
      });
      case "presetNames": return reply({ ok: true, names: Object.keys(PRESETS) });
      case "preset": {
        const ops = PRESETS[msg.name];
        if (!ops) return reply({ ok: false, error: "unknown_preset " + msg.name });
        for (const [op, tgt, v] of ops) applyOp(op, tgt, v, msg.scale);
        return reply({ ok: true, frame: E.snapshot() });
      }
      default: return reply({ ok: false, error: "unknown_op " + msg.op });
    }
  } catch (e) {
    return reply({ ok: false, error: String(e && e.message || e) });
  }
});
rl.on("close", () => process.exit(0));
reply({ ok: true, ready: true, leaves: Object.keys(LEAVES).length, traits: Object.keys(mind.engine.traitValues).length });
