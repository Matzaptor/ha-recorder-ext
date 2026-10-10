from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_recorder_ext import RecorderCoordinator
from custom_components.ha_recorder_ext.const import (
    CONF_DB_PATH,
    CONF_DB_TYPE,
    DB_TYPE_SQLITE,
    DOMAIN,
    DEFAULT_FAILED_FLUSH_THRESHOLD,
)

_ENTITY_RECORDING = "binary_sensor.ha_external_recorder_recording"
_ENTITY_IMPORT = "binary_sensor.ha_external_recorder_import_in_progress"
_ENTITY_PROBLEM = "binary_sensor.ha_external_recorder_recording_problem"


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HA External Recorder",
        data={
            CONF_DB_TYPE: DB_TYPE_SQLITE,
            CONF_DB_PATH: "ha_recorder_ext/ha_recorder_ext.db",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _mock_backend() -> AsyncMock:
    backend = AsyncMock()
    backend.get_latest_observation.return_value = None
    backend.get_or_create_entity.return_value = uuid.uuid4()
    backend.has_observations_in_range.return_value = False
    return backend


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with patch(
        "custom_components.ha_recorder_ext.create_backend",
        return_value=_mock_backend(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


class TestRecordingActiveSensor:
    async def test_entity_created(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        assert hass.states.get(_ENTITY_RECORDING) is not None

    async def test_on_while_coordinator_running(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        assert hass.states.get(_ENTITY_RECORDING).state == "on"

    async def test_has_queue_size_attribute(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        assert "queue_size" in hass.states.get(_ENTITY_RECORDING).attributes

    async def test_has_flush_interval_attribute(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        assert "flush_interval_seconds" in hass.states.get(_ENTITY_RECORDING).attributes

    async def test_turns_off_when_coordinator_stops(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)

        coordinator: RecorderCoordinator = entry.runtime_data
        # stop() sets _stopped, notifies listeners, and cancels the flush task
        await coordinator.stop()
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_RECORDING).state == "off"


class TestImportProgressSensor:
    async def test_entity_created(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        assert hass.states.get(_ENTITY_IMPORT) is not None

    async def test_off_initially(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        assert hass.states.get(_ENTITY_IMPORT).state == "off"

    async def test_turns_on_when_import_starts(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)

        coordinator: RecorderCoordinator = entry.runtime_data
        coordinator.async_set_importing(True)
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_IMPORT).state == "on"

    async def test_turns_off_when_import_ends(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)

        coordinator: RecorderCoordinator = entry.runtime_data
        coordinator.async_set_importing(True)
        coordinator.async_set_importing(False, count=42)
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_IMPORT).state == "off"

    async def test_last_import_count_in_attributes(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)

        coordinator: RecorderCoordinator = entry.runtime_data
        coordinator.async_set_importing(False, count=42)
        await hass.async_block_till_done()

        attrs = hass.states.get(_ENTITY_IMPORT).attributes
        assert attrs["last_import_intervals_inserted"] == 42

    async def test_no_count_attribute_before_first_import(
        self, hass: HomeAssistant
    ) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)

        attrs = hass.states.get(_ENTITY_IMPORT).attributes
        assert "last_import_intervals_inserted" not in attrs


class TestRecordingProblemSensor:
    @staticmethod
    async def _fail_flushes(coordinator: RecorderCoordinator, times: int) -> None:
        from datetime import datetime, timezone

        from custom_components.ha_recorder_ext import _StateSnapshot

        coordinator.backend.get_or_create_entity.side_effect = RuntimeError("boom")
        for _ in range(times):
            coordinator._queue.put_nowait(
                _StateSnapshot("sensor.t", "1", {}, datetime.now(timezone.utc))
            )
            await coordinator._flush()

    async def test_entity_created_and_off_initially(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        state = hass.states.get(_ENTITY_PROBLEM)
        assert state is not None
        assert state.state == "off"
        assert state.attributes["device_class"] == "problem"

    async def test_is_diagnostic_entity(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        reg_entry = er.async_get(hass).async_get(_ENTITY_PROBLEM)
        assert reg_entry is not None
        assert reg_entry.entity_category == EntityCategory.DIAGNOSTIC

    async def test_stays_off_below_threshold(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        coordinator: RecorderCoordinator = entry.runtime_data

        await self._fail_flushes(coordinator, DEFAULT_FAILED_FLUSH_THRESHOLD - 1)
        await hass.async_block_till_done()

        state = hass.states.get(_ENTITY_PROBLEM)
        assert state.state == "off"
        assert state.attributes["consecutive_failed_flushes"] == DEFAULT_FAILED_FLUSH_THRESHOLD - 1

    async def test_turns_on_at_threshold_with_attributes(
        self, hass: HomeAssistant
    ) -> None:
        entry = _make_entry(hass)
        await _setup(hass, entry)
        coordinator: RecorderCoordinator = entry.runtime_data

        await self._fail_flushes(coordinator, DEFAULT_FAILED_FLUSH_THRESHOLD)
        await hass.async_block_till_done()

        state = hass.states.get(_ENTITY_PROBLEM)
        assert state.state == "on"
        attrs = state.attributes
        assert attrs["last_error_type"] == "RuntimeError"
        assert attrs["failed_snapshots_total"] == DEFAULT_FAILED_FLUSH_THRESHOLD
        assert attrs["last_error_at"] is not None
        assert attrs["last_successful_write"] is None

    async def test_turns_off_after_successful_flush(self, hass: HomeAssistant) -> None:
        from datetime import datetime, timezone

        from custom_components.ha_recorder_ext import _StateSnapshot

        entry = _make_entry(hass)
        await _setup(hass, entry)
        coordinator: RecorderCoordinator = entry.runtime_data
        await self._fail_flushes(coordinator, DEFAULT_FAILED_FLUSH_THRESHOLD)

        coordinator.backend.get_or_create_entity.side_effect = None
        coordinator.backend.get_or_create_entity.return_value = uuid.uuid4()
        coordinator._queue.put_nowait(
            _StateSnapshot("sensor.t", "1", {}, datetime.now(timezone.utc))
        )
        await coordinator._flush()
        await hass.async_block_till_done()

        state = hass.states.get(_ENTITY_PROBLEM)
        assert state.state == "off"
        assert state.attributes["last_successful_write"] is not None
