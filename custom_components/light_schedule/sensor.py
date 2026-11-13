"""sensor.<raum>_light_schedule: Anzahl aktiver Profile; Attribute = Profile, laufende Lampen, nächstes Schalten."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .manager import ScheduleManager


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    mgr: ScheduleManager = hass.data[DOMAIN]
    async_add_entities([RoomSensor(mgr, r) for r in mgr.rooms])


class RoomSensor(SensorEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"
    _attr_native_unit_of_measurement = "Profile"
    _unrecorded_attributes = frozenset({"profiles", "running_lamps", "next", "version"})

    def __init__(self, mgr: ScheduleManager, room: str) -> None:
        self.mgr = mgr
        self.room = room
        self._attr_name = f"{room.upper()} Licht Zeitsteuerung"
        self._attr_unique_id = f"{DOMAIN}_{room}"
        self.entity_id = f"sensor.{room}_light_schedule"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return sum(1 for p in self.mgr.profiles.values() if p["room"] == self.room and p["enabled"])

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.room_view(self.room)
