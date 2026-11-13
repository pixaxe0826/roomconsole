"""LLM-independent service protocol; use registry.execute at a trusted boundary."""
from .core import ExecutionContext, WidgetAdapter, WidgetRegistry, request_digest
from .models import Capability, WidgetEvent, WidgetRequest, WidgetResponse
from .adapters import AlarmAdapter, CalendarAdapter, MemoAdapter, TodoAdapter, TaskServices, build_registry
from .system_adapters import (
    ClockAdapter,
    FullAlarmAdapter,
    FullMemoAdapter,
    FullTodoAdapter,
    LLMDisplayAdapter,
    LayoutAdapter,
    SettingsAdapter,
    SystemAdapter,
    WeatherAdapter,
    build_full_registry,
)
from .http import register_routes

__all__ = [
    'WidgetRequest', 'WidgetResponse', 'WidgetEvent', 'Capability', 'ExecutionContext',
    'WidgetAdapter', 'WidgetRegistry', 'request_digest', 'MemoAdapter', 'TodoAdapter',
    'CalendarAdapter', 'AlarmAdapter', 'TaskServices', 'build_registry', 'register_routes',
    'FullMemoAdapter', 'FullTodoAdapter', 'FullAlarmAdapter', 'ClockAdapter', 'WeatherAdapter',
    'LLMDisplayAdapter', 'LayoutAdapter', 'SettingsAdapter', 'SystemAdapter', 'build_full_registry',
]
