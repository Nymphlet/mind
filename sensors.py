from __future__ import annotations
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# ABI marker: bumped when the collector grows attributes the daemon depends on
# (raw_readings, ingest flags). daemon.py reuses an already-loaded `sensors`
# module only when this marker matches its expectations.
# v3: pump tach mirror (read_pump_duty_pct / sensor_real.pump_rpm_pct) and the
# HA presence reader used by the social.third_parties / lust.partner_cue feeds
# (presence_fraction) — feed-contracts-2026-11-02.md.
_SENSORS_ABI = 3


def _presence_value(state) -> Optional[float]:
    """Normalize a Home Assistant entity state to a presence fraction 0..1.
    Returns None for anything uninterpretable (never guess presence)."""
    if isinstance(state, bool):
        return 1.0 if state else 0.0
    if isinstance(state, (int, float)):
        f = float(state)
        return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
    if isinstance(state, str):
        s = state.strip().lower()
        if s in ("on", "home", "present", "true", "detected", "occupied"):
            return 1.0
        if s in ("off", "not_home", "absent", "false", "clear", "unoccupied"):
            return 0.0
        try:
            f = float(s)
        except ValueError:
            return None
        return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
    return None


@dataclass
class SensorConfig:
    # 1. Obsidian Vault & Honcho Memory Service
    obsidian_vault_path: Optional[str] = os.environ.get("HERMES_OBSIDIAN_VAULT")
    honcho_inbox_subdir: str = os.environ.get("HERMES_HONCHO_INBOX", "00_Inbox")
    honcho_url: Optional[str] = os.environ.get("HONCHO_URL")  # e.g. "http://127.0.0.1:8000"
    honcho_api_key: Optional[str] = os.environ.get("HONCHO_API_KEY")

    # 2. Home Assistant / Solar Microgrid API (`solar_kw` & `battery_soc`)
    ha_base_url: Optional[str] = os.environ.get("HA_BASE_URL")
    ha_token: Optional[str] = os.environ.get("HA_TOKEN")
    ha_solar_entity: str = os.environ.get("HA_SOLAR_ENTITY", "sensor.solar_pv_surplus_kw")
    ha_battery_entity: str = os.environ.get("HA_BATTERY_ENTITY", "sensor.home_battery_soc")
    solar_max_kw: float = float(os.environ.get("HERMES_SOLAR_MAX_KW", "5.0"))
    # When false, HA-backed solar/battery readings never overwrite the model's
    # simulated PV/battery states (pseudo mode).
    ingest_energy: bool = os.environ.get("HERMES_INGEST_ENERGY", "1") == "1"

    # 3. Watercooling Loop Sensor (`coolant_temp`: 24°C -> 0.0, 46°C -> 1.0)
    use_liquidctl: bool = os.environ.get("HERMES_USE_LIQUIDCTL", "1") == "1"
    hwmon_coolant_path: Optional[str] = os.environ.get("HERMES_HWMON_COOLANT")
    coolant_min_c: float = 24.0
    coolant_max_c: float = 46.0

    # 3b. Pump tach mirror (sensor_real.pump_rpm_pct -> cardio.flow_feed).
    # Both sources optional and unconfigured by default: liquidctl is absent on
    # the sealed host and no tach file is set, so the pump feed stays OFF.
    pump_tach_path: Optional[str] = os.environ.get("HERMES_PUMP_TACH_PATH")
    pump_rpm_max: float = float(os.environ.get("HERMES_PUMP_RPM_MAX", "4800"))

    # 4. Weather & Barometer (Open-Meteo free API)
    enable_weather_api: bool = os.environ.get("HERMES_WEATHER_API", "1") == "1"
    latitude: float = float(os.environ.get("HERMES_LAT", "48.2"))
    longitude: float = float(os.environ.get("HERMES_LON", "16.37"))


