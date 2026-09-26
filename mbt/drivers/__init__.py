"""Per-vendor battery drivers.

Importing this package registers every available driver. Drivers are added in
descending order of protocol certainty; see the plan for the per-vendor
protocol notes.
"""

from .base import Driver, Reading, all_drivers, device_key, register  # noqa: F401

# Imported for their registration side effect. Order matters: the first driver
# to claim a device key wins, so specific vendor drivers must come before any
# generic fallback.
from . import (  # noqa: E402,F401
    logitech,
    razer,
    pulsar,
    ipi,
    orbital,
    vaxee,
    compx,
    ninjutso,
    finalmouse,
    zowie,
)

__all__ = ["Driver", "Reading", "all_drivers", "device_key", "register"]
