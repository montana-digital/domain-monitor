import logging
from collections.abc import Generator
from datetime import timedelta

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.attention import ROUTINE_4H, SCHEMA_VERSION
from app.clock import as_utc, utcnow
from app.config import Settings, set_current
from app.models import AppSetting, Base, Monitor

logger = logging.getLogger(__name__)

engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None

_MONITOR_COLUMNS = (
    ("attention", "VARCHAR(16) NOT NULL DEFAULT 'routine'"),
    ("watch_days", "INTEGER NOT NULL DEFAULT 30"),
    ("watch_until", "DATETIME"),
    ("quiet_since", "DATETIME"),
    ("routine_since", "DATETIME"),
    ("registered", "BOOLEAN"),
    ("registrar", "TEXT"),
    ("host_org", "TEXT"),
    ("last_address", "VARCHAR(64)"),
)
_SNAPSHOT_COLUMNS = (
    ("registered", "BOOLEAN"),
    ("host_org", "TEXT"),
)
_SETTINGS_COLUMNS = (
    ("schema_version", "INTEGER NOT NULL DEFAULT 0"),
    ("sample_seeded", "BOOLEAN NOT NULL DEFAULT 0"),
)


def init_db(settings: Settings) -> None:
    global engine, SessionLocal
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    set_current(settings)
    database = settings.data_dir / "monitor.db"
    engine = create_engine(
        f"sqlite:///{database.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    migrate_schema()
    ensure_settings()
    from app.seed import seed_sample_domains

    seed_sample_domains()


def database_ok() -> bool:
    if engine is None:
        return False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def session_scope() -> Generator[Session, None, None]:
    if SessionLocal is None:
        raise RuntimeError("Database is not initialized.")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _column_names(connection, table: str) -> set[str]:
    rows = connection.execute(text(f"PRAGMA table_info({table})")).all()
    return {row[1] for row in rows}


def _add_columns(connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    present = _column_names(connection, table)
    if not present:
        return
    for name, declaration in columns:
        if name not in present:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"))


def migrate_schema() -> None:
    if engine is None or SessionLocal is None:
        return
    with engine.begin() as connection:
        _add_columns(connection, "monitors", _MONITOR_COLUMNS)
        _add_columns(connection, "registration_snapshots", _SNAPSHOT_COLUMNS)
        _add_columns(connection, "app_settings", _SETTINGS_COLUMNS)
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_monitors_attention_hostname ON monitors (attention, hostname)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_monitors_attention_quiet ON monitors (attention, quiet_since)")
        )
    with SessionLocal() as session:
        setting = session.get(AppSetting, 1)
        version = 0 if setting is None or setting.schema_version is None else setting.schema_version
        if version < SCHEMA_VERSION:
            now = utcnow()
            for monitor in session.scalars(select(Monitor)).all():
                monitor.attention = "routine"
                monitor.interval_minutes = ROUTINE_4H
                monitor.watch_days = 30
                monitor.watch_until = None
                monitor.quiet_since = monitor.last_checked_at or monitor.created_at or now
                monitor.routine_since = now
                checked = as_utc(monitor.last_checked_at)
                if checked is not None:
                    monitor.next_check_at = checked + timedelta(minutes=ROUTINE_4H)
            if setting is not None:
                setting.schema_version = SCHEMA_VERSION
            commit_with_retry(session)


def ensure_settings() -> None:
    if SessionLocal is None:
        return
    with SessionLocal() as session:
        if session.get(AppSetting, 1) is None:
            session.add(AppSetting(id=1, retention_days=30, schema_version=SCHEMA_VERSION, sample_seeded=False))
            commit_with_retry(session)


def commit_with_retry(session: Session) -> None:
    try:
        session.commit()
    except OperationalError:
        logger.warning("SQLite was busy.")
        raise