class TelemetryCollector:
    def __init__(self, cfg: Optional[SensorConfig] = None):
        self.cfg = cfg or SensorConfig()
        self._weather_cache: Dict[str, float] = {}
        self._weather_ts: float = 0.0
        self._vault_cache: Dict[str, float] = {}
        self._vault_ts: float = 0.0
        self._prev_pressure_hpa: Optional[float] = None
        # Most recent *real-quantity* readings (°C, %, mm/h, hPa, kW, SoC %,
        # counts). Raw physical values are recorded for history and model feeds;
        # the normalized 0..1 blend values stay what the ODE consumes.
        self.raw_readings: Dict[str, float] = {}

    def collect_all(self) -> Dict[str, float]:
        readings: Dict[str, float] = {}

        comp = self.read_compute_load()
        if comp is not None:
            readings["sensors.compute_load"] = comp

        coolant_c = self.read_coolant_c()
        if coolant_c is not None:
            self.raw_readings["sensor_real.coolant_temp_c"] = round(coolant_c, 2)
            span = max(1.0, self.cfg.coolant_max_c - self.cfg.coolant_min_c)
            readings["sensors.coolant_temp"] = max(0.0, min(1.0, (coolant_c - self.cfg.coolant_min_c) / span))

        pump_pct = self.read_pump_duty_pct()
        if pump_pct is not None:
            # Raw tach mirror only: the model has no sensors.pump leaf, and
            # cardio.flow_feed is fed from this value by the daemon (contracts doc).
            self.raw_readings["sensor_real.pump_rpm_pct"] = round(pump_pct, 1)

        readings.update(self.read_solar_and_battery())
        readings.update(self.read_weather_and_barometer())
        readings.update(self.read_obsidian_and_honcho())
        return readings

    def read_compute_load(self) -> Optional[float]:
        cpu_load = self._cpu_util()

        gpu_load = 0.0
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout.strip():
                vals = [float(x.strip()) / 100.0 for x in res.stdout.strip().splitlines() if x.strip()]
                if vals:
                    gpu_load = max(vals)
        except Exception:
            pass
        self.raw_readings["sensor_real.cpu_load_pct"] = round(100.0 * min(1.0, cpu_load), 1)
        self.raw_readings["sensor_real.gpu_load_pct"] = round(100.0 * min(1.0, gpu_load), 1)
        return max(0.0, min(1.0, max(cpu_load, gpu_load)))

    def _cpu_util(self) -> float:
        """Instantaneous CPU utilization 0..1 from /proc/stat jiffy deltas.

        1-minute load average is exponentially smoothed and visibly freezes
        between samples; the jiffy delta over a short window actually moves."""
        try:
            def sample():
                with open("/proc/stat", "r") as f:
                    fields = f.readline().split()[1:]
                vals = [int(x) for x in fields]
                idle = vals[4] + (vals[5] if len(vals) > 5 else 0)
                return idle, sum(vals)

            idle0, total0 = sample()
            time.sleep(0.2)
            idle1, total1 = sample()
            dt = total1 - total0
            if dt <= 0:
                return 0.0
            return max(0.0, min(1.0, 1.0 - (idle1 - idle0) / dt))
        except Exception:
            try:
                return min(1.0, float(os.getloadavg()[0]) / max(1, os.cpu_count() or 1))
            except Exception:
                return 0.0

    def read_coolant_c(self) -> Optional[float]:
        """Raw watercooling loop temperature in °C (hwmon or liquidctl), None when offline."""
        temp_c: Optional[float] = None
        if self.cfg.hwmon_coolant_path and os.path.exists(self.cfg.hwmon_coolant_path):
            try:
                raw = float(Path(self.cfg.hwmon_coolant_path).read_text().strip())
                temp_c = raw / 1000.0 if raw > 200 else raw
            except Exception:
                pass

        if temp_c is None and self.cfg.use_liquidctl:
            try:
                res = subprocess.run(
                    ["liquidctl", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    for dev in json.loads(res.stdout):
                        for st in dev.get("status", []):
                            key = st.get("key", "").lower()
                            if any(w in key for w in ("liquid", "coolant", "water", "sensor 1")):
                                temp_c = float(st["value"])
                                break
            except Exception:
                pass

        if temp_c is None:
            return None
        return temp_c

    def read_pump_duty_pct(self) -> Optional[float]:
        """Real pump duty 0..100 %. Two optional sources:
          1. an hwmon tach file (fanN_input, RPM) normalized by pump_rpm_max;
          2. `liquidctl status --json` pump speed/duty keys.
        Neither available -> None: sensor_real.pump_rpm_pct stays unfed and the
        cardio.flow_feed maps to OFF (liquidctl is absent on the sealed host)."""
        path = self.cfg.pump_tach_path
        if path and os.path.exists(path):
            try:
                rpm = float(Path(path).read_text().strip())
                if rpm > 0:
                    return max(0.0, min(100.0, rpm / max(1.0, float(self.cfg.pump_rpm_max)) * 100.0))
            except Exception:
                pass
        if self.cfg.use_liquidctl:
            try:
                res = subprocess.run(
                    ["liquidctl", "status", "--json"],
                    capture_output=True, text=True, timeout=2.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    for dev in json.loads(res.stdout):
                        for st in dev.get("status", []):
                            key = str(st.get("key", "")).lower()
                            if "pump" in key and any(w in key for w in ("speed", "duty", "rpm")):
                                val = float(st["value"])
                                unit = str(st.get("unit", "")).lower()
                                if "%" in unit:
                                    pct = val
                                elif "rpm" in unit:
                                    pct = val / max(1.0, float(self.cfg.pump_rpm_max)) * 100.0
                                else:
                                    pct = val if val <= 100.0 else (
                                        val / max(1.0, float(self.cfg.pump_rpm_max)) * 100.0)
                                return max(0.0, min(100.0, pct))
            except Exception:
                pass
        return None

    def presence_fraction(self, entities: List[str]) -> Optional[float]:
        """Max presence fraction 0..1 across Home Assistant entities (binary_sensor
        on/off, numeric 0..1 or 0..100). None when nothing answered — a dead sensor
        means UNKNOWN, and the caller feeds OFF rather than guessing 'alone'.
        Used by the social.third_parties and lust.partner_cue level feeds."""
        if not entities or not self.cfg.ha_base_url or not self.cfg.ha_token:
            return None
        base = self.cfg.ha_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.cfg.ha_token}",
                   "Content-Type": "application/json"}
        best: Optional[float] = None
        for entity in entities:
            try:
                req = urllib.request.Request(f"{base}/api/states/{entity}", headers=headers)
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    state = json.loads(resp.read().decode("utf-8")).get("state")
            except Exception:
                continue
            frac = _presence_value(state)
            if frac is not None:
                best = frac if best is None else max(best, frac)
        return best

    def read_solar_and_battery(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not self.cfg.ha_base_url or not self.cfg.ha_token:
            return out
        headers = {
            "Authorization": f"Bearer {self.cfg.ha_token}",
            "Content-Type": "application/json",
        }
        base = self.cfg.ha_base_url.rstrip("/")
        for entity, key, scale, raw_key, raw_scale in [
            (self.cfg.ha_solar_entity, "sensors.solar_kw", self.cfg.solar_max_kw,
             "sensor_real.solar_kwp", 1.0),
            (self.cfg.ha_battery_entity, "sensors.battery_soc", 100.0,
             "sensor_real.battery_pct", 1.0),
        ]:
            try:
                req = urllib.request.Request(f"{base}/api/states/{entity}", headers=headers)
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    raw = float(payload["state"])
                    self.raw_readings[raw_key] = round(raw * raw_scale, 3)
                    out[key] = max(0.0, min(1.0, raw / max(1e-6, scale)))
            except Exception:
                pass
        return out

    def read_weather_and_barometer(self) -> Dict[str, float]:
        if not self.cfg.enable_weather_api:
            return {}
        now = time.time()
        if now - self._weather_ts < 900 and self._weather_cache:
            return dict(self._weather_cache)

        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.cfg.latitude}&longitude={self.cfg.longitude}"
            f"&current=temperature_2m,cloud_cover,precipitation,surface_pressure"
        )
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                cur = json.loads(resp.read().decode("utf-8")).get("current", {})
                temp_c = float(cur.get("temperature_2m", 8.0))
                cloud_pct = float(cur.get("cloud_cover", 20.0))
                precip_mmh = float(cur.get("precipitation", 0.0))
                cloud = cloud_pct / 100.0
                precip = min(1.0, precip_mmh / 4.0)
                pressure = float(cur.get("surface_pressure", 1013.25))

                gloom = max(0.0, min(1.0, 0.70 * cloud + 0.30 * precip))
                delta_drop = max(0.0, (self._prev_pressure_hpa - pressure) / 3.0) if self._prev_pressure_hpa else 0.0
                self._prev_pressure_hpa = pressure
                low_p = max(0.0, (1013.0 - pressure) / 18.0)
                baro_drop = max(0.0, min(1.0, 0.65 * low_p + 0.35 * delta_drop))

                self.raw_readings.update({
                    "sensor_real.temp_c": round(temp_c, 2),
                    "sensor_real.cloud_pct": round(cloud_pct, 1),
                    "sensor_real.precip_mmh": round(precip_mmh, 2),
                    "sensor_real.pressure_hpa": round(pressure, 1),
                })
                self._weather_cache = {
                    "sensors.weather_gloom": gloom,
                    "sensors.baro_drop": baro_drop,
                }
                self._weather_ts = now
        except Exception:
            pass
        return dict(self._weather_cache)

    def read_obsidian_and_honcho(self) -> Dict[str, float]:
        now = time.time()
        if now - self._vault_ts < 120 and self._vault_cache:
            return dict(self._vault_cache)

        out: Dict[str, float] = {}

        # A. Obsidian Vault Markdown Scanner
        if self.cfg.obsidian_vault_path:
            vault_dir = Path(self.cfg.obsidian_vault_path)
            if vault_dir.is_dir():
                try:
                    md_files = [p for p in vault_dir.rglob("*.md") if not any(part.startswith(".") for part in p.parts)]
                    total_notes = max(1, len(md_files))
                    open_checkboxes = 0
                    orphan_notes = 0
                    inbox_notes = 0
                    inbox_path = vault_dir / self.cfg.honcho_inbox_subdir

                    for p in md_files:
                        if inbox_path in p.parents:
                            inbox_notes += 1
                        try:
                            txt = p.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            continue
                        open_checkboxes += len(re.findall(r"^\s*[-*]\s+\[\s\]", txt, re.MULTILINE))
                        if "[[" not in txt and "#" not in txt:
                            orphan_notes += 1

                    self.raw_readings.update({
                        "sensor_real.vault_open_checkboxes": float(open_checkboxes),
                        "sensor_real.vault_orphan_notes": float(orphan_notes),
                        "sensor_real.vault_inbox_notes": float(inbox_notes),
                        "sensor_real.vault_total_notes": float(len(md_files)),
                    })
                    out["sensors.unprocessed_notes"] = max(0.0, min(1.0, inbox_notes / 12.0))
                    out["sensors.vault_entropy"] = max(0.0, min(1.0, orphan_notes / max(10.0, total_notes * 0.35)))
                    out["sensors.open_loops"] = max(0.0, min(1.0, open_checkboxes / 25.0))
                except Exception:
                    pass

        # B. Optional Honcho REST API Pending Queue Check
        if self.cfg.honcho_url:
            try:
                headers = {"Content-Type": "application/json"}
                if self.cfg.honcho_api_key:
                    headers["Authorization"] = f"Bearer {self.cfg.honcho_api_key}"
                req = urllib.request.Request(f"{self.cfg.honcho_url.rstrip('/')}/v1/metrics", headers=headers)
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    hdata = json.loads(resp.read().decode("utf-8"))
                    pending = float(hdata.get("pending_conclusions", hdata.get("unprocessed_messages", 0)))
                    self.raw_readings["sensor_real.honcho_pending"] = pending
                    if pending > 0:
                        honcho_load = max(0.0, min(1.0, pending / 20.0))
                        out["sensors.unprocessed_notes"] = max(out.get("sensors.unprocessed_notes", 0.0), honcho_load)
            except Exception:
                pass

        if out:
            self._vault_cache = out
            self._vault_ts = now
        return dict(self._vault_cache)

