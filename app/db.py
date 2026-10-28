import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, set_current
from app.models import AppSetting, Base

logger = logging.getLogger(__name__)

engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


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
    ensure_settings()


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


def ensure_settings() -> None:
    if SessionLocal is None:
        return
    with SessionLocal() as session:
        if session.get(AppSetting, 1) is None:
            session.add(AppSetting(id=1, retention_days=30))
            commit_with_retry(session)


def commit_with_retry(session: Session) -> None:
    try:
        session.commit()
    except OperationalError:
        logger.warning("SQLite was busy.")
        raise
