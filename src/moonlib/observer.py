"""The :class:`Observer` model.

The legacy API threaded bare ``lat`` / ``lon`` floats through every function.
An explicit observer object is the right abstraction for a service that will
eventually store named observing sites for a web UI or a mobile app, and it
gives the timezone and elevation a natural home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Dict, Optional, Sequence, Union

from .errors import ObserverError
from .timeutil import (
    ensure_utc,
    format_local,
    format_offset,
    resolve_timezone,
    to_local,
)

__all__ = ["Observer"]

#: Default observing site, retained from the legacy library.
DEFAULT_LATITUDE = 30
DEFAULT_LONGITUDE = 120


@dataclass(frozen=True)
class Observer:
    """A geographical observing site.

    Parameters
    ----------
    lat
        Geodetic latitude in degrees, ``-90 .. +90`` (north positive).
    lon
        Geodetic longitude in degrees, east positive.  Values outside
        ``-180 .. 180`` are normalised.
    height_km
        Elevation above the reference ellipsoid in kilometres.
    name
        Optional human readable label, useful for reports and API payloads.
    tz
        Explicit timezone.  When omitted it is resolved from the coordinates
        on first use (offline, via ``timezonefinder``).
    """

    lat: float = DEFAULT_LATITUDE
    lon: float = DEFAULT_LONGITUDE
    height_km: float = 0.0
    name: str = ""
    _tz: Optional[tzinfo] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        lat = float(self.lat)
        lon = float(self.lon)
        height = float(self.height_km)
        if not -90.0 <= lat <= 90.0:
            raise ObserverError(f"Latitude {lat} out of range [-90, 90]")
        if not -1000.0 <= height <= 100000.0:
            raise ObserverError(f"Implausible elevation {height} km")
        # normalise longitude into [-180, 180)
        lon = (lon + 180.0) % 360.0 - 180.0
        object.__setattr__(self, "lat", lat)
        object.__setattr__(self, "lon", lon)
        object.__setattr__(self, "height_km", height)

    # -- construction helpers ----------------------------------------------
    @classmethod
    def from_value(cls, value: Union["Observer", Sequence[float], None]) -> "Observer":
        """Coerce ``None`` / ``(lat, lon)`` / :class:`Observer` into an Observer."""
        if value is None:
            return cls()
        if isinstance(value, Observer):
            return value
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            height = float(value[2]) if len(value) > 2 else 0.0
            return cls(lat=float(value[0]), lon=float(value[1]), height_km=height)
        raise ObserverError(
            "observer must be an Observer, a (lat, lon[, height_km]) sequence, or None"
        )

    # -- timezone -----------------------------------------------------------
    @property
    def timezone(self) -> tzinfo:
        """Timezone of the site (explicit if given, otherwise resolved)."""
        if self._tz is not None:
            return self._tz
        return resolve_timezone(self.lat, self.lon)

    @property
    def utc_offset_label(self) -> str:
        """Current UTC offset, e.g. ``UTC+08:00``."""
        return format_offset(self.timezone)

    def local_time(self, when: datetime) -> datetime:
        """Convert an instant to local time at the site."""
        return to_local(ensure_utc(when), self.timezone)

    def format_local(self, when: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """Format an instant in local time with a UTC-offset suffix."""
        return format_local(ensure_utc(when), self.timezone, fmt)

    # -- serialisation ------------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "height_km": self.height_km,
            "name": self.name,
            "timezone": str(self.timezone),
            "utc_offset": self.utc_offset_label,
        }

    def with_timezone(self, tz: tzinfo) -> "Observer":
        """Copy of this observer with an explicit timezone."""
        return Observer(
            lat=self.lat, lon=self.lon, height_km=self.height_km, name=self.name, _tz=tz
        )


def as_observer(value: Union[Observer, Sequence[float], None]) -> Observer:
    """Module level alias of :meth:`Observer.from_value`."""
    return Observer.from_value(value)
