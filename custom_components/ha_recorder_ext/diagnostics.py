from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DB_PATH,
    CONF_DB_TYPE,
    CONF_EXCLUDE_ATTRIBUTES,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_FAILED_FLUSH_THRESHOLD,
    CONF_FLUSH_INTERVAL,
    CONF_QUEUE_MAX_SIZE,
    DEFAULT_FAILED_FLUSH_THRESHOLD,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_QUEUE_MAX_SIZE,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    from . import RecorderCoordinator

    coordinator: RecorderCoordinator = entry.runtime_data
    return {
        "config": {
            CONF_DB_TYPE: entry.data.get(CONF_DB_TYPE),
            CONF_DB_PATH: entry.data.get(CONF_DB_PATH),
            CONF_EXCLUDE_DOMAINS: entry.data.get(CONF_EXCLUDE_DOMAINS, []),
            CONF_EXCLUDE_ENTITIES: entry.data.get(CONF_EXCLUDE_ENTITIES, []),
            CONF_EXCLUDE_ATTRIBUTES: entry.data.get(CONF_EXCLUDE_ATTRIBUTES, []),
        },
        "options": {
            CONF_FLUSH_INTERVAL: entry.options.get(
                CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL
            ),
            CONF_QUEUE_MAX_SIZE: entry.options.get(
                CONF_QUEUE_MAX_SIZE, DEFAULT_QUEUE_MAX_SIZE
            ),
            CONF_FAILED_FLUSH_THRESHOLD: entry.options.get(
                CONF_FAILED_FLUSH_THRESHOLD, DEFAULT_FAILED_FLUSH_THRESHOLD
            ),
        },
        "coordinator": {
            "is_running": coordinator.is_running,
            "queue_size": coordinator.queue_size,
            "flush_interval": coordinator.flush_interval,
            "last_flush": (
                coordinator.last_flush.isoformat()
                if coordinator.last_flush is not None
                else None
            ),
            "last_successful_write": (
                coordinator.last_successful_write.isoformat()
                if coordinator.last_successful_write is not None
                else None
            ),
            "has_write_problem": coordinator.has_write_problem,
            "consecutive_failed_flushes": coordinator.consecutive_failed_flushes,
            "failed_snapshots_total": coordinator.failed_snapshots_total,
            "dropped_snapshots_total": coordinator.dropped_snapshots_total,
            "last_error_type": coordinator.last_error_type,
            "last_error_at": (
                coordinator.last_error_at.isoformat()
                if coordinator.last_error_at is not None
                else None
            ),
        },
    }
