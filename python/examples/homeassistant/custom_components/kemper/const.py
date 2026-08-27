"""Constants shared by the Kemper Profiler integration."""

from __future__ import annotations

DOMAIN = "kemper"

#: What the device is called before discovery has told us otherwise.
DEFAULT_NAME = "Kemper Profiler"
#: Device-registry identity, fixed for every Profiler.
MANUFACTURER = "Kemper"
MODEL = "Profiler"

#: Entry data keys beyond ``CONF_HOST`` / ``CONF_PORT`` / ``CONF_NAME``: what
#: discovery told us about the device, kept so the device registry can show it
#: without re-polling.
CONF_SERIAL = "serial"
CONF_SW_VERSION = "sw_version"

#: How long a broadcast poll listens for replies, in seconds.
DISCOVERY_SECONDS = 3.0
#: How long a poll aimed at one known address listens: the device is either
#: there and answers at once, or it is not.
DIRECTED_DISCOVERY_SECONDS = 1.5

#: Options-flow keys and their defaults. The window is in minutes and the
#: threshold in percent of the meter lane's full scale, because those are the
#: units the person filling in the form thinks in; the detector converts.
CONF_ACTIVITY_WINDOW = "activity_window"
CONF_ACTIVITY_THRESHOLD = "activity_threshold"
DEFAULT_ACTIVITY_WINDOW = 5.0
DEFAULT_ACTIVITY_THRESHOLD = 2.0
