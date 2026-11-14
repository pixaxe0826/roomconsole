"""Canonical settings persistence shared by HTTP and confirmed widget adapters.

Only SQLite state changes here. Weather scheduling and notifications belong to
the caller after commit, so an enclosing agent unit of work can roll back both
settings and the invalidated weather snapshot together.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import HubSettings
from .store import Store


WEATHER_SETTING_FIELDS = ('latitude', 'longitude', 'location_name', 'timezone')


def weather_settings_key(settings: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(settings.get(field) for field in WEATHER_SETTING_FIELDS)


def apply_settings(
    store: Store,
    values: Mapping[str, Any] | HubSettings,
    *,
    patch: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Validate and atomically persist settings, returning weather invalidation.

    Partial updates distinguish an omitted coordinate from an explicit null;
    clearing a location therefore requires setting both coordinates to null.
    The full canonical model, including paired coordinates and IANA timezone,
    is validated after merging. Validation never leaves partially written data.
    """
    supplied = values.model_dump() if isinstance(values, HubSettings) else dict(values)
    with store.unit_of_work():
        current = store.get('settings') or {}
        candidate = {**current, **supplied} if patch else supplied
        updated = HubSettings.model_validate(candidate).model_dump()
        invalidated = weather_settings_key(current) != weather_settings_key(updated)
        store.set('settings', updated)
        if invalidated:
            store.set('weather', None)
    return updated, invalidated
