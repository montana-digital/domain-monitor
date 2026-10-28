import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path
    password: str | None
    secret_key: str
    host: str = "127.0.0.1"
    port: int = 8000
    start_scheduler: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        password = os.environ.get("DOMAIN_MONITOR_PASSWORD") or None
        secret = os.environ.get("DOMAIN_MONITOR_SECRET") or load_or_create_secret(data_dir)
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8000"))
        return cls(
            data_dir=data_dir,
            password=password,
            secret_key=secret,
            host=host,
            port=port,
        )


_current: Settings | None = None


def set_current(settings: Settings) -> None:
    global _current
    _current = settings


def current() -> Settings:
    if _current is None:
        raise RuntimeError("Settings are not initialized.")
    return _current


def load_or_create_secret(data_dir: Path) -> str:
    path = data_dir / "secret.key"
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key
