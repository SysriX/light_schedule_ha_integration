"""Konstanten der Zeitsteuerung (light_schedule)."""

DOMAIN = "light_schedule"
VERSION = "1.4.0"

CONF_ROOMS = "rooms"
DEFAULT_ROOMS = ["wz", "bu", "ku", "sz", "fl", "ke", "ba", "bz"]

STORE_KEY = "light_schedule"
STORE_VERSION = 1

TICK_SECONDS = [0, 20, 40]     # Prüftakt (Sekunde 0 = genau zur Startminute)
NO_END_WINDOW_MIN = 15         # ohne Aus-Zeit: Bedingungen bis 15 min nach der Startzeit prüfen
END_CATCH_UP_H = 12            # verpasstes Ausschalten (HA war aus) bis 12 h später nachholen
OFF_GRACE_S = 30               # so lange nach dem Einschalten zählt "aus" noch nicht als "jemand hat ausgeschaltet"
MAX_PROFILES_PER_ROOM = 30
MAX_SLOTS = 8

SIGNAL_UPDATE = "light_schedule_update"
EVENT_SWITCHED = "light_schedule_switched"

# Hausweite / raumweise Quellen – dieselben Helfer wie die Bewegungssteuerung (light_motion)
SRC_ALARM = "input_text.haus_light_motion_cfg_alarm_panel"
# Anwesenheitssimulation (presence_sim): solange sie läuft und Attribut lock_schedule wahr ist, schaltet dieses Profil nicht ein
SRC_SIM = "binary_sensor.haus_sys_presence_sim_active"
SIM_LOCK = "lock_schedule"
SRC_PRESENCE = "input_text.haus_light_motion_cfg_presence_sensor"
SRC_LUX_OUTDOOR = "input_text.haus_light_motion_cfg_lux_sensor"
SRC_LUX_ROOM = "input_text.{room}_light_motion_cfg_lux_sensor"
MOTION_LIGHTS = ("primary", "secondary")

MODES = ["color_temp", "color", "auto", "brightness", "on"]
PRESENCE = ["always", "home", "away"]
ALARM = ["always", "disarmed", "not_away"]

DEFAULT_PROFILE = {
    "name": "Neues Profil",
    "enabled": True,
    "independent": False,         # unabhängig von Bewegung/Hand/Helligkeit (schaltet immer, beeinflusst nichts)
    "days": [0, 1, 2, 3, 4, 5, 6],
    "date_enabled": False,        # nur in einem Zeitraum (jedes Jahr), auch über den Jahreswechsel
    "date_from": "12-01",         # MM-TT
    "date_to": "12-31",
    "slots": [{"on": "18:00", "off": "22:00"}],
    "lights": [],
    "presence": "always",
    "alarm": "always",
    "lux_below_enabled": False,   # nur einschalten, wenn dunkler als …
    "lux_below": 50,
    "lux_above_enabled": False,   # nur einschalten, wenn heller als …
    "lux_above": 5,
    "lux_source": "room",         # room | outdoor | <sensor-entity>
    "mode": "color_temp",
    "brightness": 80,
    "kelvin": 3000,
    "color": "#FFB060",
    "transition": 1.0,
    "transition_off": 1.0,
}
