from __future__ import annotations
import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

# Ensure plugin root is importable even when loaded via `importlib.util.spec_from_file_location`
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# Bind the daemon module: normally the one already loaded in this process
# (the gateway imports it at plugin registration; reusing it keeps the dashboard
# and the agent on ONE mind runtime). Reuse is conditional on the loaded module
# actually providing what this plugin's API needs — a stale or foreign ``daemon``
# (sealed store copy, or another plugin's module of the same name) falls back to
# this plugin's sibling copy, which is version-consistent by construction.
_daemon = sys.modules.get("daemon")
if _daemon is None or not (
    hasattr(_daemon, "ACTIVE_PERSONA_PATH")
    and hasattr(_daemon, "MindRuntimeSingleton.activate_persona")
    and hasattr(_daemon, "MindRuntimeSingleton.apply_runtime_settings")
    and hasattr(_daemon, "MindRuntimeSingleton.history_payload")
    # feed wiring (feed-contracts-2026-11-02.md): a pre-round copy (sealed store)
    # lacks these and must not be reused by an API that whitelists feed settings.
    and hasattr(_daemon, "FEED_DEFAULTS")
    and hasattr(_daemon, "FEED_SETTING_KEYS")
    and hasattr(_daemon, "MindRuntimeSingleton.external_feed")
):
    import importlib.util as _ilu

    _daemon_spec = _ilu.spec_from_file_location("mind_hermes_daemon", _PLUGIN_ROOT / "daemon.py")
    _daemon = _ilu.module_from_spec(_daemon_spec)
    sys.modules["mind_hermes_daemon"] = _daemon  # register before exec (dataclass/class self-lookup)
    try:
        _daemon_spec.loader.exec_module(_daemon)
    except Exception:
        sys.modules.pop("mind_hermes_daemon", None)
        raise

PLUGIN_DIR = _daemon.PLUGIN_DIR
ACTIVE_PERSONA_PATH = _daemon.ACTIVE_PERSONA_PATH
PERSONAS_DIR = _daemon.PERSONAS_DIR
BridgeError = _daemon.BridgeError
get_mind_runtime = _daemon.get_mind_runtime

# The gateway applies plugin settings via its plugin context (``__init__.py``);
# a dashboard process never runs that, so bind the configured runtime settings
# (weather location, HA/vault endpoints) here before the first tick.


