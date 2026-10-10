from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_recorder_ext.const import (
    CONF_DB_PATH,
    CONF_DB_TYPE,
    CONF_EXCLUDE_ATTRIBUTES,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_FLUSH_INTERVAL,
    CONF_QUEUE_MAX_SIZE,
    DB_TYPE_MYSQL,
    DB_TYPE_SQLITE,
    DOMAIN,
    FAILED_FLUSH_THRESHOLD,
    ISSUE_RECORDING_FAILING,
    SERVICE_IMPORT_FROM_EXTERNAL_DB,
    SERVICE_IMPORT_FROM_RECORDER,
)


def _make_entry(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    data = {
        CONF_DB_TYPE: DB_TYPE_SQLITE,
        CONF_DB_PATH: "test.db",
        CONF_EXCLUDE_DOMAINS: [],
        CONF_EXCLUDE_ENTITIES: [],
        CONF_EXCLUDE_ATTRIBUTES: [],
        **overrides,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    return entry


def _mock_backend() -> MagicMock:
    backend = AsyncMock()
    backend.get_latest_observation.return_value = None
    backend.get_or_create_entity.return_value = __import__("uuid").uuid4()
    return backend


@pytest.fixture
def mock_backend():
    return _mock_backend()


@pytest.fixture
def patched_factory(mock_backend):
    with patch(
        "custom_components.ha_recorder_ext.create_backend",
        return_value=mock_backend,
    ):
        yield mock_backend


class TestSetupAndUnload:
    async def test_setup_creates_coordinator(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hasattr(entry, "runtime_data")
        assert entry.runtime_data.is_running

    async def test_unload_stops_coordinator(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not coordinator.is_running

    async def test_unload_closes_backend(
        self, hass: HomeAssistant, patched_factory, mock_backend
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        mock_backend.close.assert_called_once()

    async def test_setup_failure_sets_retry_state(
        self, hass: HomeAssistant
    ) -> None:
        from homeassistant.config_entries import ConfigEntryState

        entry = _make_entry(hass)
        with patch(
            "custom_components.ha_recorder_ext.create_backend"
        ) as mock_factory:
            mock_backend = AsyncMock()
            mock_backend.initialize.side_effect = OSError("disk full")
            mock_factory.return_value = mock_backend

            # HA catches ConfigEntryNotReady internally and schedules a retry.
            await hass.config_entries.async_setup(entry.entry_id)
            assert entry.state == ConfigEntryState.SETUP_RETRY


class TestGracefulShutdown:
    async def test_ha_stop_triggers_flush(
        self, hass: HomeAssistant, patched_factory, mock_backend
    ) -> None:
        from custom_components.ha_recorder_ext import _StateSnapshot

        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        # Pre-populate queue so flush has actual work to do
        coordinator._queue.put_nowait(
            _StateSnapshot(
                entity_id="sensor.test",
                state="25.0",
                attributes={},
                ts=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
            )
        )

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

        # begin() is invoked during flush when the queue is non-empty
        assert mock_backend.begin.called

    async def test_stop_is_idempotent(
        self, hass: HomeAssistant, patched_factory, mock_backend
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        await coordinator.stop()
        await coordinator.stop()  # second call must not raise

        assert not coordinator.is_running


class TestEntityFilters:
    async def test_excluded_domain_not_tracked(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass, **{CONF_EXCLUDE_DOMAINS: ["automation"]})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert not coordinator._should_track_entity("automation.morning_routine")
        assert coordinator._should_track_entity("sensor.temperature")

    async def test_excluded_entity_not_tracked(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(
            hass, **{CONF_EXCLUDE_ENTITIES: ["sensor.noisy_sensor"]}
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert not coordinator._should_track_entity("sensor.noisy_sensor")
        assert coordinator._should_track_entity("sensor.temperature")

    async def test_excluded_attribute_not_tracked(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(
            hass, **{CONF_EXCLUDE_ATTRIBUTES: ["entity_picture", "icon"]}
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert not coordinator._should_track_attribute("entity_picture")
        assert not coordinator._should_track_attribute("icon")
        assert coordinator._should_track_attribute("unit_of_measurement")

    async def test_state_change_for_excluded_entity_is_ignored(
        self, hass: HomeAssistant, patched_factory, mock_backend
    ) -> None:
        entry = _make_entry(hass, **{CONF_EXCLUDE_DOMAINS: ["sun"]})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.states.async_set("sun.sun", "above_horizon")
        await hass.async_block_till_done()

        # Nothing should land in the queue for sun.sun
        coordinator = entry.runtime_data
        assert coordinator.queue_size == 0


class TestQueueBehavior:
    async def test_queue_full_during_start_drops_snapshot(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        """When the queue fills up during initial snapshot, extras are silently dropped."""
        hass.states.async_set("sensor.a", "1")
        hass.states.async_set("sensor.b", "2")
        hass.states.async_set("sensor.c", "3")
        await hass.async_block_till_done()

        # Queue max size of 1 forces overflow during start().
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_DB_TYPE: DB_TYPE_SQLITE,
                CONF_DB_PATH: "test.db",
                CONF_EXCLUDE_DOMAINS: [],
                CONF_EXCLUDE_ENTITIES: [],
                CONF_EXCLUDE_ATTRIBUTES: [],
            },
            options={CONF_FLUSH_INTERVAL: 30, CONF_QUEUE_MAX_SIZE: 1},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        # Queue may hold at most 1 item; the rest were dropped without raising.
        assert coordinator.queue_size <= 1
        assert coordinator.is_running

    async def test_queue_full_on_state_change_drops_event(
        self, hass: HomeAssistant
    ) -> None:
        """State-change events are dropped (not raised) when the queue is full."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_DB_TYPE: DB_TYPE_SQLITE,
                CONF_DB_PATH: "test.db",
                CONF_EXCLUDE_DOMAINS: [],
                CONF_EXCLUDE_ENTITIES: [],
                CONF_EXCLUDE_ATTRIBUTES: [],
            },
            options={CONF_FLUSH_INTERVAL: 300, CONF_QUEUE_MAX_SIZE: 1},
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=_mock_backend(),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            coordinator = entry.runtime_data
            # Fill the queue completely.
            from custom_components.ha_recorder_ext import _StateSnapshot
            while not coordinator._queue.full():
                coordinator._queue.put_nowait(
                    _StateSnapshot("sensor.x", "1", {}, datetime.now(timezone.utc))
                )

            # This state change must not raise despite a full queue.
            hass.states.async_set("sensor.extra", "42")
            await hass.async_block_till_done()


class TestImportServices:
    async def test_import_from_recorder_service_registered(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_FROM_RECORDER)

    async def test_import_from_external_db_service_registered(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_FROM_EXTERNAL_DB)

    async def test_import_services_removed_on_unload(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not hass.services.has_service(DOMAIN, SERVICE_IMPORT_FROM_RECORDER)
        assert not hass.services.has_service(DOMAIN, SERVICE_IMPORT_FROM_EXTERNAL_DB)

    async def test_import_from_external_db_missing_server_fields_raises(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_FROM_EXTERNAL_DB,
                {CONF_DB_TYPE: DB_TYPE_MYSQL},
                blocking=True,
            )

    async def test_import_from_external_db_missing_sqlite_path_raises(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_FROM_EXTERNAL_DB,
                {CONF_DB_TYPE: DB_TYPE_SQLITE},
                blocking=True,
            )

    async def test_import_from_recorder_rejects_concurrent_call(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        """A call while an import is already running must be rejected.

        The real first-call-then-second-call race is exercised at the unit
        level in __init__.py (the flag is set synchronously, with no `await`
        between the check and the set). Racing two real service calls here
        would be nondeterministic: the first import's background task can
        finish (and reset the flag) before the second call runs, depending on
        event-loop scheduling. Set the flag directly instead to test the
        guard itself deterministically.
        """
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        coordinator.async_set_importing(True)
        try:
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN, SERVICE_IMPORT_FROM_RECORDER, {}, blocking=True
                )
        finally:
            coordinator.async_set_importing(False)

    async def test_import_from_external_db_rejects_concurrent_call(
        self, hass: HomeAssistant, patched_factory, tmp_path
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        coordinator.async_set_importing(True)
        try:
            data = {
                CONF_DB_TYPE: DB_TYPE_SQLITE,
                CONF_DB_PATH: str(tmp_path / "nonexistent.db"),
            }
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN, SERVICE_IMPORT_FROM_EXTERNAL_DB, data, blocking=True
                )
        finally:
            coordinator.async_set_importing(False)

    async def test_import_from_recorder_allows_new_call_after_completion(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_FROM_RECORDER, {}, blocking=True
        )
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert not coordinator.is_importing

        # Must not raise now that the previous import has finished.
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_FROM_RECORDER, {}, blocking=True
        )
        await hass.async_block_till_done()

    async def test_import_from_recorder_accepts_exclude_entities(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Must not raise a schema validation error for a well-formed list.
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_FROM_RECORDER,
            {"exclude_entities": ["sensor.noisy"]},
            blocking=True,
        )
        await hass.async_block_till_done()

    async def test_import_from_recorder_accepts_exclude_attributes(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Must not raise a schema validation error for a well-formed list.
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_FROM_RECORDER,
            {"exclude_attributes": ["battery_level"]},
            blocking=True,
        )
        await hass.async_block_till_done()

    async def test_import_from_recorder_passes_configured_filters_to_importer(
        self, hass: HomeAssistant, patched_factory
    ) -> None:
        entry = _make_entry(
            hass,
            **{
                CONF_EXCLUDE_DOMAINS: ["binary_sensor"],
                CONF_EXCLUDE_ENTITIES: ["sensor.noisy"],
                CONF_EXCLUDE_ATTRIBUTES: ["battery_level"],
            },
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        mock_importer = MagicMock()
        mock_importer.run = AsyncMock(return_value=0)
        with patch(
            "custom_components.ha_recorder_ext.importer.RecorderImporter",
            return_value=mock_importer,
        ) as mock_importer_cls:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_FROM_RECORDER,
                {"exclude_attributes": ["signal_strength"]},
                blocking=True,
            )
            await hass.async_block_till_done()

        mock_importer_cls.assert_called_once_with(
            hass,
            coordinator.backend,
            coordinator.exclude_domains,
            coordinator.exclude_entities,
            coordinator.exclude_attributes,
        )
        mock_importer.run.assert_awaited_once()
        args = mock_importer.run.await_args.args
        assert args[3] is None  # exclude_entities not passed in this call
        assert args[4] == ["signal_strength"]  # exclude_attributes forwarded

    async def test_import_from_external_db_passes_configured_filters_to_importer(
        self, hass: HomeAssistant, patched_factory, tmp_path
    ) -> None:
        entry = _make_entry(
            hass,
            **{
                CONF_EXCLUDE_DOMAINS: ["binary_sensor"],
                CONF_EXCLUDE_ENTITIES: ["sensor.noisy"],
                CONF_EXCLUDE_ATTRIBUTES: ["battery_level"],
            },
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        mock_reader = AsyncMock()
        mock_importer = MagicMock()
        mock_importer.run = AsyncMock(return_value=0)
        with (
            patch(
                "custom_components.ha_recorder_ext.external_recorder_reader"
                ".create_external_recorder_reader",
                return_value=mock_reader,
            ),
            patch(
                "custom_components.ha_recorder_ext.importer.ExternalRecorderImporter",
                return_value=mock_importer,
            ) as mock_importer_cls,
        ):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_FROM_EXTERNAL_DB,
                {
                    CONF_DB_TYPE: DB_TYPE_SQLITE,
                    CONF_DB_PATH: str(tmp_path / "external.db"),
                    "exclude_attributes": ["signal_strength"],
                },
                blocking=True,
            )
            await hass.async_block_till_done()

        mock_importer_cls.assert_called_once_with(
            hass,
            coordinator.backend,
            mock_reader,
            coordinator.exclude_domains,
            coordinator.exclude_entities,
            coordinator.exclude_attributes,
        )
        mock_importer.run.assert_awaited_once()
        args = mock_importer.run.await_args.args
        assert args[4] == ["signal_strength"]  # exclude_attributes forwarded


class TestFlushRollback:
    async def test_flush_rollback_on_commit_failure(
        self, hass: HomeAssistant
    ) -> None:
        """When commit raises, rollback is called and the coordinator stays running."""
        backend = _mock_backend()
        backend.commit.side_effect = OSError("disk full")

        entry = _make_entry(hass)

        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=backend,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            coordinator = entry.runtime_data
            from custom_components.ha_recorder_ext import _StateSnapshot
            coordinator._queue.put_nowait(
                _StateSnapshot("sensor.t", "1", {}, datetime.now(timezone.utc))
            )
            await coordinator._flush()

        backend.rollback.assert_called_once()
        assert coordinator.is_running

    async def test_flush_begin_failure_requeues_snapshots(
        self, hass: HomeAssistant
    ) -> None:
        """When begin() raises (e.g. a timed-out dead connection), the queued
        snapshots must be put back for the next cycle rather than lost, and
        the failure must not propagate out of _flush()."""
        backend = _mock_backend()
        backend.begin.side_effect = TimeoutError("connection dead")

        entry = _make_entry(hass)

        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=backend,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            coordinator = entry.runtime_data
            from custom_components.ha_recorder_ext import _StateSnapshot
            coordinator._queue.put_nowait(
                _StateSnapshot("sensor.t", "1", {}, datetime.now(timezone.utc))
            )
            await coordinator._flush()  # must not raise

        backend.begin.assert_called_once()
        backend.commit.assert_not_called()
        assert coordinator.queue_size == 1

    async def test_flush_rollback_failure_does_not_raise(
        self, hass: HomeAssistant
    ) -> None:
        """If rollback() also fails after a failed commit(), _flush() must
        still not propagate — a broken connection on the way out must not
        crash the flush loop on top of the original commit failure."""
        backend = _mock_backend()
        backend.commit.side_effect = OSError("disk full")
        backend.rollback.side_effect = TimeoutError("connection dead")

        entry = _make_entry(hass)

        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=backend,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            coordinator = entry.runtime_data
            from custom_components.ha_recorder_ext import _StateSnapshot
            coordinator._queue.put_nowait(
                _StateSnapshot("sensor.t", "1", {}, datetime.now(timezone.utc))
            )
            await coordinator._flush()  # must not raise

        backend.rollback.assert_called_once()

    async def test_flush_loop_survives_exception_in_flush(
        self, hass: HomeAssistant
    ) -> None:
        """An unexpected exception out of _flush() must not kill the
        background flush loop — otherwise live recording would stop
        permanently and silently until the next HA restart."""
        backend = _mock_backend()
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_DB_TYPE: DB_TYPE_SQLITE,
                CONF_DB_PATH: "test.db",
                CONF_EXCLUDE_DOMAINS: [],
                CONF_EXCLUDE_ENTITIES: [],
                CONF_EXCLUDE_ATTRIBUTES: [],
            },
            options={CONF_FLUSH_INTERVAL: 0},
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=backend,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            coordinator = entry.runtime_data
            coordinator._flush = AsyncMock(side_effect=RuntimeError("boom"))

            for _ in range(5):
                await asyncio.sleep(0)

            assert coordinator._flush_task is not None
            assert not coordinator._flush_task.done()
            assert coordinator._flush.call_count >= 2

            # Restore a working _flush so unload's final flush doesn't raise.
            coordinator._flush = AsyncMock()
            assert await hass.config_entries.async_unload(entry.entry_id)


class TestFlushHealth:
    """Write-health tracking: counters, the Repairs issue and recovery."""

    @staticmethod
    async def _setup(hass: HomeAssistant, backend: MagicMock):
        entry = _make_entry(hass)
        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=backend,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return entry.runtime_data

    @staticmethod
    async def _flush_with(coordinator, count: int = 1) -> None:
        from custom_components.ha_recorder_ext import _StateSnapshot

        for i in range(count):
            coordinator._queue.put_nowait(
                _StateSnapshot(f"sensor.t{i}", "1", {}, datetime.now(timezone.utc))
            )
        await coordinator._flush()

    @staticmethod
    def _issue(hass: HomeAssistant):
        return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RECORDING_FAILING)

    async def test_all_snapshots_failing_counts_as_failed_flush(
        self, hass: HomeAssistant
    ) -> None:
        backend = _mock_backend()
        backend.get_or_create_entity.side_effect = RuntimeError("boom")
        coordinator = await self._setup(hass, backend)

        await self._flush_with(coordinator, count=2)

        assert coordinator.consecutive_failed_flushes == 1
        assert coordinator.failed_snapshots_total == 2
        assert coordinator.last_error_type == "RuntimeError"
        assert coordinator.last_error_at is not None
        assert coordinator.last_successful_write is None
        assert not coordinator.has_write_problem
        assert self._issue(hass) is None

    async def test_problem_and_issue_raised_at_threshold(
        self, hass: HomeAssistant
    ) -> None:
        backend = _mock_backend()
        backend.get_or_create_entity.side_effect = RuntimeError("boom")
        coordinator = await self._setup(hass, backend)

        for _ in range(FAILED_FLUSH_THRESHOLD):
            await self._flush_with(coordinator)

        assert coordinator.has_write_problem
        issue = self._issue(hass)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert issue.translation_placeholders == {"error_type": "RuntimeError"}

    async def test_recovery_clears_problem_and_issue(
        self, hass: HomeAssistant
    ) -> None:
        backend = _mock_backend()
        backend.get_or_create_entity.side_effect = RuntimeError("boom")
        coordinator = await self._setup(hass, backend)
        for _ in range(FAILED_FLUSH_THRESHOLD):
            await self._flush_with(coordinator)
        assert coordinator.has_write_problem

        backend.get_or_create_entity.side_effect = None
        backend.get_or_create_entity.return_value = __import__("uuid").uuid4()
        await self._flush_with(coordinator)

        assert coordinator.consecutive_failed_flushes == 0
        assert not coordinator.has_write_problem
        assert coordinator.last_successful_write is not None
        assert self._issue(hass) is None
        # The last error stays visible after recovery, for post-mortems.
        assert coordinator.last_error_type == "RuntimeError"

    async def test_partial_failure_is_not_a_failed_flush(
        self, hass: HomeAssistant
    ) -> None:
        """One bad entity must not keep the problem sensor on forever."""
        backend = _mock_backend()
        good_pk = __import__("uuid").uuid4()
        backend.get_or_create_entity.side_effect = [good_pk, RuntimeError("bad")]
        coordinator = await self._setup(hass, backend)

        await self._flush_with(coordinator, count=2)

        assert coordinator.consecutive_failed_flushes == 0
        assert coordinator.failed_snapshots_total == 1
        assert coordinator.last_successful_write is not None

    async def test_begin_failure_counts_but_does_not_lose_snapshots(
        self, hass: HomeAssistant
    ) -> None:
        backend = _mock_backend()
        backend.begin.side_effect = OSError("db down")
        coordinator = await self._setup(hass, backend)

        await self._flush_with(coordinator, count=2)

        assert coordinator.consecutive_failed_flushes == 1
        assert coordinator.last_error_type == "OSError"
        # Re-queued for the next cycle, so not (yet) lost.
        assert coordinator.failed_snapshots_total == 0
        assert coordinator.queue_size == 2

    async def test_commit_failure_loses_the_whole_batch(
        self, hass: HomeAssistant
    ) -> None:
        backend = _mock_backend()
        backend.commit.side_effect = OSError("disk full")
        coordinator = await self._setup(hass, backend)

        await self._flush_with(coordinator, count=3)

        assert coordinator.consecutive_failed_flushes == 1
        assert coordinator.failed_snapshots_total == 3
        assert coordinator.last_error_type == "OSError"
        assert coordinator.last_successful_write is None

    async def test_dropped_snapshots_are_counted(self, hass: HomeAssistant) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_DB_TYPE: DB_TYPE_SQLITE,
                CONF_DB_PATH: "test.db",
                CONF_EXCLUDE_DOMAINS: [],
                CONF_EXCLUDE_ENTITIES: [],
                CONF_EXCLUDE_ATTRIBUTES: [],
            },
            options={CONF_FLUSH_INTERVAL: 300, CONF_QUEUE_MAX_SIZE: 1},
        )
        entry.add_to_hass(hass)
        with patch(
            "custom_components.ha_recorder_ext.create_backend",
            return_value=_mock_backend(),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data
            from custom_components.ha_recorder_ext import _StateSnapshot

            while not coordinator._queue.full():
                coordinator._queue.put_nowait(
                    _StateSnapshot("sensor.x", "1", {}, datetime.now(timezone.utc))
                )
            before = coordinator.dropped_snapshots_total

            hass.states.async_set("sensor.extra", "42")
            await hass.async_block_till_done()

        assert coordinator.dropped_snapshots_total == before + 1

    async def test_listeners_notified_after_flush(self, hass: HomeAssistant) -> None:
        coordinator = await self._setup(hass, _mock_backend())
        listener = MagicMock()
        coordinator.async_add_listener(listener)

        await self._flush_with(coordinator)

        listener.assert_called()

    async def test_stop_clears_open_issue(self, hass: HomeAssistant) -> None:
        backend = _mock_backend()
        backend.get_or_create_entity.side_effect = RuntimeError("boom")
        coordinator = await self._setup(hass, backend)
        for _ in range(FAILED_FLUSH_THRESHOLD):
            await self._flush_with(coordinator)
        assert self._issue(hass) is not None

        await coordinator.stop()

        assert self._issue(hass) is None
