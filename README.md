# ha-recorder-ext

[![Tests](https://github.com/Matzaptor/ha-recorder-ext/actions/workflows/tests.yml/badge.svg)](https://github.com/Matzaptor/ha-recorder-ext/actions/workflows/tests.yml)

A Home Assistant custom integration that logs entity state and attribute observations into a structured database optimized for Machine Learning datasets, feature engineering, and historical analysis.

## Why this exists

Home Assistant's built-in recorder stores raw events. This integration stores **validity-range observations** instead: each record represents one value for one field (state or attribute) of one entity over a continuous time interval. Repeated observations of the same value extend the interval rather than creating duplicate rows. This produces a compact, analytics-friendly dataset.

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Matzaptor&repository=ha-recorder-ext&category=integration)

1. Click the button above to add this repository to HACS.
2. Install **HA External Recorder** from HACS.
3. Restart Home Assistant.
4. Click the button below to open the setup wizard.

[![Add to My Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=ha_recorder_ext)

### Manual

1. Copy `custom_components/ha_recorder_ext/` into your Home Assistant `custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **HA External Recorder**.

## Configuration

### Supported database backends

| Backend | Type | Driver | Notes |
|---|---|---|---|
| **SQLite** | Embedded file | `aiosqlite` | Default, no server needed |
| **MySQL / MariaDB** | Server | `aiomysql` | Requires a running MySQL 8+ or MariaDB server |
| **PostgreSQL** | Server | `asyncpg` | Requires a running PostgreSQL 13+ server |

> **DuckDB support is temporarily disabled.** DuckDB's native worker threads crash the
> Python interpreter on shutdown under Python 3.14 (`Fatal Python error: gilstate_tss_set`).
> The backend code remains in the repository and will be re-enabled once upstream DuckDB
> ships a fix for this Python version.

### Setup

**Step 1 — choose backend**

| Field | Description | Default |
|---|---|---|
| Database type | `sqlite`, `mysql`, or `postgresql` | `sqlite` |

**Step 2a — embedded backend (SQLite)**

| Field | Description | Default |
|---|---|---|
| Database file path | Path relative to the HA config directory | `ha_recorder_ext/ha_recorder_ext.db` |
| Exclude domains | List of domains to skip (e.g. `automation`, `sun`) | empty |
| Exclude entities | List of entity IDs to skip (entity picker) | empty |
| Exclude attributes | List of attribute names to never log | empty |

**Step 2b — server backends (MySQL / PostgreSQL)**

| Field | Description | Default |
|---|---|---|
| Host | Server hostname or IP | `localhost` |
| Port | TCP port | `3306` (MySQL) / `5432` (PostgreSQL) |
| Database name | Schema / database to use | `ha_recorder_ext` |
| Username | Database user | — |
| Password | Database password | — |
| Exclude domains | List of domains to skip | empty |
| Exclude entities | List of entity IDs to skip (entity picker) | empty |
| Exclude attributes | List of attribute names to never log | empty |

### Options (adjustable after setup)

| Field | Description | Default | Range |
|---|---|---|---|
| Flush interval | Seconds between buffer flushes to the database | `30` | 5–300 |
| Queue size limit | Max observations buffered in memory before dropping | `10000` | 100–100 000 |
| Failed flushes before alert | Consecutive failed flushes after which *Recording problem* turns on and a Repairs issue is raised (delay = this value × flush interval) | `3` | 1–20 |
| Exclude domains | Override the setup-time domain filter | _(from setup)_ | |
| Exclude entities | Override the setup-time entity filter | _(from setup)_ | |
| Exclude attributes | Override the setup-time attribute filter | _(from setup)_ | |

Options can be changed at any time via **Settings → Devices & Services → HA External Recorder → Configure**. Changing options reloads the integration automatically. Filter values set here override the values entered at setup time.

## Database schema

The integration creates and manages two tables.

### `entities`

One row per tracked entity.

| Column | Type | Description |
|---|---|---|
| `id` | BLOB (UUIDv7) | Primary key |
| `entity_id` | TEXT | e.g. `sensor.living_room_temperature` |
| `domain` | TEXT | e.g. `sensor` |
| `first_seen` | TEXT (ISO 8601 UTC) | When the entity was first observed |
| `last_seen` | TEXT (ISO 8601 UTC) | Updated on every observation |

### `observations`

One row per **validity interval** for a field (state or attribute).

| Column | Type | Description |
|---|---|---|
| `id` | BLOB (UUIDv7) | Primary key |
| `entity_pk` | BLOB | Foreign key → `entities.id` |
| `field` | TEXT | `"state"` or an attribute name |
| `value_type` | TEXT | Type discriminator (see below) |
| `value_str` | TEXT | Active when `value_type = 'str'` |
| `value_int` | INTEGER | Active when `value_type = 'int'` |
| `value_float` | REAL | Active when `value_type IN ('float', 'timedelta')` |
| `value_bool` | INTEGER | Active when `value_type = 'bool'` (0 or 1) |
| `value_datetime` | TEXT | Active when `value_type = 'datetime'` (ISO 8601 UTC) |
| `value_date` | TEXT | Active when `value_type = 'date'` (YYYY-MM-DD) |
| `value_time` | TEXT | Active when `value_type = 'time'` (HH:MM:SS[.ffffff]) |
| `value_json` | TEXT | Active when `value_type = 'json'` (lists, dicts, etc.) |
| `first_seen` | TEXT | When this value was first observed |
| `last_seen` | TEXT | Updated as long as the value stays the same |

#### Supported value types

| `value_type` | Python type | Notes |
|---|---|---|
| `str` | `str` | |
| `int` | `int` | |
| `float` | `float` | NaN and Inf are stored as `null` |
| `bool` | `bool` | |
| `null` | `None` | Also used for unavailable/unknown |
| `datetime` | `datetime.datetime` | Always normalized to UTC |
| `date` | `datetime.date` | |
| `time` | `datetime.time` | |
| `timedelta` | `datetime.timedelta` | Stored as total seconds in `value_float` |
| `json` | `list`, `dict`, other | JSON-encoded string |

Primary keys use **UUIDv7** (time-ordered), which provides natural chronological ordering and efficient B-tree indexing without integer overflow risk.

## Deduplication semantics

The integration tracks the **latest stored value** for each `(entity, field)` pair.

- If the new observation equals the latest stored value → `last_seen` is updated, no new row.
- If the value changed → a new row is inserted with a fresh `first_seen`.

Example for `sensor.temperature`:

```
time  value   result
T0    20.0  → INSERT  (first_seen=T0, last_seen=T0)
T1    20.0  → UPDATE  (last_seen=T1)
T2    21.0  → INSERT  (first_seen=T2, last_seen=T2)
T3    20.0  → INSERT  (first_seen=T3, last_seen=T3)  ← new interval, not the T0 row
```

The third `20.0` opens a new interval because it follows a different value. This preserves the full history of value transitions without duplicating unchanged periods.

## Performance

State change observations are buffered in an in-memory queue and flushed to the database in a **single transaction** per flush cycle. No I/O happens inside HA event callbacks.

If the queue fills up before a flush, new observations are dropped and a warning is logged. Increase **Queue size limit** if this happens frequently.

## Entities

The integration creates three binary sensors alongside the config entry.

| Entity | On when | Attributes |
|---|---|---|
| **Recording** | The background flush loop is running | `queue_size`, `last_flush`, `flush_interval_seconds` |
| **Import in progress** | An `import_from_recorder` or `import_from_external_db` service call is currently running | `last_import_intervals_inserted` (once at least one import has completed) |
| **Recording problem** | Writing to the database has failed for the configured number of consecutive flushes, 3 by default (diagnostic entity, `problem` device class) | `consecutive_failed_flushes`, `failed_snapshots_total`, `dropped_snapshots_total`, `last_error_type`, `last_error_at`, `last_successful_write`, `queue_size` |

**Recording** only tells you the flush loop is alive, not that data is being stored. Use **Recording problem** to be alerted when the database is unreachable or rejecting writes.

### Write health

A flush counts as *failed* when nothing was stored: the transaction could not be started or committed, or every observation in the batch failed. A few individually rejected observations (for example one entity whose value the database refuses) do not count as a failed flush; they only increase `failed_snapshots_total`.

- **Recording problem** turns on after **Failed flushes before alert** consecutive failed flushes (option, default 3, so about 90 seconds at the default 30 s flush interval) and turns off again on the first successful flush.
- At the same time a **Repairs** issue (*Settings → System → Repairs*) is raised, and removed automatically once writes succeed again.
- `last_error_type` holds only the exception class name (for example `OperationalError`), never the message, because database error messages can contain host names or queries. The full error is in the Home Assistant log.
- Observations of a failed flush are lost when the transaction cannot be committed; if the transaction cannot even be started they stay in the queue for the next cycle, and are dropped once the queue is full (counted in `dropped_snapshots_total`). Use `import_from_recorder` to fill gaps afterwards.

Example automation:

```yaml
automation:
  - alias: "Notify when the external recorder cannot write"
    triggers:
      - trigger: state
        entity_id: binary_sensor.ha_external_recorder_recording_problem
        to: "on"
    actions:
      - action: notify.notify
        data:
          title: "HA External Recorder"
          message: >
            Writes are failing
            ({{ state_attr('binary_sensor.ha_external_recorder_recording_problem', 'last_error_type') }}).
```

## Diagnostics

The integration exposes diagnostics data under **Settings → Devices & Services → HA External Recorder → Download diagnostics**.

| Section | Key | Description |
|---|---|---|
| `config` | `db_type` | Database backend in use |
| `config` | `db_path` | Resolved path to the database file |
| `config` | `exclude_domains` | Domains excluded from logging |
| `config` | `exclude_entities` | Entities excluded from logging |
| `config` | `exclude_attributes` | Attributes excluded from logging |
| `options` | `flush_interval` | Flush interval in seconds |
| `options` | `queue_max_size` | Maximum queue depth |
| `options` | `failed_flush_threshold` | Failed flushes before the problem sensor turns on |
| `coordinator` | `is_running` | Whether the background loop is active |
| `coordinator` | `queue_size` | Current number of buffered observations |
| `coordinator` | `flush_interval` | Effective flush interval |
| `coordinator` | `last_flush` | UTC ISO 8601 timestamp of last successful flush, or `null` |
| `coordinator` | `last_successful_write` | UTC ISO 8601 timestamp of the last flush that stored at least one observation, or `null` |
| `coordinator` | `has_write_problem` | Whether the *Recording problem* sensor is on |
| `coordinator` | `consecutive_failed_flushes` | Failed flushes in a row (resets on success) |
| `coordinator` | `failed_snapshots_total` | Observations that could not be stored since startup |
| `coordinator` | `dropped_snapshots_total` | Observations discarded since startup because the queue was full |
| `coordinator` | `last_error_type` | Exception class of the last write error, or `null` |
| `coordinator` | `last_error_at` | UTC ISO 8601 timestamp of the last write error, or `null` |

## Development setup

```bash
# Clone the repository
git clone <repo-url>
cd ha-recorder-ext

# Create a virtual environment (Python 3.13+)
python3.13 -m venv .venv
source .venv/bin/activate

# Install test dependencies
pip install -r requirements-test.txt

# Run tests
pytest tests/ -v
```

Tests use `pytest-homeassistant-custom-component` and do not require a running Home Assistant instance or a real database server. MySQL and PostgreSQL backend tests use mocked drivers.

## Importing historical data from the HA Recorder

If you have months or years of history in the built-in HA Recorder, you can backfill ha_recorder_ext using the **Import from Recorder** service.

### How it works

1. Go to **Developer tools → Services** and call `ha_recorder_ext.import_from_recorder`.
2. The service queries the Recorder for the requested time range, compresses consecutive equal values into validity-range intervals (RLE), and inserts only time ranges that do not already exist in your ha_recorder_ext database.
3. Progress is logged at INFO level once per processed day, with cumulative inserted/skipped counts (`Logger: ha_recorder_ext.importer`). For full per-entity detail, enable debug logging for this integration under **Settings → Devices & Services → HA External Recorder → ⋮ → Enable debug logging**.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `start_date` | ISO 8601 string | none | Earliest date to import, defaults to the oldest state available in the recorder |
| `end_date` | ISO 8601 string | today | Latest date to import |
| `entity_ids` | list of entity IDs | all entities | Restrict import to specific entities |
| `exclude_entities` | list of entity IDs | none | Skip these entities, applied after `entity_ids` resolves to a list |
| `exclude_attributes` | list of attribute names | none | Skip these attributes for every imported entity, merged with the attributes excluded in the integration's own configuration |

### Idempotency and resumability

The import is **safe to run multiple times**. Before inserting each interval, it checks whether an overlapping observation already exists in ha_recorder_ext. If the import is interrupted (e.g. HA restart), re-running it skips already-imported time ranges and continues from the first gap.

## Importing historical data from an external database (recorder backup)

If you have a backup of a **Home Assistant Recorder database** sitting on a separate server (for example an old instance's MySQL export), you can import it directly with the **Import from External Database** service, without connecting it to a live HA instance first.

### How it works

1. Go to **Developer tools → Services** and call `ha_recorder_ext.import_from_external_db`, providing `db_type` (`sqlite`, `mysql`, or `postgresql`) and the matching connection parameters.
2. The service connects read-only to the external database, queries it for the requested time range, compresses consecutive equal values into validity-range intervals (RLE), and inserts only time ranges that do not already exist in your ha_recorder_ext database.
3. Progress is logged at INFO level once per processed day, with cumulative inserted/skipped counts (`Logger: ha_recorder_ext.importer`). For full per-entity detail, enable debug logging for this integration under **Settings → Devices & Services → HA External Recorder → ⋮ → Enable debug logging**.

This service reads a backup of **another** Home Assistant instance's Recorder database — not this integration's own storage. Only the modern, normalized Recorder schema (HA 2023.4+, with `states_meta` and `state_attributes` tables) is supported; older backups fail explicitly with a clear error instead of importing partial or incorrect data.

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `db_type` | `sqlite` \| `mysql` \| `postgresql` | yes | Type of the external database |
| `db_path` | string | only for `sqlite` | Path to the recorder backup file |
| `db_host` | string | only for `mysql`/`postgresql` | Database server hostname |
| `db_port` | number | only for `mysql`/`postgresql` | Database server port |
| `db_name` | string | only for `mysql`/`postgresql` | Database name |
| `db_username` | string | only for `mysql`/`postgresql` | Database username (a read-only user is recommended) |
| `db_password` | string | only for `mysql`/`postgresql` | Database password |
| `start_date` | ISO 8601 string | no | Earliest date to import, defaults to the oldest state in the source |
| `end_date` | ISO 8601 string | no | Latest date to import, defaults to today |
| `entity_ids` | list of entity IDs | no | Restrict import to specific entities |
| `exclude_entities` | list of entity IDs | no | Skip these entities, applied after `entity_ids` resolves to a list |
| `exclude_attributes` | list of attribute names | no | Skip these attributes for every imported entity, merged with the attributes excluded in the integration's own configuration |

Connection parameters are used only for the duration of the call and are not persisted anywhere by ha_recorder_ext. They will, however, appear in Home Assistant's own service-call history and in any automation that calls this service — treat this the same as any other HA service with a password field.

### Concurrency and network resilience

- Only one import (`import_from_recorder` or `import_from_external_db`) can run at a time. Calling either service while one is already in progress (see the *Import in progress* sensor) is rejected with a clear error instead of racing the running import.
- Within a single day-sized chunk, an additional progress line is logged at INFO level every 25 entities processed, so a day with unusually heavy volume shows visible forward progress instead of appearing stalled between the once-per-day summary lines.
- Log lines are prefixed with the name of the service actually running (`import_from_recorder` or `import_from_external_db`), so it's always clear which import produced a given line.
- `import_from_external_db` connections to MySQL/PostgreSQL time out after 10 seconds to connect and 300 seconds per query, so a dead server or a network blip surfaces as a logged error instead of hanging the import task forever.

## Known limitations

- DuckDB backend is temporarily disabled — it crashes the Python interpreter on shutdown
  under Python 3.14. Selecting `duckdb` raises a clear configuration error.
- `import_from_external_db` only supports HA Recorder backups using the 2023.4+ schema
  generation (`states_meta`/`state_attributes` normalized tables). Older recorder schema
  generations are not supported and raise a clear error instead of importing wrong or
  partial data.
- No data retention policy yet — the database grows indefinitely.
- No export tooling yet — query the database file directly (SQLite) or use any SQL client (MySQL/PostgreSQL).

## Removal

1. Go to **Settings → Devices & Services → HA External Recorder**.
2. Click the three-dot menu → **Delete**.
3. Restart Home Assistant.
4. Optionally delete the database directory from your HA config directory (default: `ha_recorder_ext/`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming conventions, commit message format, development setup, and the pull request checklist.

## Security

The integration runs entirely inside the Home Assistant process and stores data locally. No data is sent outside your local network. Do not commit database files, `.env` files, or HA tokens to version control.