def _plugin_settings_view_raw() -> Dict[str, object]:
    """Raw ``plugins.entries.<name>.settings`` dict, {} when unreadable."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
        from hermes_cli.plugins import _plugin_settings_entry  # type: ignore
        return (_plugin_settings_entry(load_config_readonly() or {}, "mind-hermes") or {}).get("settings", {}) or {}
    except Exception:
        return {}


_daemon.get_mind_runtime().apply_plugin_config(_plugin_settings_view_raw())
# Dashboard processes never run the gateway's ODE ticker, so nothing would
# sample the History ring while the gateway ticker is old/off. Start a
# record-only sampler here; it dedupes against any live ticker producer.
get_mind_runtime().start_recorder(
    float(get_mind_runtime().tick_interval_sec)
)

router = APIRouter()


class EventPayload(BaseModel):
    kind: str  # "preset" | "nudge" | "set"
    target: str
    value: float = 1.0


class FeedPayload(BaseModel):
    """External placeholder-feed ingress (feed-contracts-2026-11-02.md).
    value=None clears the feed to OFF (-1); hold_min>0 makes it a daemon-expired
    event pulse, hold_min=0 a level value the next tick may overwrite."""
    param: str
    value: Optional[float] = None
    hold_min: float = 0.0


class PersonaPayload(BaseModel):
    name: str
    keep_state: bool = False

# ---------------------------------------------------------------------------
# Console endpoints — lean, read-mostly views for a compact dashboard tab.
# Generic: no Workbench scenario/param/graph editing lives here.
# ---------------------------------------------------------------------------


def _plugin_settings_view() -> Dict[str, object]:
    """Whitelisted runtime settings for display: location always (weather
    source), plus non-secret scalar settings. Secrets/paths are reported as
    configured/unconfigured only — the view never echoes token values."""
    cfg: Dict[str, object] = {}
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
        from hermes_cli.plugins import _plugin_settings_entry  # type: ignore
        cfg = (_plugin_settings_entry(load_config_readonly() or {}, "mind-hermes") or {}).get("settings", {})
    except Exception:
        cfg = {}
    view: Dict[str, object] = {}
    for key, value in cfg.items():
        if key.endswith("_token"):
            view[key] = {"configured": bool(value)}
        elif isinstance(value, (bool, int, float, str)):
            view[key] = value
    lat = cfg.get("latitude")
    lon = cfg.get("longitude")
    if lat is not None and lon is not None:
        view["weather_location"] = {"latitude": lat, "longitude": lon}
    return view


@router.get("/live")
async def get_console_live():
    """One compact snapshot for the Console tab: identity, live stats, sensors."""
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    snap = rt.status_dict()
    state = snap.get("state", {})
    return {
        "sim_time": snap["sim_time"],
        "degraded": snap["degraded"],
        "state_telemetry": snap["state_telemetry"],
        "persona": rt.persona_info(),
        "settings": _plugin_settings_view(),
        "weather": {
            "latitude": rt.sensors.cfg.latitude,
            "longitude": rt.sensors.cfg.longitude,
            "enabled": rt.sensors.cfg.enable_weather_api,
            "last_fetch_age_s": round(time.time() - rt.sensors._weather_ts) if rt.sensors._weather_ts else None,
        },
        "sensors": {k: round(v, 4) for k, v in state.items() if k.startswith("sensors.")},
        "raw": snap.get("raw", {}),
        "ingest": snap.get("ingest", {}),
        "traits": snap["traits"],
        "wake": snap["wake"],
    }


# ---------------------------------------------------------------------------
# Runtime history + editable settings
# ---------------------------------------------------------------------------

# Display metadata for recorded series: (label, unit). "real" marks actual
# physical quantities; "pseudo" marks the normalized 0..1 values the ODE eats.
_SERIES_META = {
    "sensor_real.temp_c": ("Outside temp", "°C"),
    "sensor_real.cloud_pct": ("Cloud cover", "%"),
    "sensor_real.precip_mmh": ("Precipitation", "mm/h"),
    "sensor_real.pressure_hpa": ("Pressure", "hPa"),
    "sensor_real.coolant_temp_c": ("Coolant loop", "°C"),
    "sensor_real.cpu_load_pct": ("CPU load", "%"),
    "sensor_real.gpu_load_pct": ("GPU load", "%"),
    "sensor_real.solar_kwp": ("Solar surplus", "kW"),
    "sensor_real.battery_pct": ("Battery SoC", "%"),
    "sensor_real.vault_inbox_notes": ("Vault inbox", "notes"),
    "sensor_real.vault_orphan_notes": ("Vault orphans", "notes"),
    "sensor_real.vault_open_checkboxes": ("Open loops", "tasks"),
    "sensor_real.vault_total_notes": ("Vault size", "notes"),
    "sensor_real.honcho_pending": ("Honcho pending", "items"),
    "sensors.weather_gloom": ("Weather gloom", "0..1"),
    "sensors.baro_drop": ("Baro drop", "0..1"),
    "sensors.compute_load": ("Compute load", "0..1"),
    "sensors.coolant_temp": ("Coolant (norm)", "0..1"),
    "sensors.solar_kw": ("Solar (norm)", "0..1"),
    "sensors.battery_soc": ("Battery (norm)", "0..1"),
    "sensors.unprocessed_notes": ("Unprocessed notes", "0..1"),
    "sensors.vault_entropy": ("Vault entropy", "0..1"),
    "sensors.open_loops": ("Open loops", "0..1"),
}


@router.get("/history")
async def get_history():
    """Per-tick ring buffer as series-oriented data for graph rendering."""
    rt = get_mind_runtime()
    data = rt.history_payload()
    meta = {}
    for name in data["series"]:
        label, unit = _SERIES_META.get(name, (name.split(".")[-1], ""))
        meta[name] = {
            "label": label,
            "unit": unit,
            "block": name.split(".")[0],
            "real": name.startswith("sensor_real."),
            "pseudo": name.startswith("sensors."),
        }
    return {**data, "meta": meta}


# Console-editable settings whitelist: key -> kind. Secrets are settable but
# never echoed back. Override persists to plugin-data/settings.json and is
# re-applied by every process (gateway included) at startup.
_EDITABLE = {
    "latitude": "float",
    "longitude": "float",
    "solar_max_kw": "float",
    "enable_weather_api": "bool",
    "use_liquidctl": "bool",
    "ingest_weather": "bool",
    "ingest_hardware": "bool",
    "ingest_energy": "bool",
    "ingest_vault": "bool",
    "feed_real_values": "bool",
    "obsidian_vault_path": "str",
    "honcho_inbox_subdir": "str",
    "honcho_url": "str",
    "ha_base_url": "str",
    "ha_token": "secret",
    "ha_solar_entity": "str",
    "ha_battery_entity": "str",
    "hwmon_coolant_path": "str",
    "gateway_url": "str",
    "outreach_webhook": "str",
    "tick_interval_sec": "float",
    "max_context_chars": "float",
}

# External feed sources (dashboard/feed-contracts-2026-11-02.md). Every key here
# defaults to off/empty in daemon.FEED_DEFAULTS, so whitelisting them changes
# nothing until someone points a real source at the daemon. List-valued keys are
# whitelisted as comma-separated strings (daemon.FEED_DEFAULTS parses them).
for _fk, _fv in _daemon.FEED_DEFAULTS.items():
    if _fk in _EDITABLE:
        continue
    if isinstance(_fv, bool):
        _EDITABLE[_fk] = "bool"
    elif isinstance(_fv, (int, float)):
        _EDITABLE[_fk] = "float"
    else:
        _EDITABLE[_fk] = "str"


@router.get("/settings")
async def get_settings_view():
    """Merged runtime settings: Nix config < settings.json override < live flags.

    Secrets appear as {configured: bool} only — values are never echoed."""
    rt = get_mind_runtime()
    cfg = _plugin_settings_view_raw()
    overrides = rt._settings_override
    values: Dict[str, object] = {}
    for key, kind in _EDITABLE.items():
        if kind == "secret":
            values[key] = {"configured": bool(cfg.get(key) or overrides.get(key))}
        elif key in overrides:
            values[key] = overrides[key]
        elif key in cfg and isinstance(cfg[key], (bool, int, float, str)):
            values[key] = cfg[key]
    # Runtime-owned knobs: show the *effective* value when neither source set one.
    values.setdefault("tick_interval_sec", rt.tick_interval_sec)
    values.setdefault("max_context_chars", rt.max_context_chars)
    return {
        "values": values,
        "flags": dict(rt.ingest_flags),
        "override_keys": sorted(overrides),
        "config_keys": sorted(cfg),
    }


@router.put("/settings")
async def put_settings(patch: Dict[str, object]):
    """Apply + persist console-editable settings (whitelist only)."""
    rt = get_mind_runtime()
    clean: Dict[str, object] = {}
    for key, value in patch.items():
        kind = _EDITABLE.get(key)
        if kind is None:
            raise HTTPException(400, f"unknown or non-editable setting: {key!r}")
        if kind == "float":
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"setting {key!r} needs a number, got {value!r}")
        elif kind == "bool":
            clean[key] = bool(value)
        else:
            clean[key] = str(value)
    with rt.lock:
        rt.apply_runtime_settings(clean)
        rt.save_settings_override(clean)
    return {"ok": True, "applied": clean}


@router.get("/traits/meta")
async def get_traits_meta():
    """Trait id, value, declared bounds and description (from the active model)."""
    rt = get_mind_runtime()
    meta = []
    ir_traits = getattr(rt.engine.ir, "traits", {})
    for name, value in sorted(rt.engine.traits.items()):
        t = ir_traits.get(name)
        entry = {"name": name, "value": value}
        if t is not None:
            entry["min"] = getattr(t, "min_val", 0.0)
            entry["max"] = getattr(t, "max_val", 1.0)
            for attr in ("label", "description"):
                val = getattr(t, attr, None)
                if isinstance(val, str):
                    entry[attr] = val
        meta.append(entry)
    return {"traits": meta}


@router.get("/persona")
async def get_persona():
    """Active persona + override-dir listing (generic .mind files)."""
    rt = get_mind_runtime()
    personas = []
    if PERSONAS_DIR.is_dir():
        for f in sorted(PERSONAS_DIR.glob("*.mind")):
            if f.name == "active.mind":
                continue
            personas.append({"name": f.name, "mtime": f.stat().st_mtime, "size": f.stat().st_size})
    return {**rt.persona_info(), "personas": personas}


@router.post("/persona/load")
async def load_persona(req: PersonaPayload):
    """Activate a persona from the override dir, or 'bundled' for the factory model."""
    rt = get_mind_runtime()
    if req.name in ("", "bundled", "default"):
        target = None
    else:
        candidate = (PERSONAS_DIR / req.name).resolve()
        if candidate.parent != PERSONAS_DIR.resolve() or not candidate.suffix == ".mind":
            raise HTTPException(400, f"persona must be a .mind file inside the personas dir: {req.name!r}")
        rt.reload_from_disk_if_newer()
        with rt.lock:
            PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
            ACTIVE_PERSONA_PATH.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
        target = ACTIVE_PERSONA_PATH
    try:
        with rt.lock:
            result = rt.activate_persona(target, keep_state=req.keep_state)
    except BridgeError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "loaded": result, **rt.persona_info()}


@router.get("/status")
async def get_live_status():
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    return rt.status_dict()


@router.post("/tick")
async def force_live_tick():
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    rt.tick_once()
    return rt.status_dict()


@router.post("/event")
async def trigger_live_event(req: EventPayload):
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer ()
    try:
        with rt.lock:
            if req.kind == "preset":
                rt.engine.apply_preset(req.target, req.value)
            elif req.kind == "set":
                rt.engine.set_leaf(req.target, req.value)
            elif req.kind == "nudge":
                rt.engine.nudge(req.target, req.value)
            else:
                raise HTTPException(400, f"Unknown kind {req.kind}")
            rt.save_state()
    except BridgeError as exc:
        raise HTTPException(400, str(exc))
    rt.record_event(req.kind, req.target, req.value)
    return rt.status_dict()


@router.post("/feed")
async def feed_live_placeholder(req: FeedPayload):
    """External feed ingress for placeholder params (feed-contracts-2026-11-02.md).
    Unknown params and out-of-range values are refused (400) — an absent source
    must be reported as OFF by the daemon, never guessed by a caller."""
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    ok = rt.external_feed(req.param, req.value, req.hold_min)
    if not ok:
        raise HTTPException(400, f"unknown placeholder param or out-of-range value: {req.param!r}")
    return rt.status_dict()


@router.post("/event/external")
async def feed_live_event(req: FeedPayload):
    """External shared-channel event ingress (desires-2-feeds 2026-11-03): the
    'param' field carries the EVENT name (nsfw_cue | partner_cue | worry | praise |
    headpat | cuddle_touch | culmination | climax). Nudge names nudge the tick-less
    ema HOLD leaf; culmination/climax fire the afterglow satiety SET so the model
    integrates the ~0.1 climax-debt dose. value=None uses the configured default
    level. Unknown names are refused (400), never guessed."""
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    ok = rt.external_event(req.param, req.value)
    if not ok:
        raise HTTPException(400, f"unknown event name or out-of-range value: {req.param!r}")
    return rt.status_dict()


@router.put("/traits")
async def update_live_traits(traits: Dict[str, float]):
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    with rt.lock:
        for k, v in traits.items():
            if k in rt.engine.traits:
                t_ir = rt.engine.ir.traits[k]
                rt.engine.set_trait(k, max(t_ir.min_val, min(t_ir.max_val, float(v))))
        rt.save_state()
    if traits:
        rt.record_event("trait", ",".join(traits.keys()), float(max(traits.values())))
    return rt.status_dict()


@router.put("/params")
async def update_live_params(params: Dict[str, float]):
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    with rt.lock:
        for k, v in params.items():
            if k in rt.engine.params:
                rt.engine.set_param(k, float(v))
        rt.save_state()
    if params:
        rt.record_event("param", ",".join(params.keys()), float(max(params.values())))
    return rt.status_dict()


@router.get("/workbench", response_class=HTMLResponse)
async def serve_workbench_html():
    # Fully self-contained: iframe/srcdoc callers cannot attach the dashboard
    # session header, so every resource the Workbench needs is inlined and it
    # makes zero authenticated sub-requests.
    html = (PLUGIN_DIR / "web" / "index.html").read_text(encoding="utf-8")
    mind_js = (PLUGIN_DIR / "web" / "mind.js").read_text(encoding="utf-8")
    model_src = (PLUGIN_DIR / "model" / "model.mind").read_text(encoding="utf-8")
    inline = (
        "<script>window.__MIND_INLINE_MODEL = " + json.dumps(model_src) + ";</script>\n"
        "<script>\n" + mind_js + "\n</script>"
    )
    html = html.replace('<script src="mind.js"></script>', inline)
    return HTMLResponse(html)


@router.get("/mind.js")
async def serve_mind_js():
    return FileResponse(PLUGIN_DIR / "web" / "mind.js", media_type="application/javascript")


@router.get("/model/model.mind")
@router.get("/model.mind")
async def serve_model_mind():
    return FileResponse(PLUGIN_DIR / "model" / "model.mind", media_type="text/plain")

