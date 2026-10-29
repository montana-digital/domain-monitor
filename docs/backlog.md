# Later work

No open items.

The schema version step landed with the watch-list columns. Startup reads `PRAGMA table_info`, adds any missing columns, and records `app_settings.schema_version`. An existing `monitor.db` gains those columns on startup. A new database is created in its final shape. See [data-model.md](data-model.md).
