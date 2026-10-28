#!/usr/bin/env python3
"""Install Domain Monitor on Debian or Ubuntu, then start it.

The first run creates ``.venv``, installs this checkout, and downloads
Chromium into ``.playwright``. Later runs start the process. ``--service``
installs a systemd unit so the process comes back after logout.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
BROWSERS = ROOT / ".playwright"
PYPROJECT = ROOT / "pyproject.toml"
UNIT_PATH = Path("/etc/systemd/system/domain-monitor.service")
MIN_PYTHON = (3, 12)
DEPS_STAMP_NAME = ".system-deps"
CHROMIUM_STAMP_NAME = ".chromium-ok"
APT_LOCK_ATTEMPTS = 6
APT_LOCK_WAIT_SECONDS = 10
DOWNLOAD_ATTEMPTS = 5
DOWNLOAD_WAITS = (5, 10, 20, 40)
DOWNLOAD_IDLE_TIMEOUT = "600000"
RUNTIME_IMPORTS = ("fastapi", "uvicorn", "playwright")
CHROMIUM_EXECUTABLES = (
    Path("chrome-linux64") / "chrome",
    Path("chrome-linux") / "chrome",
)
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def python_version_ok(version: tuple[int, ...]) -> bool:
    return version >= MIN_PYTHON


def version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version[:3])


def read_project_version(text: str) -> str:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"pyproject.toml could not be parsed: {exc}") from exc
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise SystemExit("pyproject.toml [project] table has no version")
    return version.strip()


def venv_satisfies(installed_version: str | None, imports_ok: bool, project_version: str) -> bool:
    return imports_ok and installed_version == project_version


def needs_pip_install(
    installed_version: str | None,
    imports_ok: bool,
    project_version: str,
    repair: bool,
) -> bool:
    if repair:
        return True
    return not venv_satisfies(installed_version, imports_ok, project_version)


def chromium_present(browsers: Path) -> bool:
    if not browsers.is_dir():
        return False
    for revision in browsers.iterdir():
        if not revision.is_dir() or not revision.name.startswith("chromium-"):
            continue
        for relative in CHROMIUM_EXECUTABLES:
            if (revision / relative).is_file():
                return True
    return False


def chromium_ready(browsers: Path) -> bool:
    return chromium_present(browsers) and (browsers / DEPS_STAMP_NAME).is_file()


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def open_host_warning(host: str, password: str | None) -> str | None:
    if is_loopback(host) or password:
        return None
    return (
        f"HOST is {host} and DOMAIN_MONITOR_PASSWORD is unset. "
        "Anyone who can reach that address can use the UI."
    )


def listen_url(host: str, port: str) -> str:
    shown = host.strip() or "127.0.0.1"
    if shown in {"0.0.0.0", "::"}:
        shown = "127.0.0.1"
    if ":" in shown and not shown.startswith("["):
        shown = f"[{shown}]"
    return f"http://{shown}:{port}"


def unsupported_platform_message(platform: str) -> str | None:
    if platform == "linux":
        return None
    commands = (
        "python -m venv .venv\n"
        ".venv\\Scripts\\activate\n"
        'pip install -e ".[dev]"\n'
        "pytest\n"
        "uvicorn app.main:app --host 127.0.0.1 --port 8000\n"
    )
    if platform == "win32":
        return (
            "This launcher installs Domain Monitor on Debian or Ubuntu.\n"
            "On Windows, use the local development commands:\n\n"
            + commands
        )
    return (
        "This launcher installs Domain Monitor on Debian or Ubuntu.\n"
        "On this machine, use the local development commands from the README.\n"
    )


def environment_line(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{name}={escaped}"'


def systemd_token(value: str) -> str:
    if any(char in value for char in " \t\"'#"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_unit(
    *,
    user: str,
    group: str,
    root: Path,
    python: Path,
    browsers: Path,
    data_dir: Path,
    host: str,
    port: str,
    password: str | None,
    secret: str | None,
) -> str:
    lines = [
        "[Unit]",
        "Description=Domain Monitor",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={systemd_token(user)}",
        f"Group={systemd_token(group)}",
        f"WorkingDirectory={systemd_token(root.as_posix())}",
        environment_line("PLAYWRIGHT_BROWSERS_PATH", browsers.as_posix()),
        environment_line("DATA_DIR", data_dir.as_posix()),
        environment_line("HOST", host),
        environment_line("PORT", port),
    ]
    if password:
        lines.append(environment_line("DOMAIN_MONITOR_PASSWORD", password))
    if secret:
        lines.append(environment_line("DOMAIN_MONITOR_SECRET", secret))
    lines.extend(
        [
            f"ExecStart={systemd_token(python.as_posix())} -m app.main",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    return "\n".join(lines)


def say(message: str) -> None:
    print(message, flush=True)


def have_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def apply_download_timeout(env: dict[str, str]) -> None:
    if not env.get("PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"):
        env["PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"] = DOWNLOAD_IDLE_TIMEOUT


def browser_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    apply_download_timeout(env)
    return env


def browser_download_interrupted(text: str) -> bool:
    lowered = text.lower()
    markers = ("econnreset", "etimedout", "econnrefused", "eai_again", "socket hang up")
    return any(marker in lowered for marker in markers)


def download_wait_seconds(attempt: int) -> int:
    index = min(max(attempt, 1), len(DOWNLOAD_WAITS)) - 1
    return DOWNLOAD_WAITS[index]


def chromium_download_needed(browsers: Path, repair: bool) -> bool:
    if repair or not chromium_present(browsers):
        return True
    return not (browsers / CHROMIUM_STAMP_NAME).is_file()


def venv_apt_package(version: tuple[int, ...]) -> str:
    return f"python{version[0]}.{version[1]}-venv"


def root_argv(
    sudo: str | None,
    command: list[str],
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Argv for a root command. Extra variables are set inside the root shell."""
    if extra_env:
        exports = " ".join(
            f"export {key}={shlex.quote(value)};" for key, value in extra_env.items()
        )
        script = exports + " " + " ".join(shlex.quote(part) for part in command)
        wrapped = ["bash", "-c", script]
    else:
        wrapped = list(command)
    if sudo:
        return [sudo, *wrapped]
    return wrapped


