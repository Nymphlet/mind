"""Mind-Hermes runtime: resident ODE state shared by Hermes hooks, dashboard API,
and (optionally) proactive gateway wakes.

The ODE engine is the authoritative JavaScript framework (web/mind.js) driven
through a resident Node subprocess (js/bridge_server.mjs, JSON-lines RPC).
No Python re-port: single source of truth, exact parity with the Workbench.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# The host process may already hold a *foreign or stale* ``sensors`` module in
# sys.modules (sealed Nix store copy of this plugin). Reuse it only when it
# carries the current ABI marker; otherwise load this plugin's sibling copy.
import importlib.util as _ilu  # noqa: E402

_SENSORS_ABI = 3  # v3: pump tach mirror + HA presence reader (sensors.py)

def _load_sensors_module():
    mod = sys.modules.get("sensors")
    if mod is not None and getattr(mod, "_SENSORS_ABI", 0) == _SENSORS_ABI and str(getattr(mod, "__file__", "")).startswith(str(PLUGIN_DIR)):
        return mod
    spec = _ilu.spec_from_file_location("mind_hermes_sensors", PLUGIN_DIR / "sensors.py")
    mod = _ilu.module_from_spec(spec)
    # Register before exec: @dataclass annotation resolution looks the module
    # up in sys.modules by name while the module body is still running.
    sys.modules["mind_hermes_sensors"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("mind_hermes_sensors", None)
        raise
    return mod

_sensors = _load_sensors_module()
SensorConfig = _sensors.SensorConfig
TelemetryCollector = _sensors.TelemetryCollector

DEFAULT_MODEL_PATH = PLUGIN_DIR / "model" / "model.mind"
BRIDGE_PATH = PLUGIN_DIR / "js" / "bridge_server.mjs"


def get_data_dir() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    d = hermes_home / "plugin-data" / "mind-hermes"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Persona overrides live in plugin-data (writable on sealed installs); the
# bundled model stays the factory default. `active.mind` is the loaded persona.
PERSONAS_DIR = get_data_dir() / "personas"
ACTIVE_PERSONA_PATH = PERSONAS_DIR / "active.mind"

# Console-editable runtime settings override (survives Nix rebuilds; layered on
# top of plugins.entries.mind-hermes.settings at startup). Secrets live here,
# never echoed back by the settings view.
SETTINGS_PATH = get_data_dir() / "settings.json"

# One recorded sample per tick; 1440 samples = 24h at the default 60s tick.
# Persisted to disk so the gateway ticker (which produces samples) and the
# dashboard process (which renders them) share one ring despite separate
# runtime singletons.
HISTORY_MAX = 1440
HISTORY_PATH = get_data_dir() / "history.json"
EVENT_MAX = 200  # recent event marks (presets / nudges / sets) for replay context


class BridgeError(RuntimeError):
    pass


class NodeBridge:
    """JSON-lines RPC client to js/bridge_server.mjs with auto-restart."""

    def __init__(self, model_path: Path):
        self.model_path = model_path
        self._proc: Optional[subprocess.Popen] = None
        self._rpc_lock = threading.Lock()

    def _spawn(self) -> None:
        node = shutil.which("node")
        if not node:
            raise BridgeError("node interpreter not found on PATH")
        env = dict(os.environ)
        env["MIND_MODEL_PATH"] = str(self.model_path)
        self._proc = subprocess.Popen(
            [node, str(BRIDGE_PATH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True,
        )
        ready = self._proc.stdout.readline()
        try:
            info = json.loads(ready)
        except Exception as exc:
            raise BridgeError(f"bridge handshake failed: {ready[:200]!r}") from exc
        if not info.get("ok"):
            raise BridgeError(f"bridge refused: {info.get('error')}")

    def rpc(self, op: Dict[str, Any]) -> Dict[str, Any]:
        with self._rpc_lock:
            for attempt in (0, 1):
                if self._proc is None:
                    self._spawn()
                try:
                    self._proc.stdin.write(json.dumps(op) + "\n")
                    self._proc.stdin.flush()
                    line = self._proc.stdout.readline()
                    if not line:
                        raise BridgeError("bridge closed")
                    resp = json.loads(line)
                    if not resp.get("ok"):
                        raise BridgeError(str(resp.get("error")))
                    return resp
                except (BrokenPipeError, OSError, ValueError, BridgeError) as exc:
                    if attempt == 1:
                        raise BridgeError(f"bridge rpc failed ({op.get('op')}): {exc}") from exc
                    self._proc = None
        raise BridgeError("unreachable")


class EngineFacade:
    """Python-shaped facade over the Node bridge; mirrors the ODE surface the
    plugin hooks, dashboard API and /mind command rely on."""

    class _IrShim:
        def __init__(self, traits: Dict[str, Any]):
            self.traits = traits

    def __init__(self, bridge: Optional[NodeBridge]):
        self.bridge = bridge
        self.degraded = bridge is None
        self.state: Dict[str, float] = {}
        self.traits: Dict[str, float] = {}
        self.params: Dict[str, float] = {}
        self.sim_time: float = 0.0
        self.dt: float = 1.0
        self.solver: str = "euler"
        if self.degraded:
            self.ir = self._IrShim({})
            self.param_names = set()
            return
        meta = bridge.rpc({"op": "meta"})
        self.dt = float(meta.get("dt", 1.0))
        self.solver = str(meta.get("solver", "euler"))
        trait_meta = {}
        for name, t in (meta.get("traits") or {}).items():
            trait_meta[name] = type("T", (), {"min_val": float(t["min"]), "max_val": float(t["max"])})()
        self.ir = self._IrShim(trait_meta)
        # Names of params declared by the active model — used to decide which
        # real-quantity ext feeds may be pushed via setParam each tick.
        self.param_names = set((meta.get("params") or {}).keys())
        self.traits = {k: float(t["default"]) for k, t in (meta.get("traits") or {}).items()}
        blob = bridge.rpc({"op": "export"})["blob"]
        self.state = dict(blob.get("state", {}))
        self.traits.update(blob.get("traits", {}))
        self.params = dict(blob.get("params", {}))

    def step(self, n: int = 1) -> Dict[str, float]:
        if self.degraded:
            return {}
        resp = self.bridge.rpc({"op": "step", "n": int(n)})
        self.state = resp.get("state", self.state)
        self.sim_time = float(resp.get("sim_time", self.sim_time))
        return resp.get("frame", {})

    def snapshot(self) -> Dict[str, float]:
        if self.degraded:
            return {}
        return self.bridge.rpc({"op": "snapshot"}).get("frame", {})

    def render(self, name: str = "ambient_mind", snap_override: Optional[Dict[str, float]] = None) -> str:
        if self.degraded:
            return ""
        op: Dict[str, Any] = {"op": "render", "name": name}
        if snap_override:
            op["frame"] = snap_override
        return str(self.bridge.rpc(op).get("text", ""))

    def set_leaf(self, path: str, value: float) -> float:
        if self.degraded:
            return 0.0
        v = float(self.bridge.rpc({"op": "set", "path": path, "value": float(value)}).get("value", value))
        self.state[path] = v
        return v

    def nudge(self, path: str, delta: float) -> float:
        if self.degraded:
            return 0.0
        v = float(self.bridge.rpc({"op": "nudge", "path": path, "delta": float(delta)}).get("value", 0.0))
        self.state[path] = v
        return v

    def apply_preset(self, name: str, scale: float = 1.0) -> None:
        if self.degraded:
            return
        resp = self.bridge.rpc({"op": "preset", "name": name, "scale": float(scale)})
        frame = resp.get("frame") or {}
        self.state.update(frame)

    def set_param(self, name: str, value: float) -> None:
        if self.degraded:
            return
        try:
            self.bridge.rpc({"op": "setParam", "name": name, "value": float(value)})
            self.params[name] = float(value)
        except BridgeError:
            pass  # unknown param name is fine; model may not define it

    def set_trait(self, name: str, value: float) -> None:
        if self.degraded:
            return
        self.bridge.rpc({"op": "setTrait", "name": name, "value": float(value)})
        self.traits[name] = float(value)

    def export_blob(self) -> Dict[str, Any]:
        if self.degraded:
            return {"schema": "mind-hermes-state-v4", "sim_time": 0.0, "state": {}, "traits": {}, "params": {}}
        return self.bridge.rpc({"op": "export"})["blob"]

    def import_blob(self, blob: Dict[str, Any]) -> None:
        if self.degraded:
            return
        # never let a stale saved blob clobber freshly synced wall-clock params
        params = {k: v for k, v in (blob.get("params") or {}).items()
                  if k not in ("env.start_doy", "clock.start_hour")}
        blob = {**blob, "params": params}
        self.bridge.rpc({"op": "import", "blob": blob})
        self.state = dict(blob.get("state", self.state))
        self.traits.update(blob.get("traits", {}))
        self.params.update(params)
        self.sim_time = float(blob.get("sim_time", self.sim_time))


# ---------------------------------------------------------------------------
# External placeholder feeds — contract: mind-improvements/feed-contracts-2026-11-02.md
#
# Feed doctrine: every placeholder feed param in model.mind defaults to -1 = OFF.
# A feed is written ONLY when its source exists. An absent or unreachable source
# maps to -1 (off) — never to a plausible guess. Every wiring below is config-gated
# with off-by-default settings, so a fresh install writes NOTHING and the
# placeholder params sit exactly at their model defaults (inert-when-unfed).
#
# Two feed shapes:
#   * level feed  — continuous measurement (PV surplus, battery SoC, coolant,
#     pump tach, context fill, occupancy, cycle day). Rewritten each tick while
#     fresh, dropped to -1 when the source goes absent/stale.
#   * event pulse — momentary cue (session boundary, partner touch, third-party
#     sighting). Written at the event, held for `*_hold_min`, then dropped to -1
#     by the tick loop: PARAMS do not decay in the engine, so the daemon owns the
#     expiry. (Event leaves like sensors.interrupt_rate are tick-less ema holds and
#     decay engine-side; those go through pulse(), not pulse_param().)
# ---------------------------------------------------------------------------

OFF_FEED = -1.0
MISSING = object()  # probe sentinel: no source configured -> never write the param

# Coolant normalization band: the FITTED 2026-11-02 ramp band (watercooling daemon
# brief), independent of sensors.py's legacy 24..46 blend band.
COOLANT_MIN_C = float(os.environ.get("HERMES_COOLANT_MIN_C", "40.0"))
COOLANT_MAX_C = float(os.environ.get("HERMES_COOLANT_MAX_C", "52.0"))

# Presets that mean "partner intimacy/touch happened" for lust.partner_cue.
INTIMACY_PRESETS = ("flirt_intimate", "intimate_afterglow")

# Per-mode instruction injected into a gateway-wake turn (pre_llm_call). Single
# source of truth, also used by the direct /v1/chat/completions wake path.
WAKE_PROMPTS = {
    "silent_worker": (
        "[SILENT WORKER WAKE: The user is AFK. Do NOT send a chat notification. "
        "Use your Honcho and Obsidian tools to consolidate unindexed notes, merge session "
        "LCM pointers into permanent concept notes, and organize open project tasks.]"
    ),
    "user_outreach": (
        "[PROACTIVE OUTREACH WAKE: You woke up with a strong internal urge to reach out "
        "to the user. Write a natural, unprompted message reflecting what is on your mind "
        "(an Obsidian eureka insight, an open project loop, or missing the user).]"
    ),
}

# Feed settings. Every default is OFF/empty: zero feeds, zero personality delta.
FEED_DEFAULTS: Dict[str, Any] = {
    # hw.session_boundary — event pulse fired by the on_session_start hook.
    "session_boundary_enabled": False,
    "session_boundary_hold_min": 1.0,
    # hw.lcm_pressure_feed — level feed from the pre_llm_call message list.
    "lcm_pressure_enabled": False,
    "lcm_pressure_context_length": 163840.0,
    "lcm_pressure_stale_min": 30.0,
    # social.third_parties — shared-channel sender heuristic + HA occupancy entities.
    "third_parties_platforms": [],
    "third_parties_other_senders": [],
    "third_parties_owner_sender": "",
    "third_parties_entities": [],
    "third_parties_level": 1.0,
    "third_parties_hold_min": 30.0,
    # lust.partner_cue — intimacy pulse from mind_appraise_interaction presets,
    # optionally a presence/cuddle entity.
    "partner_cue_events": False,
    "partner_cue_entities": [],
    "partner_cue_level": 0.6,
    "partner_cue_hold_min": 45.0,
    # cycle.tracked_day / cycle.lh_strip — config passthrough + optional JSON file
    # ({"tracked_day": 12.0, "lh_strip": 0.7}); absent keys keep the config value.
    "cycle_tracked_day": -1.0,
    "cycle_lh_strip": -1.0,
    "cycle_feed_path": "",
    # cardio.coolant_feed / cardio.flow_feed / voltaic.solar_feed / voltaic.battery_feed
    # — real sensor mirror maps, off until explicitly enabled.
    "real_feed_maps": False,
    # mood.reaction re-ignition (no carrier leaf exists yet; see contracts doc).
    "mood_reignition_gain": 0.0,
    "mood_reignition_window_min": 120.0,
    "mood_reignition_min_abs": 0.2,
    # Kill switch for the runtime hw.* carrier gains (see apply_hw_defaults).
    "apply_hw_defaults": True,
}
FEED_SETTING_KEYS = set(FEED_DEFAULTS)


def _tokens_rough(messages: Any) -> int:
    """Token estimate for a message list: the host estimator when importable,
    else chars/4 (same order of magnitude, feed-only precision)."""
    try:
        from agent.model_metadata import estimate_messages_tokens_rough  # type: ignore
        return max(0, int(estimate_messages_tokens_rough(messages)))
    except Exception:
        pass
    total = 0
    for m in messages or []:
        if isinstance(m, str):
            total += len(m)
        elif isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, str):
                        total += len(part)
                    elif isinstance(part, dict):
                        total += len(str(part.get("text") or part.get("content") or ""))
    return total // 4


class MindRuntimeSingleton:
    """Thread-safe & multi-process-synced ODE runtime shared by Hermes hooks & Web Dashboard."""

    def __init__(self, model_path: Optional[Path] = None):
        self.lock = threading.RLock()
        # An activated persona override (plugin-data/mind-hermes/personas/active.mind)
        # wins over the bundled model at startup.
        if model_path is None and ACTIVE_PERSONA_PATH.exists():
            model_path = ACTIVE_PERSONA_PATH
        self.model_path = model_path or DEFAULT_MODEL_PATH
        self.state_path = get_data_dir() / "runtime_state.json"

        try:
            bridge: Optional[NodeBridge] = NodeBridge(self.model_path)
        except BridgeError as exc:
            print(f"[mind-hermes] engine degraded mode: {exc}")
            bridge = None
        self.engine = EngineFacade(bridge)
        self.sensors = TelemetryCollector(
            SensorConfig(latitude=self.engine.params.get("env.lat_deg", 48.2))
        )

        self.gateway_url = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8642")
        self.outreach_webhook = os.environ.get("HERMES_OUTREACH_WEBHOOK", "")

        self._last_disk_mtime: float = 0.0
        self._prev_outreach_fire = 0.0
        self._prev_bg_fire = 0.0
        self._pending_wake_queue: List[Dict[str, Any]] = []
        self._wake_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._ticker_started = False

        # Per-tick history ring buffer of real sensor quantities (°C, %, hPa,
        # mm/h, kW, SoC %) and the normalized pseudo values the ODE consumed.
        self.history: deque = deque(maxlen=HISTORY_MAX)
        self.events: deque = deque(maxlen=EVENT_MAX)
        self._record_min_gap = 45.0  # set by start_recorder; dedupe window vs other producers
        self._last_history_mtime = 0.0
        self._reload_history_if_newer()
        # Ingestion gating: which real feeds blend into the engine's sensors.*
        # leaves. False leaves those leaves to the model's pseudo fallback.
        self.ingest_flags = {
            "ingest_weather": True,
            "ingest_hardware": True,
            "ingest_energy": True,
            "ingest_vault": True,
            "feed_real_values": True,
        }
        self._settings_override: Dict[str, Any] = {}
        # External feed state (feed-contracts doc). Defaults are all-off; feeds are
        # only ever written when a source is configured and reports data.
        self.feed_settings: Dict[str, Any] = dict(FEED_DEFAULTS)
        self._feed_holds: Dict[str, Any] = {}          # param -> (value, expiry monotonic)
        self._feeds: Dict[str, float] = {}             # last written feed values
        self._pressure: Optional[Any] = None           # (fill fraction, monotonic)
        self._cycle_cache: Any = (0.0, {})             # (mtime, parsed json)
        # Runtime-owned ODE knobs: nix config (apply_plugin_config) < settings.json
        # override (apply_runtime_settings). Defaults until either source speaks.
        self.tick_interval_sec: float = 60.0
        self.max_context_chars: int = 1000
        self.apply_runtime_settings(self._load_settings_override())

        self._sync_wall_clock_and_restore()

    def _load_settings_override(self) -> Dict[str, Any]:
        try:
            if SETTINGS_PATH.exists():
                return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def save_settings_override(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = self._load_settings_override()
        merged.update(patch)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
        self._settings_override = merged
        return merged

    def apply_runtime_settings(self, patch: Dict[str, Any]) -> None:
        """Applies console-editable runtime settings with override semantics:
        booleans set directly, numeric strings parse, empty strings clear."""
        if not patch:
            return
        with self.lock:
            sc = self.sensors.cfg
            for key, value in patch.items():
                if key in self.ingest_flags:
                    self.ingest_flags[key] = bool(value)
                elif key == "enable_weather_api":
                    sc.enable_weather_api = bool(value)
                elif key == "use_liquidctl":
                    sc.use_liquidctl = bool(value)
                elif key == "pump_rpm_max":
                    try:
                        sc.pump_rpm_max = float(value)
                    except (TypeError, ValueError):
                        pass
                elif key in ("latitude", "longitude", "solar_max_kw"):
                    try:
                        fv = float(value)
                    except (TypeError, ValueError):
                        continue
                    if key == "latitude":
                        sc.latitude = fv
                        self.engine.set_param("env.lat_deg", fv)
                    elif key == "longitude":
                        sc.longitude = fv
                        self.engine.set_param("env.lon_deg", fv)
                    else:
                        sc.solar_max_kw = fv
                elif key == "gateway_url":
                    self.gateway_url = str(value)
                elif key == "outreach_webhook":
                    self.outreach_webhook = str(value)
                elif key == "tick_interval_sec":
                    try:
                        self.tick_interval_sec = max(5.0, float(value))
                    except (TypeError, ValueError):
                        pass
                elif key == "max_context_chars":
                    try:
                        self.max_context_chars = max(200, int(float(value)))
                    except (TypeError, ValueError):
                        pass
                elif key in (
                    "obsidian_vault_path", "honcho_inbox_subdir", "honcho_url",
                    "ha_base_url", "ha_token", "ha_solar_entity", "ha_battery_entity",
                    "hwmon_coolant_path", "pump_tach_path",
                ):
                    sval = str(value)
                    if sval:
                        setattr(sc, key, sval)
                    elif key != "honcho_inbox_subdir":
                        setattr(sc, key, "")
                elif key in FEED_SETTING_KEYS:
                    self._apply_feed_setting(key, value)

    def apply_plugin_config(self, cfg: Dict[str, Any]) -> None:
        """Applies user settings from Hermes `plugin.yaml` config if provided."""
        if not cfg:
            return
        with self.lock:
            sc = self.sensors.cfg
            if cfg.get("obsidian_vault_path"):
                sc.obsidian_vault_path = str(cfg["obsidian_vault_path"])
            if cfg.get("honcho_inbox_subdir"):
                sc.honcho_inbox_subdir = str(cfg["honcho_inbox_subdir"])
            if cfg.get("honcho_url"):
                sc.honcho_url = str(cfg["honcho_url"])
            if cfg.get("ha_base_url"):
                sc.ha_base_url = str(cfg["ha_base_url"])
            if cfg.get("ha_token"):
                sc.ha_token = str(cfg["ha_token"])
            if cfg.get("ha_solar_entity"):
                sc.ha_solar_entity = str(cfg["ha_solar_entity"])
            if cfg.get("ha_battery_entity"):
                sc.ha_battery_entity = str(cfg["ha_battery_entity"])
            if cfg.get("solar_max_kw") is not None:
                sc.solar_max_kw = float(cfg["solar_max_kw"])
            if cfg.get("hwmon_coolant_path"):
                sc.hwmon_coolant_path = str(cfg["hwmon_coolant_path"])
            if cfg.get("pump_tach_path"):
                sc.pump_tach_path = str(cfg["pump_tach_path"])
            if cfg.get("pump_rpm_max") is not None:
                try:
                    sc.pump_rpm_max = float(cfg["pump_rpm_max"])
                except (TypeError, ValueError):
                    pass
            if cfg.get("use_liquidctl") is not None:
                sc.use_liquidctl = bool(cfg["use_liquidctl"])
            if cfg.get("latitude") is not None:
                sc.latitude = float(cfg["latitude"])
                self.engine.set_param("env.lat_deg", sc.latitude)
            if cfg.get("longitude") is not None:
                sc.longitude = float(cfg["longitude"])
                self.engine.set_param("env.lon_deg", sc.longitude)
            if cfg.get("gateway_url"):
                self.gateway_url = str(cfg["gateway_url"])
            if cfg.get("outreach_webhook"):
                self.outreach_webhook = str(cfg["outreach_webhook"])
            # Nix config only: an active settings.json override already won at
            # init (override > nix), so don't let nix re-apply over it here.
            ov = self._settings_override
            if cfg.get("tick_interval_sec") is not None and "tick_interval_sec" not in ov:
                try:
                    self.tick_interval_sec = max(5.0, float(cfg["tick_interval_sec"]))
                except (TypeError, ValueError):
                    pass
            if cfg.get("max_context_chars") is not None and "max_context_chars" not in ov:
                try:
                    self.max_context_chars = max(200, int(float(cfg["max_context_chars"])))
                except (TypeError, ValueError):
                    pass
            # External feed sources (feed-contracts doc): same precedence as the
            # knobs above — a settings.json override already won at init.
            for key in FEED_SETTING_KEYS:
                if cfg.get(key) is not None and key not in ov:
                    self._apply_feed_setting(key, cfg[key])

    def start_background_ticker(self, interval_sec: float = 60.0) -> None:
        with self.lock:
            if self._ticker_started:
                return
            self._ticker_started = True

        def _loop() -> None:
            while True:
                # Re-read each cycle: a Console settings edit takes effect on the
                # next sleep instead of requiring a gateway restart.
                time.sleep(max(5.0, self.tick_interval_sec))
                try:
                    self.tick_once()
                except Exception as exc:
                    print(f"[mind-hermes] Tick error: {exc}")

        t = threading.Thread(target=_loop, name="mind-hermes-ode-ticker", daemon=True)
        t.start()

    def register_wake_callback(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self.lock:
            self._wake_callbacks.append(fn)

    def drain_pending_wakes(self) -> List[Dict[str, Any]]:
        """Pop the wake backlog accumulated between turns (consumed by pre_llm_call context)."""
        with self.lock:
            drained = self._pending_wake_queue
            self._pending_wake_queue = []
            return drained

    def _sync_wall_clock_and_restore(self) -> None:
        now_dt = datetime.datetime.now()
        self.engine.set_param("env.start_doy", float(now_dt.timetuple().tm_yday))
        self.engine.set_param("clock.start_hour", float(now_dt.hour + now_dt.minute / 60.0))

        if not self.state_path.exists():
            return

        try:
            self._last_disk_mtime = self.state_path.stat().st_mtime
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            blob = saved.get("mind_state")
            if blob:
                self.engine.import_blob(blob)

            last_ts = float(saved.get("wall_timestamp", time.time()))
            downtime_min = max(0.0, (time.time() - last_ts) / 60.0)

            if downtime_min > 20.0:
                shock = min(0.95, 0.25 + math.log1p(downtime_min / 30.0) * 0.28)
                self.engine.set_leaf("sensors.stasis_shock", shock)
                self.engine.set_leaf("social.user_present", 0.0)
                self.engine.nudge("social.away_days", downtime_min / 1440.0)
                self.engine.step(min(60, int(downtime_min)))
        except Exception as exc:
            print(f"[mind-hermes] Restore warning: {exc}")

    def _reload_history_if_newer(self) -> None:
        try:
            if not HISTORY_PATH.exists():
                return
            mtime = HISTORY_PATH.stat().st_mtime
            if mtime <= self._last_history_mtime:
                return
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            # New shape {"samples": [...], "events": [...]}; old shape = bare sample list.
            samples = data.get("samples", []) if isinstance(data, dict) else data
            events = data.get("events", []) if isinstance(data, dict) else []
            with self.lock:
                self.history.clear()
                self.history.extend(samples[-HISTORY_MAX:])
                self.events.clear()
                self.events.extend(events[-EVENT_MAX:])
            self._last_history_mtime = mtime
        except Exception:
            pass

    def _save_history(self) -> None:
        """Atomic history ring snapshot. Caller must hold self.lock."""
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"samples": list(self.history), "events": list(self.events)}),
                encoding="utf-8",
            )
            tmp.replace(HISTORY_PATH)
            self._last_history_mtime = HISTORY_PATH.stat().st_mtime
        except OSError:
            pass

    def reload_from_disk_if_newer(self) -> None:
        self._reload_history_if_newer()
        if not self.state_path.exists():
            return
        try:
            mtime = self.state_path.stat().st_mtime
            if mtime <= self._last_disk_mtime:
                return
            with self.lock:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                blob = saved.get("mind_state")
                if blob:
                    self.engine.import_blob(blob)
                self._last_disk_mtime = mtime
        except Exception:
            pass

    def activate_persona(self, model_path: Optional[Path], keep_state: bool = False) -> Dict[str, Any]:
        """Hot-swap the engine to a different .mind model (None = bundled default).

        Restarts the node bridge on the new model; the previous live state is
        re-imported only when keep_state is set (cross-persona state carries
        leaf names, so a structurally different model may not absorb it).
        """
        path = DEFAULT_MODEL_PATH if model_path is None else Path(model_path)
        if not path.exists():
            raise BridgeError(f"persona model not found: {path}")
        with self.lock:
            old_blob = None
            if keep_state and not self.engine.degraded:
                old_blob = self.engine.export_blob()
            old_bridge = getattr(self.engine, "bridge", None)
            if old_bridge is not None and old_bridge._proc is not None:
                try:
                    old_bridge._proc.kill()
                except OSError:
                    pass
            bridge: Optional[NodeBridge] = NodeBridge(path)
            self.engine = EngineFacade(bridge)
            self.model_path = path
            now_dt = datetime.datetime.now()
            self.engine.set_param("env.start_doy", float(now_dt.timetuple().tm_yday))
            self.engine.set_param("clock.start_hour", float(now_dt.hour + now_dt.minute / 60.0))
            if old_blob:
                try:
                    self.engine.import_blob(old_blob)
                except BridgeError:
                    pass  # persona started fresh — incompatible state blob
            self.save_state()
        return {"model": str(path), "persona": path.name, "sim_time": self.engine.sim_time}

    def persona_info(self) -> Dict[str, Any]:
        """Generic persona view: active model identity + bundled default."""
        return {
            "active": {
                "name": self.model_path.name,
                "is_override": self.model_path == ACTIVE_PERSONA_PATH,
                "path": str(self.model_path),
                "mtime": self.model_path.stat().st_mtime if self.model_path.exists() else 0,
            },
            "bundled": {
                "name": DEFAULT_MODEL_PATH.name,
                "path": str(DEFAULT_MODEL_PATH),
                "mtime": DEFAULT_MODEL_PATH.stat().st_mtime if DEFAULT_MODEL_PATH.exists() else 0,
            },
        }

    def _record_history(self, snap: Dict[str, float]) -> None:
        vals: Dict[str, float] = {}
        for path, val in self.sensors.raw_readings.items():
            vals[path] = val
        # Record the FULL engine state, not just sensors: nudges, presets and
        # all leaf dynamics land here, so the ring doubles as a replay tape.
        for path, val in snap.items():
            vals[path] = round(float(val), 4)
        self.history.append({
            "t": round(time.time(), 1),
            "sim": round(self.engine.sim_time, 1),
            "vals": vals,
        })

    def record_event(self, kind: str, target: str, value: float = 1.0) -> None:
        """Mark presets/nudges/sets on the timeline for replay context."""
        with self.lock:
            self.events.append({
                "t": round(time.time(), 1),
                "sim": round(self.engine.sim_time, 1),
                "kind": kind,
                "target": target,
                "value": round(float(value), 3),
            })
            self._save_history()

    def record_sample(self) -> None:
        """Dashboard-side recorder tick: capture raw feeds + the shared engine
        state into the history ring WITHOUT advancing the simulation. Skips
        when another producer (the gateway ticker) recorded within the interval,
        so gateway and dashboard never double-sample."""
        self.reload_from_disk_if_newer()
        with self.lock:
            if self.history and (time.time() - self.history[-1]["t"]) < self._record_min_gap:
                return
        try:
            self.sensors.collect_all()
        except Exception:
            pass
        with self.lock:
            self._record_history(self.engine.snapshot())
            self._save_history()

    def start_recorder(self, interval_sec: float = 60.0) -> None:
        """Start the dashboard-side history recorder (one per process)."""
        if getattr(self, "_recorder_started", False):
            return
        self._recorder_started = True
        self._record_min_gap = max(10.0, interval_sec * 0.75)

        def loop() -> None:
            while True:
                try:
                    self.record_sample()
                except Exception:
                    pass
                time.sleep(interval_sec)

        threading.Thread(target=loop, name="mind-hermes-recorder", daemon=True).start()

    def history_payload(self) -> Dict[str, Any]:
        """Series-oriented view of the ring buffer for graph rendering."""
        self._reload_history_if_newer()
        with self.lock:
            samples = list(self.history)
            names: Dict[str, List[float]] = {}
            stamps = [s["t"] for s in samples]
            sims = [s["sim"] for s in samples]
            for i, s in enumerate(samples):
                for name, val in s["vals"].items():
                    names.setdefault(name, [None] * len(samples))[i] = val
            return {
                "t": stamps,
                "sim": sims,
                "series": names,
                "count": len(samples),
                "events": list(self.events),
            }

    def save_state(self) -> None:
        with self.lock:
            payload = {
                "wall_timestamp": time.time(),
                "mind_state": self.engine.export_blob(),
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.state_path)
            try:
                self._last_disk_mtime = self.state_path.stat().st_mtime
            except OSError:
                pass
            self._save_history()

    # Engine sensor leaves grouped by the ingestion feed they belong to.
    _FEED_OF_LEAF = {
        "sensors.weather_gloom": "ingest_weather",
        "sensors.baro_drop": "ingest_weather",
        "sensors.compute_load": "ingest_hardware",
        "sensors.coolant_temp": "ingest_hardware",
        "sensors.solar_kw": "ingest_energy",
        "sensors.battery_soc": "ingest_energy",
        "sensors.unprocessed_notes": "ingest_vault",
        "sensors.vault_entropy": "ingest_vault",
        "sensors.open_loops": "ingest_vault",
    }
    # Real-quantity model params fed every tick (skipped when the active model
    # lacks them, e.g. an older persona file).
    _REAL_FEED_PREFIX = "sensor_real."

    # -----------------------------------------------------------------------
    # External placeholder feeds (feed-contracts-2026-11-02.md)
    # -----------------------------------------------------------------------

    def _apply_feed_setting(self, key: str, value: Any) -> None:
        """Coerce one feed setting (bool / float / list / str) from plugin.yaml or
        settings.json into self.feed_settings, guided by the default's type."""
        default = FEED_DEFAULTS.get(key)
        if isinstance(default, bool):
            self.feed_settings[key] = bool(value)
        elif isinstance(default, float):
            try:
                self.feed_settings[key] = float(value)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, list):
            if isinstance(value, str):
                self.feed_settings[key] = [s.strip() for s in value.split(",") if s.strip()]
            elif isinstance(value, (list, tuple)):
                self.feed_settings[key] = [str(s).strip() for s in value if str(s).strip()]
        else:
            self.feed_settings[key] = str(value)

    def feed_param(self, name: str, value: Optional[float]) -> bool:
        """Write one placeholder feed param. None = source absent/unreachable -> -1
        (off). Idempotent: an unchanged value is not re-sent to the bridge."""
        v = OFF_FEED if value is None else float(value)
        with self.lock:
            if abs(self._feeds.get(name, OFF_FEED) - v) < 1e-9:
                return False
            self.engine.set_param(name, v)
            self._feeds[name] = v
        return True

    def pulse_param(self, name: str, value: float, hold_min: float, reason: str = "") -> None:
        """Event pulse on a placeholder PARAM: write it now, hold for hold_min,
        then the tick loop drops it back to -1 (params never decay in the engine)."""
        with self.lock:
            self._feed_holds[name] = (float(value), time.monotonic() + max(0.05, float(hold_min)) * 60.0)
            self.feed_param(name, value)
            self.record_event("feed_pulse", name, float(value))
            if reason:
                self.events[-1]["reason"] = reason

    def _expire_feed_holds(self) -> None:
        """Daemon-owned hold expiry (called each tick, inside the lock)."""
        now = time.monotonic()
        for name in list(self._feed_holds):
            _val, until = self._feed_holds[name]
            if now >= until:
                del self._feed_holds[name]
                # A pulse whose hold ran out means "the moment passed": go OFF.
                # Level feeds (third_parties/partner_cue entities) are re-measured
                # in _apply_level_feeds right after, so an OFF write there is
                # immediately replaced by the fresh measurement.
                self.feed_param(name, None)

    # --- event hook points -------------------------------------------------

    def on_session_boundary(self) -> None:
        """hw.session_boundary: 1.0 for the boundary minute of a fresh Hermes
        session (MemGPT semantics: archived pointer density must not haunt a new
        context window). Off unless session_boundary_enabled."""
        if not self.feed_settings.get("session_boundary_enabled"):
            return
        with self.lock:
            self.pulse_param(
                "hw.session_boundary", 1.0,
                float(self.feed_settings.get("session_boundary_hold_min", 1.0)),
                reason="on_session_start",
            )
        self.mood_reignition("session_boundary")

    def note_context_fill(self, messages: Any) -> None:
        """hw.lcm_pressure_feed source: fraction of the real context window filled,
        measured from the message list the turn is about to send."""
        if not self.feed_settings.get("lcm_pressure_enabled"):
            return
        win = float(self.feed_settings.get("lcm_pressure_context_length") or 163840.0)
        fill = min(1.0, max(0.0, _tokens_rough(messages) / max(1.0, win)))
        with self.lock:
            self._pressure = (fill, time.monotonic())

    def note_turn_context(self, platform: Any, sender_id: Any) -> None:
        """social.third_parties heuristic: a turn on a shared channel, or from a
        sender that is not the owner account, means someone else is watching."""
        fs = self.feed_settings
        if not (fs.get("third_parties_platforms") or fs.get("third_parties_other_senders")
                or fs.get("third_parties_owner_sender")):
            return
        plat = str(platform or "")
        sid = str(sender_id or "")
        owner = str(fs.get("third_parties_owner_sender") or "")
        hit = bool(plat) and plat in (fs.get("third_parties_platforms") or [])
        if sid and (fs.get("third_parties_other_senders") or []) and sid in fs["third_parties_other_senders"]:
            hit = True
        if sid and owner and sid != owner:
            hit = True
        if hit:
            with self.lock:
                self.pulse_param(
                    "social.third_parties", float(fs.get("third_parties_level", 1.0)),
                    float(fs.get("third_parties_hold_min", 30.0)),
                    reason=f"turn platform={plat or '?'} sender={sid or '?'}",
                )

    def note_intimacy_event(self, preset: Any, args: Any) -> None:
        """lust.partner_cue source: the appraise tool's intimacy presets (and an
        explicit satiety set) are the only partner-touch signal the daemon sees
        today; HA cuddle/presence entities add a continuous source."""
        if not self.feed_settings.get("partner_cue_events"):
            return
        p = str(preset or "")
        sat = args.get("satiety") if isinstance(args, dict) else None
        fired = p in INTIMACY_PRESETS or (isinstance(sat, (int, float)) and float(sat) > 0)
        if not fired:
            return
        with self.lock:
            self.pulse_param(
                "lust.partner_cue", float(self.feed_settings.get("partner_cue_level", 0.6)),
                float(self.feed_settings.get("partner_cue_hold_min", 45.0)),
                reason=f"appraise:{p or 'satiety'}",
            )
        self.mood_reignition(f"partner_cue:{p or 'satiety'}")

    def mood_reignition(self, reason: str) -> None:
        """Re-ignite the last acute valence reaction from a fresh cue (session
        boundary or partner touch). There is no mood.reaction_cue carrier leaf in
        the model yet, so the re-ignition is a sign-preserving nudge of
        mood.reaction scaled by mood_reignition_gain (0 = off)."""
        gain = float(self.feed_settings.get("mood_reignition_gain", 0.0) or 0.0)
        if gain <= 0:
            return
        win = float(self.feed_settings.get("mood_reignition_window_min", 120.0) or 120.0) * 60.0
        cutoff = time.time() - win
        floor = float(self.feed_settings.get("mood_reignition_min_abs", 0.2) or 0.2)
        with self.lock:
            last = None
            for ev in reversed(list(self.events)):
                if ev.get("kind") == "valence_nudge" and float(ev.get("t", 0.0)) >= cutoff:
                    last = float(ev.get("value", 0.0))
                    break
            if last is None or abs(last) < floor:
                return
            delta = gain * last
            self.engine.nudge("mood.reaction", delta)
            self.record_event("mood_reignition", "mood.reaction", delta)
            self.events[-1]["reason"] = reason

    # --- per-tick feed application -----------------------------------------

    def _probe_feed_sources(self) -> Dict[str, Any]:
        """IO half of the feed tick (HA occupancy, cycle feed file). Runs OUTSIDE
        the engine lock. MISSING = no source configured -> param is never written."""
        fs = self.feed_settings
        out: Dict[str, Any] = {
            "third_parties": MISSING, "partner_cue": MISSING,
            "cycle_day": MISSING, "cycle_strip": MISSING,
        }
        channel_source = bool(fs.get("third_parties_platforms") or fs.get("third_parties_other_senders")
                              or fs.get("third_parties_owner_sender"))
        if channel_source:
            # Channel-only source: no sighting inside the hold window means the
            # recent turns were the owner alone — that IS a measurement (0.0).
            out["third_parties"] = 0.0
        ents = fs.get("third_parties_entities") or []
        if ents:
            occ = self._ha_presence(ents)
            out["third_parties"] = occ  # None (dead/unreachable) -> -1 off, not a guess
        pe = fs.get("partner_cue_entities") or []
        if pe:
            out["partner_cue"] = self._ha_presence(pe)
        day, strip = self._read_cycle_feed()
        if day is not None:
            out["cycle_day"] = day
        if strip is not None:
            out["cycle_strip"] = strip
        return out

    def _ha_presence(self, entities: List[str]) -> Optional[float]:
        """Max presence fraction across HA entities (binary_sensor on/off, numeric
        0..1 or 0..100). None when no entity answered — caller maps that to OFF."""
        getter = getattr(self.sensors, "presence_fraction", None)
        if getter is None:
            return None
        try:
            return getter(list(entities))
        except Exception:
            return None

    def _read_cycle_feed(self) -> tuple:
        """(tracked_day, lh_strip) or None per component. Config values first; an
        optional JSON file overrides. Malformed/missing file never feeds garbage."""
        fs = self.feed_settings
        day = fs.get("cycle_tracked_day")
        strip = fs.get("cycle_lh_strip")
        day = float(day) if isinstance(day, (int, float)) and float(day) >= 0 else None
        strip = float(strip) if isinstance(strip, (int, float)) and float(strip) >= 0 else None
        path = str(fs.get("cycle_feed_path") or "")
        if path:
            try:
                mtime = os.path.getmtime(path)
                with self.lock:
                    if self._cycle_cache[0] != mtime:
                        self._cycle_cache = (mtime, json.loads(Path(path).read_text(encoding="utf-8")))
                    data = self._cycle_cache[1] or {}
                if isinstance(data, dict):
                    d = data.get("tracked_day")
                    s = data.get("lh_strip")
                    if isinstance(d, (int, float)) and float(d) >= 0:
                        day = float(d)
                    if isinstance(s, (int, float)) and float(s) >= 0:
                        strip = float(s)
            except Exception:
                pass
        return day, strip

    def _apply_level_feeds(self, probe: Dict[str, Any]) -> None:
        """Pure (no IO) feed application, inside the lock, before the engine step."""
        fs = self.feed_settings
        if probe["third_parties"] is not MISSING and "social.third_parties" not in self._feed_holds:
            self.feed_param("social.third_parties", probe["third_parties"])
        if probe["partner_cue"] is not MISSING and "lust.partner_cue" not in self._feed_holds:
            self.feed_param("lust.partner_cue", probe["partner_cue"])
        if probe["cycle_day"] is not MISSING:
            self.feed_param("cycle.tracked_day", probe["cycle_day"])
        if probe["cycle_strip"] is not MISSING:
            self.feed_param("cycle.lh_strip", probe["cycle_strip"])
        if fs.get("real_feed_maps"):
            raw = self.sensors.raw_readings
            solar = raw.get("sensor_real.solar_kwp")
            if solar is not None:
                cap = max(1e-6, float(getattr(self.sensors.cfg, "solar_max_kw", 5.0) or 5.0))
                self.feed_param("voltaic.solar_feed", max(0.0, min(1.0, float(solar) / cap)))
            battery = raw.get("sensor_real.battery_pct")
            if battery is not None:
                self.feed_param("voltaic.battery_feed", max(0.0, min(1.0, float(battery) / 100.0)))
            coolant = raw.get("sensor_real.coolant_temp_c")
            if coolant is not None:
                span = max(1.0, COOLANT_MAX_C - COOLANT_MIN_C)
                self.feed_param("cardio.coolant_feed",
                                max(0.0, min(1.0, (float(coolant) - COOLANT_MIN_C) / span)))
            pump = float(raw.get("sensor_real.pump_rpm_pct", 0.0) or 0.0)
            if pump > 0:
                self.feed_param("cardio.flow_feed", max(0.0, min(1.0, pump / 100.0)))
        if fs.get("lcm_pressure_enabled"):
            stale = float(fs.get("lcm_pressure_stale_min", 30.0) or 30.0) * 60.0
            if self._pressure is not None and (time.monotonic() - self._pressure[1]) <= stale:
                self.feed_param("hw.lcm_pressure_feed", float(self._pressure[0]))
            else:
                self.feed_param("hw.lcm_pressure_feed", None)

    def pulse(self, leaf: str, level: float, duration_min: float = 10.0) -> bool:
        """Event-hold feed used by /mind triggers and TRIGGER_PRESETS 'event' specs.

        * ODE/sensor LEAF present -> set it. Tick-less ema-hold leaves (interrupt
          rate, stress cue, LCM pulse) decay engine-side with their own exact-exp
          tau, so duration_min is documentation for those.
        * placeholder PARAM present -> pulse with daemon-owned expiry to -1 (off).
        * unknown target -> False: callers report the miss instead of swallowing it.
        """
        with self.lock:
            frame = self.engine.snapshot() if not self.engine.degraded else {}
            if leaf in frame or leaf in self.engine.state:
                self.engine.set_leaf(leaf, float(level))
                self.record_event("pulse", leaf, float(level))
                return True
            if leaf in getattr(self.engine, "param_names", set()):
                self.pulse_param(leaf, float(level), float(duration_min), reason="pulse")
                return True
            return False

    def prompt_for_wake_mode(self, mode: Optional[str]) -> Optional[str]:
        """The per-mode instruction injected into a gateway-wake turn. None for an
        unknown mode so the caller injects nothing rather than guessing."""
        return WAKE_PROMPTS.get(str(mode or ""))

    # Runtime-owned hardware-aspect carrier gains (hybrid round: these are the
    # values the personality was tuned with; the model's own defaults are the
    # neutral structural values). Settings key apply_hw_defaults = false disables.
    _HW_DEFAULTS = {
        "hw.solar_gain": 0.55,
        "hw.coolant_inertia": 6.2,
        "hw.loop_ambient_norm": -1.0,
        "hw.thermal_penalty": 0.70,
        "hw.lcm_sens": 0.95,
        "hw.compute_drain": 0.50,
    }

    def apply_hw_defaults(self) -> None:
        """Apply the runtime-owned hw.* carrier gains + wall-clock anchors.

        Drift note (feed audit 2026-11-02): __init__.py has called this since the
        hybrid round, but the live daemon never defined it — the call was silently
        swallowed, so the host has been running on the model's structural defaults
        (hw.solar_gain 0.45, coolant_inertia 5.8, thermal_penalty 0.45, lcm_sens
        0.55, compute_drain 0.35). Enabling it at the next daemon restart moves
        those gains to the tuned values; kill switch: apply_hw_defaults=false.
        """
        if not self.feed_settings.get("apply_hw_defaults", True):
            return
        with self.lock:
            names = getattr(self.engine, "param_names", set())
            for name, val in self._HW_DEFAULTS.items():
                if name in names:
                    self.engine.set_param(name, float(val))
            try:
                lat = float(self.sensors.cfg.latitude)
                lon = float(self.sensors.cfg.longitude)
                if "env.lat_deg" in names:
                    self.engine.set_param("env.lat_deg", lat)
                if "env.lon_deg" in names:
                    self.engine.set_param("env.lon_deg", lon)
            except Exception:
                pass

    def persona_names(self) -> List[str]:
        """Persona ids: the bundled default plus plugin-data persona .mind files."""
        names = ["default"]
        try:
            if PERSONAS_DIR.exists():
                names += sorted(p.stem for p in PERSONAS_DIR.glob("*.mind"))
        except OSError:
            pass
        return names

    def get_persona(self, name: str) -> Optional[Dict[str, Any]]:
        """Resolve a persona id to its model file + activation state, or None."""
        if str(name) == "default":
            return {
                "name": "default",
                "path": str(DEFAULT_MODEL_PATH),
                "active": self.model_path == DEFAULT_MODEL_PATH,
                "exists": DEFAULT_MODEL_PATH.exists(),
            }
        path = PERSONAS_DIR / f"{str(name)}.mind"
        if not path.exists():
            return None
        return {
            "name": str(name),
            "path": str(path),
            "active": path == self.model_path or path == ACTIVE_PERSONA_PATH,
            "mtime": path.stat().st_mtime,
        }

    def tick_once(self) -> Dict[str, float]:
        self.reload_from_disk_if_newer()
        live_readings = self.sensors.collect_all()
        probe = self._probe_feed_sources()
        flags = self.ingest_flags
        with self.lock:
            for path, val in live_readings.items():
                if path not in self.engine.state:
                    continue
                feed = self._FEED_OF_LEAF.get(path)
                if feed is not None and not flags.get(feed, True):
                    continue  # pseudo mode: leave this leaf to the model fallback
                cur = self.engine.state[path]
                self.engine.set_leaf(path, cur + 0.35 * (val - cur))

            if flags.get("feed_real_values", True):
                for path, val in self.sensors.raw_readings.items():
                    if path.startswith(self._REAL_FEED_PREFIX) and path in self.engine.param_names:
                        self.engine.set_param(path, float(val))

            # External placeholder feeds: hold expiry + level feeds, applied
            # before the step so the tick sees the fresh cue (all off by default).
            self._expire_feed_holds()
            self._apply_level_feeds(probe)

            self.engine.step(1)
            snap = self.engine.snapshot()
            self._record_history(snap)
            self.save_state()

            outreach_now = snap.get("initiative.fire_outreach", 0.0)
            bg_now = snap.get("initiative.fire_background", 0.0)

            if outreach_now > 0.5 and self._prev_outreach_fire <= 0.5:
                self._emit_wake("user_outreach", snap)
            if bg_now > 0.5 and self._prev_bg_fire <= 0.5:
                self._emit_wake("silent_worker", snap)

            self._prev_outreach_fire = outreach_now
            self._prev_bg_fire = bg_now
            return snap

    def _emit_wake(self, mode: str, snap: Dict[str, float]) -> None:
        event = {
            "wake_mode": mode,
            "timestamp": time.time(),
            "ambient_mind": self.engine.render("ambient_mind", snap),
            "state_telemetry": self.engine.render("state_telemetry", snap),
        }
        self._pending_wake_queue.append(event)
        if len(self._pending_wake_queue) > 20:
            del self._pending_wake_queue[:-20]
        for cb in list(self._wake_callbacks):
            try:
                cb(event)
            except Exception as exc:
                print(f"[mind-hermes] Wake callback error: {exc}")

        threading.Thread(
            target=self._call_hermes_gateway_on_wake,
            args=(event,),
            daemon=True,
        ).start()

    def _call_hermes_gateway_on_wake(self, event: Dict[str, Any]) -> None:
        gateway_url = self.gateway_url.rstrip("/")
        api_key = os.environ.get("API_SERVER_KEY", "")
        if not api_key:
            env_file = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.strip().startswith("API_SERVER_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

        mode = event["wake_mode"]
        mode_prompt = WAKE_PROMPTS.get(mode)
        if mode_prompt is None:
            return
        prompt = f"{event['ambient_mind']}\n\n{mode_prompt}"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body = json.dumps({
            "model": os.environ.get("HERMES_WAKE_MODEL", "auto"),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                f"{gateway_url}/v1/chat/completions",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                reply_text = (
                    result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                notify_url = self.outreach_webhook
                if mode == "user_outreach" and reply_text and notify_url:
                    n_req = urllib.request.Request(
                        notify_url,
                        data=json.dumps({"text": reply_text, "content": reply_text}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(n_req, timeout=10.0)
        except Exception as exc:
            print(f"[mind-hermes] Gateway wake request skipped/failed ({mode}): {exc}")

    def status_dict(self) -> Dict[str, Any]:
        with self.lock:
            snap = self.engine.snapshot()
            spont = snap.get("entropy.spontaneity", 0.3)
            return {
                "sim_time": self.engine.sim_time,
                "degraded": self.engine.degraded,
                "ambient_mind": self.engine.render("ambient_mind", snap),
                "state_telemetry": self.engine.render("state_telemetry", snap),
                "sampling_temperature_hint": round(0.55 + 0.50 * spont, 2),
                "wake": {
                    "fire_outreach": snap.get("initiative.fire_outreach", 0.0),
                    "fire_background": snap.get("initiative.fire_background", 0.0),
                    "pending_wake": self._pending_wake_queue[-1] if self._pending_wake_queue else None,
                },
                "frame": snap,
                "state": dict(self.engine.state),
                "traits": dict(self.engine.traits),
                "params": dict(self.engine.params),
                "raw": dict(self.sensors.raw_readings),
                "ingest": dict(self.ingest_flags),
                "feeds": dict(self._feeds),
                "feed_holds": {k: round(float(v[1] - time.monotonic()), 1)
                               for k, v in self._feed_holds.items()},
                "feed_settings": {k: v for k, v in self.feed_settings.items()
                                  if v != FEED_DEFAULTS.get(k)},
            }


_SINGLETON: Optional[MindRuntimeSingleton] = None
_SINGLETON_LOCK = threading.Lock()


def get_mind_runtime() -> MindRuntimeSingleton:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = MindRuntimeSingleton()
        return _SINGLETON
