"""Mind-Hermes plugin entry point.

Embodied ODE mind: always-inserted `ambient_mind` prose at every turn
(user-message channel -> cache-stable prefix untouched), state telemetry,
proactive wake backlog, interaction appraisal tools, /mind command.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from daemon import FEED_SETTING_KEYS, get_mind_runtime  # noqa: E402
from mind_core import TRIGGER_PRESETS  # noqa: E402

# settings keys mirroring plugin.yaml `config:` (plugins.entries.mind-hermes.settings.<key>)
_CONFIG_KEYS = (
    "obsidian_vault_path", "honcho_inbox_subdir", "honcho_url",
    "ha_base_url", "ha_token", "ha_solar_entity", "ha_battery_entity",
    "solar_max_kw", "hwmon_coolant_path", "use_liquidctl",
    "latitude", "longitude", "gateway_url", "outreach_webhook",
    "max_context_chars", "tick_interval_sec",
    "pump_tach_path", "pump_rpm_max",
    # External feed sources (feed-contracts-2026-11-02.md) — every one defaults
    # to off/empty in daemon.FEED_DEFAULTS, so listing them here changes nothing.
) + tuple(sorted(FEED_SETTING_KEYS))

_MAX_CTX_CHARS_DEFAULT = 1000  # mirrored as runtime default in daemon.py


def _collect_config(ctx: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    get = getattr(ctx, "get_config", None)
    if callable(get):
        for key in _CONFIG_KEYS:
            try:
                val = get(key)
            except Exception:
                val = None
            if val is not None:
                cfg[key] = val
    return cfg


def register(ctx: Any) -> None:
    """Native Hermes Agent entry point. No network/thread side effects here —
    the ticker starts lazily on the first hook so `hermes plugins doctor` stays clean."""
    plugin_cfg = _collect_config(ctx)
    # tick_interval_sec / max_context_chars land on the runtime via
    # apply_plugin_config (override-aware); no one-shot closure constants.
    get_mind_runtime().apply_plugin_config(plugin_cfg)

    def on_pre_llm_call(**kwargs: Any) -> Dict[str, Any]:
        rt = get_mind_runtime()
        rt.start_background_ticker()
        rt.reload_from_disk_if_newer()

        # External feed sources visible on this hook (feed-contracts doc):
        # real context-fill pressure + shared-channel/sender third-party signal.
        # Both are off-by-default config gates; with defaults they write nothing.
        try:
            rt.note_context_fill(kwargs.get("conversation_history"))
            rt.note_turn_context(kwargs.get("platform"), kwargs.get("sender_id"))
        except Exception:
            pass

        raw_msg = str(kwargs.get("user_message", "") or kwargs.get("prompt", "")).strip()
        lower_msg = raw_msg.lower()

        # self-wake guard: gateway/daemon-originated turns must not fake user presence
        is_autonomous_wake = (
            "[SILENT WORKER WAKE:" in raw_msg
            or "[PROACTIVE OUTREACH WAKE:" in raw_msg
            or bool(kwargs.get("is_autonomous"))
        )

        with rt.lock:
            if not is_autonomous_wake:
                if lower_msg in ("gn", "goodnight", "good night", "logging off", "gtg", "bye"):
                    rt.engine.apply_preset("user_leave", 1.0)
                else:
                    rt.engine.set_leaf("social.user_present", 0.95)
                    rt.engine.nudge("drives.task_commit", 0.16)
                    rt.engine.nudge("sensors.unprocessed_notes", 0.04)

            snap = rt.engine.snapshot()
            ambient = rt.engine.render("ambient_mind", snap)
            rt.save_state()

        # drain wakes accumulated while we were away into this turn's context
        backlog = rt.drain_pending_wakes()
        if backlog:
            lines = [f"[WAKE BACKLOG x{len(backlog)} — internal fires since last turn]"]
            lines += [f"- {e['wake_mode']}: {e['state_telemetry']}" for e in backlog[-3:]]
            ambient = "\n".join(lines) + "\n" + ambient

        if len(ambient) > rt.max_context_chars:
            ambient = ambient[:rt.max_context_chars].rsplit(" ", 1)[0] + " …"
        return {"context": ambient}

    def on_post_compaction(**kwargs: Any) -> None:
        del kwargs  # real fire-site: agent/compaction_events.py — one per committed compaction
        rt = get_mind_runtime()
        rt.reload_from_disk_if_newer()
        with rt.lock:
            rt.engine.apply_preset("lcm_compaction", 1.0)
            rt.save_state()

    def on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **kwargs: Any) -> None:
        del args, result
        rt = get_mind_runtime()
        rt.reload_from_disk_if_newer()
        tname = str(tool_name or kwargs.get("name", "")).lower()
        is_error = kwargs.get("status") == "error" or bool(kwargs.get("error_type"))
        with rt.lock:
            if is_error:
                rt.engine.nudge("sensors.interrupt_rate", 0.35)
                rt.engine.nudge("cognition.adversity", 0.25)
                rt.engine.nudge("cognition.surprise", 0.20)
            elif "lcm" in tname or "compress" in tname or "compact" in tname:
                rt.engine.apply_preset("lcm_compaction", 0.8)
            elif "obsidian" in tname or "memory" in tname or "honcho" in tname:
                rt.engine.nudge("sensors.unprocessed_notes", -0.15)
                rt.engine.nudge("sensors.vault_entropy", -0.12)
            rt.save_state()

    def on_session_start(**kwargs: Any) -> None:
        del kwargs
        rt = get_mind_runtime()
        # The ticker loop re-reads rt.tick_interval_sec every cycle (config
        # override aware), so no one-shot interval constant is passed here.
        rt.start_background_ticker()
        rt.reload_from_disk_if_newer()
        with rt.lock:
            rt.engine.apply_preset("user_arrive", 1.0)
            # hw.session_boundary placeholder feed (config-gated, off by default):
            # a fresh context window must not inherit archived pointer density.
            rt.on_session_boundary()
            rt.save_state()

    def on_session_finalize(**kwargs: Any) -> None:
        del kwargs
        rt = get_mind_runtime()
        rt.reload_from_disk_if_newer()
        with rt.lock:
            rt.engine.nudge("sensors.unprocessed_notes", 0.15)
            rt.save_state()

    for hook_name, fn in [
        ("pre_llm_call", on_pre_llm_call),
        ("post_tool_call", on_post_tool_call),
        ("post_compaction", on_post_compaction),
        ("on_session_start", on_session_start),
        ("on_session_finalize", on_session_finalize),
    ]:
        ctx.register_hook(hook_name, fn)

    def handle_get_telemetry(params: Dict[str, Any], **kwargs: Any) -> str:
        del params, kwargs
        rt = get_mind_runtime()
        rt.reload_from_disk_if_newer()
        return json.dumps(rt.status_dict(), indent=2)

    def handle_appraise_interaction(params: Dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        rt = get_mind_runtime()
        rt.reload_from_disk_if_newer()
        return _handle_appraisal(rt, params or {})

    ctx.register_tool(
        name="mind_get_telemetry",
        toolset="mind",
        schema={
            "name": "mind_get_telemetry",
            "description": "Read your internal affective, cognitive, watercooling, solar battery, and LCM pointer telemetry.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=handle_get_telemetry,
    )

    ctx.register_tool(
        name="mind_appraise_interaction",
        toolset="mind",
        schema={
            "name": "mind_appraise_interaction",
            "description": (
                "Update your internal emotional/intimate/cognitive traces when a meaningful moment "
                "happens in conversation (e.g. warm praise, flirtation, intimate climax/afterglow, "
                "user stress, or harsh friction)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_preset": {
                        "type": "string",
                        "enum": list(TRIGGER_PRESETS.keys()),
                        "description": "Preset bundle to trigger.",
                    },
                    "valence_nudge": {"type": "number", "description": "Direct nudge to mood.reaction (-1..1)."},
                    "warmth_nudge": {"type": "number", "description": "Direct nudge to social.warmth (-0.5..0.8)."},
                    "afterglow_satiety": {
                        "type": "number",
                        "description": "Set post-intimacy refractory afterglow (0..1) ONLY when intimacy culminates.",
                    },
                    "nsfw_cue": {
                        "type": "number",
                        "description": "Per-event NUDGE of events.nsfw_cue (0..1, shared nudge channel): an encountered desire cue (nsfw material in downtime, erotic cue) — transient, decays ~40 min, kindles wanting across a binge.",
                    },
                    "worry_cue": {
                        "type": "number",
                        "description": "Per-event NUDGE of events.worry (0..1, shared nudge channel): a state-anxiety moment (rejection sting, performance worry around intimacy) — transiently lifts the contextual brake while held.",
                    },
                },
            },
        },
        handler=handle_appraise_interaction,
    )

    def handle_mind_command(args_str: str = "", **kwargs: Any) -> str:
        del kwargs
        rt = get_mind_runtime()
        rt.reload_from_disk_if_newer()
        parts = args_str.strip().split()
        with rt.lock:
            if not parts or parts[0] == "status":
                st = rt.status_dict()
                return f"{st['state_telemetry']}\n\n{st['ambient_mind']}"
            if parts[0] == "preset" and len(parts) >= 2:
                val = float(parts[2]) if len(parts) >= 3 else 1.0
                rt.engine.apply_preset(parts[1], val)
                rt.save_state()
                return f"Applied preset '{parts[1]}':\n{rt.engine.render('state_telemetry')}"
            if parts[0] == "trait" and len(parts) == 3:
                tname, tval = parts[1], float(parts[2])
                if tname not in rt.engine.traits:
                    return f"Unknown trait '{tname}'."
                t_ir = rt.engine.ir.traits[tname]
                clamped = max(t_ir.min_val, min(t_ir.max_val, tval))
                rt.engine.set_trait(tname, clamped)
                rt.save_state()
                return f"Trait {tname} set to {rt.engine.traits[tname]:.2f}."
            if parts[0] in ("nudge", "set") and len(parts) == 3:
                op, target, val = parts[0], parts[1], float(parts[2])
                if op == "set":
                    rt.engine.set_leaf(target, val)
                else:
                    rt.engine.nudge(target, val)
                rt.save_state()
                return f"{op.upper()} {target} ({val}):\n{rt.engine.render('state_telemetry')}"
            return (
                "Usage:\n"
                "  /mind status\n"
                "  /mind preset <praise_success|flirt_intimate|intimate_afterglow|gpu_crunch|solar_peak|...>\n"
                "  /mind trait <trait_name> <value>\n"
                "  /mind set|nudge <state.leaf> <value>"
            )

    if hasattr(ctx, "register_command"):
        ctx.register_command("mind", handle_mind_command, "Inspect or nudge the Mind-Hermes ODE engine")


def _handle_appraisal(rt: Any, args: Dict[str, Any]) -> str:
    with rt.lock:
        if args.get("event_preset"):
            rt.engine.apply_preset(str(args["event_preset"]), 1.0)
        if args.get("valence_nudge") is not None:
            rt.engine.nudge("mood.reaction", float(args["valence_nudge"]))
        if args.get("warmth_nudge") is not None:
            rt.engine.nudge("social.warmth", float(args["warmth_nudge"]))
        if args.get("afterglow_satiety") is not None:
            rt.engine.set_leaf("intimacy.satiety", float(args["afterglow_satiety"]))
        # Shared-channel per-event nudges (desires-2-feeds 12b): explicit args are
        # the source — ungated like valence_nudge; the ema HOLD owns the decay.
        if args.get("nsfw_cue") is not None:
            rt.note_event_nudge("nsfw_cue", args["nsfw_cue"], reason="appraise:nsfw_cue")
        if args.get("worry_cue") is not None:
            rt.note_event_nudge("worry", args["worry_cue"], reason="appraise:worry_cue")
        # lust.partner_cue placeholder feed (config-gated, off by default): the
        # intimacy presets / explicit satiety are the partner-touch signal.
        rt.note_intimacy_event(args.get("event_preset"), args)
        # Climax set-event (config-gated, off by default): guarantees the satiety
        # set class so the model integrates the ~0.1 climax-debt dose.
        rt.note_culmination_event(args.get("event_preset"), args)
        rt.save_state()
        return json.dumps(
            {
                "status": "updated",
                "state_telemetry": rt.engine.render("state_telemetry"),
                "ambient_mind": rt.engine.render("ambient_mind"),
            }
        )