def apt_lock_busy(text: str) -> bool:
    return "lock-frontend" in text


def run_showing(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    process = subprocess.Popen(
        argv,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    chunks: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        chunks.append(line)
    return process.wait(), "".join(chunks)


def run_as_root(
    command: list[str],
    extra_env: dict[str, str] | None = None,
    retry_lock: bool = False,
) -> None:
    sudo = None if have_root() else shutil.which("sudo")
    if sudo is None and not have_root():
        raise SystemExit("This step needs root. Run:\n  " + " ".join(command))
    argv = root_argv(sudo, command, extra_env)
    attempts = APT_LOCK_ATTEMPTS if retry_lock else 1
    for attempt in range(1, attempts + 1):
        code, output = run_showing(argv)
        if code == 0:
            return
        if retry_lock and apt_lock_busy(output) and attempt < attempts:
            say("Ubuntu Software is using apt. Waiting to try again.")
            time.sleep(APT_LOCK_WAIT_SECONDS)
            continue
        raise subprocess.CalledProcessError(code, argv)


def venv_python() -> Path:
    return VENV / "bin" / "python"


def ensurepip_available() -> bool:
    result = subprocess.run(
        [sys.executable, "-c", "import venv, ensurepip"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def install_apt_packages(packages: list[str]) -> None:
    if shutil.which("apt-get") is None:
        raise SystemExit(
            "apt-get is not available. Install these packages, then run launch.py again:\n  "
            + " ".join(packages)
        )
    say("Installing system packages: " + ", ".join(packages))
    run_as_root(["apt-get", "update"], retry_lock=True)
    run_as_root(["apt-get", "install", "-y", *packages], retry_lock=True)


def interpreter_version(python: Path) -> tuple[int, int] | None:
    result = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def create_venv() -> None:
    if not ensurepip_available():
        package = venv_apt_package(sys.version_info)
        install_apt_packages([package])
        if not ensurepip_available():
            raise SystemExit(f"{package} is still missing after apt install.")
    say("Creating .venv.")
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], cwd=ROOT, check=True)


def ensure_venv() -> None:
    python = venv_python()
    if python.is_file() and python_version_ok(interpreter_version(python) or (0, 0)):
        return
    if VENV.exists():
        say("Replacing .venv.")
        shutil.rmtree(VENV)
    create_venv()


def query_installed_version(python: Path) -> str | None:
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; print(m.version('domain-monitor'), end='')",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def query_imports(python: Path) -> bool:
    names = ", ".join(RUNTIME_IMPORTS)
    result = subprocess.run(
        [str(python), "-c", f"import {names}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def pip_install() -> None:
    say("Installing Domain Monitor into .venv.")
    subprocess.run(
        [str(venv_python()), "-m", "pip", "install", "-e", str(ROOT)],
        cwd=ROOT,
        check=True,
    )


def ensure_packages(repair: bool) -> None:
    version = read_project_version(PYPROJECT.read_text(encoding="utf-8"))
    python = venv_python()
    installed = query_installed_version(python)
    imports_ok = query_imports(python)
    if needs_pip_install(installed, imports_ok, version, repair):
        pip_install()


def mark_chromium_deps(browsers: Path) -> None:
    browsers.mkdir(parents=True, exist_ok=True)
    (browsers / DEPS_STAMP_NAME).write_text("ok\n", encoding="utf-8")


def mark_chromium_download(browsers: Path) -> None:
    browsers.mkdir(parents=True, exist_ok=True)
    (browsers / CHROMIUM_STAMP_NAME).write_text("ok\n", encoding="utf-8")


def install_chromium_browser() -> None:
    argv = [str(venv_python()), "-m", "playwright", "install", "chromium"]
    env = browser_env()
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        code, output = run_showing(argv, env)
        if code == 0:
            return
        if browser_download_interrupted(output) and attempt < DOWNLOAD_ATTEMPTS:
            wait = download_wait_seconds(attempt)
            say(f"The browser download was reset. Trying again in {wait} seconds.")
            time.sleep(wait)
            continue
        raise subprocess.CalledProcessError(code, argv)


def ensure_chromium(repair: bool) -> None:
    if chromium_download_needed(BROWSERS, repair):
        say("Chromium is a large download and can take a while.")
        say("Installing Chromium into .playwright.")
        BROWSERS.mkdir(parents=True, exist_ok=True)
        install_chromium_browser()
        mark_chromium_download(BROWSERS)
    if repair or not (BROWSERS / DEPS_STAMP_NAME).is_file():
        say("Installing Chromium system libraries.")
        run_as_root(
            [str(venv_python()), "-m", "playwright", "install-deps", "chromium"],
            extra_env={"DEBIAN_FRONTEND": "noninteractive"},
            retry_lock=True,
        )
        mark_chromium_deps(BROWSERS)


def checkout_ready() -> None:
    if not PYPROJECT.is_file() or not (ROOT / "app" / "main.py").is_file():
        raise SystemExit("launch.py must stay in the Domain Monitor repository.")


def service_account() -> tuple[str, str]:
    import grp
    import pwd

    name = os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        record = pwd.getpwnam(name)
    except KeyError:
        raise SystemExit(f"Cannot find a user account named {name}.") from None
    return name, grp.getgrgid(record.pw_gid).gr_name


def write_text_as_root(path: Path, content: str, mode: int) -> None:
    if have_root():
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        return
    if shutil.which("sudo") is None:
        raise SystemExit(f"Cannot write {path}. Re-run as root.")
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    try:
        handle.write(content)
        handle.close()
        run_as_root(["install", "-m", f"{mode:o}", handle.name, str(path)])
    finally:
        os.unlink(handle.name)


def systemctl(*args: str) -> None:
    binary = shutil.which("systemctl")
    if binary is None:
        raise SystemExit("systemctl is not available. --service needs systemd.")
    run_as_root([binary, *args])


def warn_if_open(host: str) -> None:
    warning = open_host_warning(host, os.environ.get("DOMAIN_MONITOR_PASSWORD") or None)
    if warning:
        say("Warning: " + warning)


def install_service() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8000")
    password = os.environ.get("DOMAIN_MONITOR_PASSWORD") or None
    secret = os.environ.get("DOMAIN_MONITOR_SECRET") or None
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
    warn_if_open(host)
    user, group = service_account()
    if user == "root":
        say("The service will run as root, so Chromium uses --no-sandbox.")
    content = render_unit(
        user=user,
        group=group,
        root=ROOT,
        python=venv_python(),
        browsers=BROWSERS,
        data_dir=data_dir,
        host=host,
        port=port,
        password=password,
        secret=secret,
    )
    mode = 0o600 if password or secret else 0o644
    say(f"Writing {UNIT_PATH}.")
    write_text_as_root(UNIT_PATH, content, mode)
    say("Enabling domain-monitor.service.")
    systemctl("daemon-reload")
    systemctl("enable", "domain-monitor.service")
    systemctl("restart", "domain-monitor.service")
    say(f"Domain Monitor is running at {listen_url(host, port)}.")


def start_process() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8000")
    warn_if_open(host)
    say(f"Starting Domain Monitor at {listen_url(host, port)}.")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    python = venv_python()
    os.execv(str(python), [str(python), "-m", "app.main"])


def ensure_ready(repair: bool) -> None:
    os.chdir(ROOT)
    checkout_ready()
    if not python_version_ok(sys.version_info):
        raise SystemExit(
            f"Python {version_text(sys.version_info)} is too old. "
            "Domain Monitor needs Python 3.12 or newer."
        )
    ensure_venv()
    ensure_packages(repair)
    ensure_chromium(repair)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Domain Monitor on Debian or Ubuntu and start it."
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Reinstall the Python package and Chromium.",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Install a systemd service and start it.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    message = unsupported_platform_message(sys.platform)
    if message is not None:
        print(message, file=sys.stderr)
        return 2
    try:
        ensure_ready(args.repair)
        if args.service:
            install_service()
            return 0
        start_process()
    except subprocess.CalledProcessError as exc:
        command = " ".join(str(part) for part in exc.cmd) if exc.cmd else "command"
        print(f"Command failed ({exc.returncode}): {command}", file=sys.stderr)
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
