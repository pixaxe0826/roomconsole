"""LLM-independent service protocol; use registry.execute at a trusted boundary."""
from .core import ExecutionContext, WidgetAdapter, WidgetRegistry, request_digest
from .models import Capability, WidgetEvent, WidgetRequest, WidgetResponse
from .adapters import AlarmAdapter, CalendarAdapter, MemoAdapter, TodoAdapter, TaskServices, build_registry
from .http import register_routes

_SYSTEM_ADAPTER_EXPORTS = {
    'ClockAdapter',
    'FullAlarmAdapter',
    'FullCalendarAdapter',
    'FullMemoAdapter',
    'FullTodoAdapter',
    'IndoorSensorAdapter',
    'LLMDisplayAdapter',
    'LLMResponseAdapter',
    'LayoutAdapter',
    'SettingsAdapter',
    'SystemAdapter',
    'SystemStatusAdapter',
    'WeatherAdapter',
    'build_full_registry',
}

__all__ = [
    'WidgetRequest', 'WidgetResponse', 'WidgetEvent', 'Capability', 'ExecutionContext',
    'WidgetAdapter', 'WidgetRegistry', 'request_digest', 'MemoAdapter', 'TodoAdapter',
    'CalendarAdapter', 'AlarmAdapter', 'TaskServices', 'build_registry', 'register_routes',
    'FullMemoAdapter', 'FullTodoAdapter', 'FullCalendarAdapter', 'FullAlarmAdapter',
    'ClockAdapter', 'WeatherAdapter', 'IndoorSensorAdapter', 'LLMDisplayAdapter',
    'LLMResponseAdapter', 'LayoutAdapter', 'SettingsAdapter', 'SystemAdapter',
    'SystemStatusAdapter', 'build_full_registry',
]


def __getattr__(name: str):
    if name in _SYSTEM_ADAPTER_EXPORTS:
        from . import system_adapters

        val = getattr(system_adapters, name)
        globals()[name] = val
        return val
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
