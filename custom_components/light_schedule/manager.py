"""Zeitsteuerung der Lichter: beliebig viele Profile je Raum.

Ein Profil hat einen Namen, Wochentage, optional einen Zeitraum im Jahr (z. B. 01.12.–31.12.),
mehrere Zeiten pro Tag (Ein um, optional Aus um), die zu schaltenden Lampen/Gruppen, Bedingungen
(Anwesenheit, Alarmanlage, dunkler als / heller als) und die Lichteinstellung (Modus, Helligkeit,
Farbtemperatur, Farbe).

Ablauf je Zeit ("Vorkommen" = Profil + Zeit + Tag):
  Fenster = Ein-Zeit bis Aus-Zeit (ohne Aus-Zeit: bis 15 min nach der Ein-Zeit).
  Im Fenster: sobald alle Bedingungen passen → einmal einschalten (danach nicht erneut, auch wenn
  jemand das Licht ausschaltet). Aus-Zeit erreicht → die Lampen ausschalten, die dieses Vorkommen
  eingeschaltet hat und die ihm noch gehören.

Zusammenspiel mit der Bewegungssteuerung (light_motion, Vorrang-Regel aus KONZEPT.md):
  Gehört eine Lampe zu group.<raum>_light_motion_<licht>:
    source = motion und Vorrang Zeitsteuerung = motion → nicht schalten (die Bewegung behält sie)
    sonst → source = schedule setzen, dann schalten.
  Beim Ausschalten: nur, wenn source noch schedule ist.
  Alle Aufrufe laufen mit einem Kind-Context (parent_id gesetzt), damit die Bewegungssteuerung
  sie nicht für "manuell" hält (keine Sperre nach Hand-Aus).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ALARM, DEFAULT_PROFILE, END_CATCH_UP_H, EVENT_SWITCHED, MAX_PROFILES_PER_ROOM, MAX_SLOTS, MODES,
    MOTION_LIGHTS, NO_END_WINDOW_MIN, OFF_GRACE_S, PRESENCE, SIGNAL_UPDATE, SRC_ALARM, SRC_LUX_OUTDOOR,
    SIM_LOCK, SRC_LUX_ROOM, SRC_PRESENCE, SRC_SIM, STORE_KEY, STORE_VERSION, TICK_SECONDS, VERSION,
)

_LOGGER = logging.getLogger(__name__)

RE_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RE_MD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
RE_ENT = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
RE_COL = re.compile(r"^#[0-9A-Fa-f]{6}$")
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}
BAD = ("unknown", "unavailable", "", None)


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _at(d: date, hm: str) -> datetime:
    h, m = _hm(hm)
    return dt_util.as_local(datetime(d.year, d.month, d.day, h, m, tzinfo=dt_util.DEFAULT_TIME_ZONE))


def in_date_range(p: dict, d: date) -> bool:
    """Zeitraum im Jahr (MM-TT bis MM-TT, beide inklusive, über den Jahreswechsel möglich)."""
    if not p.get("date_enabled"):
        return True
    a, b, x = p.get("date_from", "01-01"), p.get("date_to", "12-31"), d.strftime("%m-%d")
    return a <= x <= b if a <= b else (x >= a or x <= b)


def occurrences(p: dict, around: datetime) -> list[tuple[int, datetime, datetime, bool]]:
    """Vorkommen (Zeit-Index, Start, Ende, hat_aus) mit Start gestern/heute/morgen."""
    out = []
    for off in (-1, 0, 1):
        d = (around + timedelta(days=off)).date()
        if d.weekday() not in p.get("days", []) or not in_date_range(p, d):
            continue
        for i, s in enumerate(p.get("slots", [])):
            start = _at(d, s["on"])
            if s.get("off"):
                end = _at(d, s["off"])
                if end <= start:
                    end = _at(d + timedelta(days=1), s["off"])
                out.append((i, start, end, True))
            else:
                out.append((i, start, start + timedelta(minutes=NO_END_WINDOW_MIN), False))
    return out


class ScheduleManager:
    def __init__(self, hass: HomeAssistant, rooms: list[str]) -> None:
        self.hass = hass
        self.rooms = rooms
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.profiles: dict[str, dict] = {}
        # fired: Vorkommen-Schlüssel → Ende (ISO); active: Schlüssel → {pid, end, has_end, lamps, at}
        self.fired: dict[str, str] = {}
        self.active: dict[str, dict] = {}
        self.last_check: dict[str, str] = {}     # pid → Grund, warum gerade nicht geschaltet wird
        self.root = Context()
        self._unsubs: list = []
        self._busy = False

    # ================================================================ Lebenszyklus
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.profiles = {k: self._clean({**DEFAULT_PROFILE, **v}, k) for k, v in (data.get("profiles") or {}).items()}
        self.fired = data.get("fired") or {}
        self.active = data.get("active") or {}
        self._unsubs.append(async_track_time_change(self.hass, self._tick, second=TICK_SECONDS))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        self.hass.async_create_task(self._process())

    async def _async_stop(self, _ev: Event | None = None) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self._save_now()

    def _data(self) -> dict:
        return {"profiles": self.profiles, "fired": self.fired, "active": self.active}

    def _save(self) -> None:
        self.store.async_delay_save(self._data, 2)

    async def _save_now(self) -> None:
        await self.store.async_save(self._data())

    # ================================================================ Profile
    def _clean(self, p: dict, pid: str | None = None) -> dict:
        if isinstance(p.get("transition"), (int, float)) and p["transition"] < 0:
            p = {**p, "transition": 1.0}
        def num(k, lo, hi, d):
            try:
                return max(lo, min(hi, float(p.get(k, d))))
            except (TypeError, ValueError):
                return d
        room = str(p.get("room") or "")
        if room not in self.rooms:
            raise ValueError(f"Unbekannter Raum: {room!r}")
        slots = []
        for s in (p.get("slots") or [])[:MAX_SLOTS]:
            on, off = str((s or {}).get("on") or ""), (s or {}).get("off") or None
            if not RE_HM.match(on):
                raise ValueError(f"Ungültige Ein-Zeit: {on!r}")
            if off is not None and not RE_HM.match(str(off)):
                raise ValueError(f"Ungültige Aus-Zeit: {off!r}")
            slots.append({"on": on, "off": off})
        days = sorted({int(d) for d in (p.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
        lights = [e for e in dict.fromkeys(p.get("lights") or []) if isinstance(e, str) and RE_ENT.match(e)]
        df, dto = str(p.get("date_from") or "12-01"), str(p.get("date_to") or "12-31")
        if not RE_MD.match(df) or not RE_MD.match(dto):
            raise ValueError("Ungültiger Zeitraum (erwartet MM-TT)")
        src = str(p.get("lux_source") or "room")
        if src not in ("room", "outdoor") and not RE_ENT.match(src):
            src = "room"
        return {
            "id": pid or p.get("id") or uuid.uuid4().hex[:10],
            "room": room,
            "name": (str(p.get("name") or "").strip() or "Profil")[:40],
            "enabled": bool(p.get("enabled", True)),
            "independent": bool(p.get("independent")),
            "days": days,
            "date_enabled": bool(p.get("date_enabled")),
            "date_from": df, "date_to": dto,
            "slots": slots,
            "lights": lights[:40],
            "presence": p.get("presence") if p.get("presence") in PRESENCE else "always",
            "alarm": p.get("alarm") if p.get("alarm") in ALARM else "always",
            "lux_below_enabled": bool(p.get("lux_below_enabled")),
            "lux_below": num("lux_below", 0, 100000, 50),
            "lux_above_enabled": bool(p.get("lux_above_enabled")),
            "lux_above": num("lux_above", 0, 100000, 5),
            "lux_source": src,
            "mode": p.get("mode") if p.get("mode") in MODES else "color_temp",
            "brightness": int(num("brightness", 1, 100, 80)),
            "kelvin": int(num("kelvin", 2000, 6500, 3000)),
            "color": str(p.get("color")).upper() if RE_COL.match(str(p.get("color") or "")) else "#FFB060",
            "transition": num("transition", 0, 60, 1.0),
            "transition_off": num("transition_off", 0, 60, float(p.get("transition", 1.0))),
        }

    async def async_save(self, patch: dict) -> str:
        pid = patch.get("id")
        if pid and pid not in self.profiles:
            pid = None
        if not pid and sum(1 for p in self.profiles.values() if p["room"] == patch.get("room")) >= MAX_PROFILES_PER_ROOM:
            raise ValueError(f"Höchstens {MAX_PROFILES_PER_ROOM} Profile je Raum")
        base = {**DEFAULT_PROFILE, **(self.profiles.get(pid) or {}), **patch}
        p = self._clean(base, pid)
        self.profiles[p["id"]] = p
        # laufende Vorkommen: neue Aus-Zeit übernehmen, gelöschte Zeiten nicht mehr ausschalten
        for key, a in list(self.active.items()):
            if a["pid"] != p["id"]:
                continue
            i, start = a.get("slot", -1), dt_util.parse_datetime(a.get("start") or "")
            if 0 <= i < len(p["slots"]) and start and p["slots"][i].get("off"):
                end = _at(start.date(), p["slots"][i]["off"])
                if end <= start:
                    end += timedelta(days=1)
                a["end"], a["has_end"] = end.isoformat(), True
            else:
                a["has_end"] = False
        self._save()
        await self._process()
        return p["id"]

    async def async_delete(self, pid: str) -> None:
        if self.profiles.pop(pid, None) is None:
            raise ValueError("Profil nicht gefunden")
        for key in [k for k, a in self.active.items() if a["pid"] == pid]:
            self.active.pop(key)            # Lampen bleiben, wie sie sind
        for key in [k for k in self.fired if k.startswith(pid + "|")]:
            self.fired.pop(key)
        self._save()
        self._push()

    async def async_test(self, profile: dict) -> None:
        """Lichteinstellung sofort an den Lampen zeigen (ohne Bedingungen, ohne Besitz)."""
        p = self._clean({**DEFAULT_PROFILE, **profile}, profile.get("id") or "test")
        lamps = self.expand(p["lights"])
        if not lamps:
            raise ValueError("Keine Lampen gewählt")
        await self._turn_on(p, lamps)

    # ================================================================ Hilfen
    def _state(self, eid: str | None):
        return self.hass.states.get(eid) if eid else None

    def _txt(self, eid: str) -> str:
        s = self._state(eid)
        return s.state.strip() if s and s.state not in BAD else ""

    def expand(self, ents: list[str]) -> list[str]:
        """group.* auflösen (auch verschachtelt), Lichtgruppen (light.*) bleiben eine Lampe."""
        out: list[str] = []

        def add(e: str, depth: int) -> None:
            if e.startswith("group.") and depth < 4:
                s = self._state(e)
                for x in (s.attributes.get("entity_id") or []) if s else []:
                    add(x, depth + 1)
            elif e.split(".")[0] in ("light", "switch", "input_boolean") and e not in out:
                out.append(e)
        for e in ents:
            add(e, 0)
        return out

    def motion_slot(self, lamp: str) -> str | None:
        """Basis <raum>_light_motion_<licht>, wenn die Lampe zu einer Bewegungsgruppe gehört."""
        for r in self.rooms:
            for L in MOTION_LIGHTS:
                b = f"{r}_light_motion_{L}"
                g = self._state(f"group.{b}")
                if g and lamp in (g.attributes.get("entity_id") or []):
                    return b
        return None

    def _source(self, b: str) -> str:
        s = self._state(f"input_select.{b}_source")
        return s.state if s else ""

    def conditions(self, p: dict) -> tuple[bool, str]:
        """(erfüllt, Grund). Fehlende oder ungültige Quellen gelten als erfüllt (wie bei der Bewegung)."""
        sim = self._state(SRC_SIM)
        if sim and sim.state == "on" and sim.attributes.get(SIM_LOCK):
            return False, "Anwesenheitssimulation aktiv"
        if p["alarm"] != "always":
            a = self._state(self._txt(SRC_ALARM))
            if a and a.state not in BAD:
                if p["alarm"] == "disarmed" and a.state != "disarmed":
                    return False, "Alarmanlage scharf"
                if p["alarm"] == "not_away" and a.state not in ("disarmed", "armed_home", "armed_night"):
                    return False, "Alarmanlage abwesend"
        if p["presence"] != "always":
            e = self._txt(SRC_PRESENCE)
            s = self._state(e)
            if s and s.state not in BAD:
                home = s.state in ("on", "home") or (e.startswith("zone.") and s.state.isdigit() and int(s.state) > 0)
                if p["presence"] == "home" and not home:
                    return False, "Niemand zuhause"
                if p["presence"] == "away" and home:
                    return False, "Jemand zuhause"
        if p["lux_below_enabled"] or p["lux_above_enabled"]:
            src = p["lux_source"]
            e = self._txt(SRC_LUX_ROOM.format(room=p["room"])) if src == "room" else self._txt(SRC_LUX_OUTDOOR) if src == "outdoor" else src
            s = self._state(e)
            try:
                lux = float(s.state) if s else None
            except ValueError:
                lux = None
            if lux is not None:
                if p["lux_below_enabled"] and lux >= p["lux_below"]:
                    return False, f"Zu hell ({lux:g} lx)"
                if p["lux_above_enabled"] and lux <= p["lux_above"]:
                    return False, f"Zu dunkel ({lux:g} lx)"
        return True, ""

    # ================================================================ Überblendzeit
    @staticmethod
    def _fade(kind: str, p: dict | None) -> float:
        """Überblendzeit des Profils: 'on' = transition, 'off' = transition_off (sonst wie 'on')."""
        p = p or {}
        v = p.get("transition_off", p.get("transition")) if kind == "off" else p.get("transition")
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 1.0

    async def _turn_off(self, ents: list[str], p: dict | None = None) -> None:
        """Lampen ausblenden, alles andere normal ausschalten."""
        lights = [e for e in ents if e.startswith("light.")]
        other = [e for e in ents if not e.startswith("light.")]
        if lights:
            await self.hass.services.async_call("light", "turn_off", {"entity_id": lights, "transition": self._fade("off", p)}, context=self._ctx())
        if other:
            await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": other}, context=self._ctx())

    def _ctx(self) -> Context:
        return Context(parent_id=self.root.id)

    async def _turn_on(self, p: dict, lamps: list[str]) -> None:
        """Jede Lampe bekommt, was sie kann (wie script.light_motion_apply)."""
        groups: dict[tuple, list[str]] = {}
        h = p["color"].lstrip("#")
        rgb = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
        mode, tr = p["mode"], self._fade("on", p)
        for e in lamps:
            dom = e.split(".")[0]
            if dom != "light":
                groups.setdefault(("other",), []).append(e)
                continue
            s = self._state(e)
            modes = set((s.attributes.get("supported_color_modes") if s else None) or [])
            has_color, has_ct, has_bri = bool(modes & COLOR_MODES), "color_temp" in modes, bool(modes - {"onoff"})
            if mode == "on":
                groups.setdefault(("plain",), []).append(e)
            elif mode in ("color", "auto") and has_color:
                groups.setdefault(("color",), []).append(e)
            elif mode in ("color_temp", "auto") and has_ct:
                groups.setdefault(("ct",), []).append(e)
            elif has_bri:
                groups.setdefault(("bri",), []).append(e)
            else:
                groups.setdefault(("plain",), []).append(e)
        for (k,), ents in groups.items():
            if k == "other":
                await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": ents}, context=self._ctx())
                continue
            data: dict[str, Any] = {"entity_id": ents}
            if k != "plain":
                data["brightness_pct"] = p["brightness"]
                data["transition"] = tr
            if k == "color":
                data["rgb_color"] = rgb
            elif k == "ct":
                data["color_temp_kelvin"] = p["kelvin"]
            await self.hass.services.async_call("light", "turn_on", data, context=self._ctx())

    async def _set_source(self, b: str, opt: str) -> None:
        if self._state(f"input_select.{b}_source"):
            await self.hass.services.async_call("input_select", "select_option",
                                                {"entity_id": f"input_select.{b}_source", "option": opt}, context=self._ctx())

    # ================================================================ Ablauf
    async def _tick(self, _now: datetime) -> None:
        await self._process()

    async def _process(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            changed = await self._ends()
            changed |= await self._starts()
            self._prune()
            if changed:
                self._save()
            self._push()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Zeitsteuerung: Fehler beim Prüfen der Profile")
        finally:
            self._busy = False

    async def _ends(self) -> bool:
        now, changed = dt_util.now(), False
        for key, a in list(self.active.items()):
            at = dt_util.parse_datetime(a.get("at") or "") or now
            # Lampen, die inzwischen jemand ausgeschaltet hat, gehören dem Profil nicht mehr
            if (now - at).total_seconds() > OFF_GRACE_S:
                keep = [e for e in a["lamps"] if (s := self._state(e)) and s.state == "on"]
                if keep != a["lamps"]:
                    a["lamps"], changed = keep, True
            end = dt_util.parse_datetime(a.get("end") or "")
            if not a["lamps"] or not a.get("has_end"):
                if not a["lamps"] or (end and now >= end):
                    self.active.pop(key)
                    changed = True
                continue
            if end and now >= end:
                self.active.pop(key)
                changed = True
                if (now - end).total_seconds() > END_CATCH_UP_H * 3600:
                    continue
                await self._switch_off(a)
        return changed

    async def _switch_off(self, a: dict) -> None:
        # Lampen, die ein anderes noch laufendes Vorkommen übernommen hat, bleiben an
        taken = {e for x in self.active.values() for e in x["lamps"]}
        off, bases = [], set()
        for e in a["lamps"]:
            if e in taken:
                continue
            b = None if a.get("independent") else self.motion_slot(e)
            if b and self._source(b) != "schedule":
                continue            # Bewegung oder Hand hat das Licht übernommen
            off.append(e)
            if b:
                bases.add(b)
        if not off:
            return
        p = self.profiles.get(a["pid"]) or {}
        _LOGGER.info("Zeitsteuerung %s: aus %s", p.get("name", a["pid"]), off)
        await self._turn_off(off, p)
        self.hass.bus.async_fire(EVENT_SWITCHED, {"profile_id": a["pid"], "name": p.get("name"), "room": p.get("room"), "action": "off", "lights": off}, context=self._ctx())

    async def _starts(self) -> bool:
        now, changed = dt_util.now(), False
        self.last_check = {}
        for pid, p in self.profiles.items():
            if not p["enabled"] or not p["lights"]:
                continue
            for i, start, end, has_end in occurrences(p, now):
                if not (start <= now < end):
                    continue
                key = f"{pid}|{i}|{start.isoformat()}"
                if key in self.fired:
                    continue
                ok, why = self.conditions(p)
                if not ok:
                    self.last_check[pid] = why
                    continue
                lamps, blocked = await self._fire(p, i, start, end, has_end, key)
                changed = True
                if blocked and not lamps:
                    # Bewegung hält das Licht (Vorrang Bewegung) → später erneut versuchen, solange das Fenster offen ist
                    self.last_check[pid] = "Bewegung hat Vorrang"
                    continue
                self.fired[key] = end.isoformat()
                _LOGGER.info("Zeitsteuerung %s (%s): ein %s", p["name"], p["slots"][i]["on"], lamps)
        return changed

    async def _fire(self, p: dict, i: int, start: datetime, end: datetime, has_end: bool, key: str) -> tuple[list[str], bool]:
        owned = {e: k for k, x in self.active.items() for e in x["lamps"]}
        take, bases, blocked = [], set(), False
        if p["independent"]:
            # Unabhängig: alle Lampen einschalten, Bewegung/Hand/Helligkeit weder prüfen noch beeinflussen.
            # Zum Ende nur ausschalten, was vorher aus war (oder einem anderen Profil gehörte).
            lamps = self.expand(p["lights"])
            if not lamps:
                return [], False
            keep = [e for e in lamps if not ((s := self._state(e)) and s.state == "on") or e in owned]
            for k, x in self.active.items():
                x["lamps"] = [e for e in x["lamps"] if e not in lamps]
            await self._turn_on(p, lamps)
            self.active[key] = {"pid": p["id"], "slot": i, "start": start.isoformat(), "end": end.isoformat(),
                                "has_end": has_end, "lamps": keep, "independent": True, "at": dt_util.now().isoformat()}
            self.hass.bus.async_fire(EVENT_SWITCHED, {"profile_id": p["id"], "name": p["name"], "room": p["room"], "action": "on", "lights": lamps}, context=self._ctx())
            return lamps, False
        for e in self.expand(p["lights"]):
            s = self._state(e)
            is_on = bool(s and s.state == "on")
            b = self.motion_slot(e)
            if b:
                src = self._source(b)
                ps = self._state(f"input_select.{b}_cfg_priority_schedule")
                prio = ps.state if ps else "function"
                if src == "motion":
                    if prio == "motion":
                        blocked = True
                        continue            # Bewegung hat Vorrang und behält das Licht
                elif is_on and src != "schedule" and e not in owned:
                    continue                # von Hand o. Ä. an → nicht anfassen
                bases.add(b)
            elif is_on and e not in owned:
                continue                    # schon an, nicht von uns → nicht anfassen
            take.append(e)
        if not take:
            return [], blocked
        for b in bases:
            if self._source(b) != "schedule":
                await self._set_source(b, "schedule")
        for k, x in self.active.items():   # von einem anderen Vorkommen übernehmen
            x["lamps"] = [e for e in x["lamps"] if e not in take]
        await self._turn_on(p, take)
        self.active[key] = {"pid": p["id"], "slot": i, "start": start.isoformat(), "end": end.isoformat(),
                            "has_end": has_end, "lamps": take, "at": dt_util.now().isoformat()}
        self.hass.bus.async_fire(EVENT_SWITCHED, {"profile_id": p["id"], "name": p["name"], "room": p["room"], "action": "on", "lights": take}, context=self._ctx())
        return take, blocked

    def _prune(self) -> None:
        now = dt_util.now()
        for k, end in list(self.fired.items()):
            e = dt_util.parse_datetime(end)
            if not e or (now - e).total_seconds() > 2 * 86400:
                self.fired.pop(k)

    # ================================================================ Anzeige
    def next_switch(self, p: dict) -> dict | None:
        """Nächstes Ein (oder Aus eines laufenden Vorkommens), bis 1 Jahr voraus."""
        if not p["enabled"] or not p["lights"] or not p["slots"]:
            return None
        now, cands = dt_util.now(), []
        for a in self.active.values():
            if a["pid"] == p["id"] and a.get("has_end") and a["lamps"]:
                cands.append((dt_util.parse_datetime(a["end"]), "off", a["slot"]))
        # erster Tag (bis 1 Jahr voraus) mit passendem Wochentag und Zeitraum, der noch eine Ein-Zeit hat
        for d in range(0, 367):
            day = (now + timedelta(days=d)).date()
            if day.weekday() not in p["days"] or not in_date_range(p, day):
                continue
            ons = [(_at(day, sl["on"]), "on", i) for i, sl in enumerate(p["slots"])]
            ons = [c for c in ons if c[0] > now]
            if ons:
                cands += ons
                break
        cands = [c for c in cands if c[0]]
        if not cands:
            return None
        at, action, slot = min(cands, key=lambda c: c[0])
        return {"at": at.isoformat(), "action": action, "slot": slot}

    def room_view(self, room: str) -> dict:
        profs = []
        for p in sorted((x for x in self.profiles.values() if x["room"] == room), key=lambda x: x["name"].lower()):
            run = [a for a in self.active.values() if a["pid"] == p["id"] and a["lamps"]]
            profs.append({**p, "lamps": self.expand(p["lights"]), "next": self.next_switch(p),
                          "running": [{"slot": a["slot"], "until": a["end"] if a.get("has_end") else None, "lamps": a["lamps"]} for a in run],
                          "waiting": self.last_check.get(p["id"])})
        running = sorted({e for p in profs for r in p["running"] for e in r["lamps"]})
        nxt = sorted((dict(p["next"], profile=p["name"]) for p in profs if p["next"]), key=lambda x: x["at"])
        return {"profiles": profs, "running_lamps": running, "next": nxt[0] if nxt else None, "version": VERSION}

    @callback
    def _push(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
