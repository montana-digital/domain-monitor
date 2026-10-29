#!/usr/bin/env python3
"""Install Domain Monitor on Linux or Windows, then start it.

A current ``.venv`` whose core packages match this checkout starts the process.
A missing browser does not block that start. Registration, Playwright, the
Chromium download, and system libraries are optional: a failure leaves that
feature off and the core app still starts. Otherwise the launcher asks which
install to create. ``--service`` installs a systemd unit on Linux so the
process comes back after logout.
An interactive terminal shows a wordmark and a progress panel.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

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
CORE_IMPORTS = ("fastapi", "uvicorn")
REGISTRATION_IMPORTS = ("whoisit", "whois")
BROWSER_IMPORTS = ("playwright",)
# Longer names first so ``docker-ce`` is reported before ``docker``.
FORBIDDEN_PACKAGES = (
    "docker-desktop",
    "docker.io",
    "docker-ce",
    "containerd",
    "podman",
    "docker",
    "wsl",
)
CHROMIUM_EXECUTABLES = (
    Path("chrome-linux64") / "chrome",
    Path("chrome-linux") / "chrome",
    Path("chrome-win") / "chrome.exe",
)
DECLINE_MESSAGE = "Install declined. Nothing was changed."
NONINTERACTIVE_MESSAGE = (
    "No .venv yet. Re-run in a terminal and choose an install, or pass --yes or --without-browser."
)
CONTINUE_MESSAGE = "Run launch.py again to continue."
REPLACE_VENV_MESSAGE = (
    "Could not replace .venv. Close Domain Monitor if it is running, then run launch.py again."
)
WINDOWS_VENV_MESSAGE = (
    "This Python cannot create a virtualenv. Install the venv module, then run launch.py again."
)
SERVICE_WINDOWS_MESSAGE = "systemd is only available on Linux."
START_FAILED_MESSAGE = "Could not start Domain Monitor."
CANCEL_MESSAGE = "Install cancelled."
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PANEL_WIDTH = 76
STATE_LABEL = {
    "wait": "waiting",
    "run": "running",
    "ok": "done",
    "skip": "skipped",
    "fail": "failed",
}
STATE_COLOR = {
    "waiting": "\033[2m",
    "running": "\033[33m",
    "done": "\033[32m",
    "skipped": "\033[2m",
    "failed": "\033[31m",
}
CYAN = "\033[36m"
RESET = "\033[0m"
GLYPHS: dict[str, tuple[str, ...]] = {
    "D": ("██████ ", "██   ██", "██   ██", "██   ██", "██████ "),
    "O": (" ████ ", "██  ██", "██  ██", "██  ██", " ████ "),
    "M": ("███   ███", "████ ████", "██ ███ ██", "██  █  ██", "██     ██"),
    "A": (" █████ ", "██   ██", "███████", "██   ██", "██   ██"),
    "I": ("  █  ", "  █  ", "  █  ", "  █  ", "  █  "),
    "N": ("███  ██", "████ ██", "██ ██ █", "██ ████", "██  ███"),
    "T": ("███████", "  ██   ", "  ██   ", "  ██   ", "  ██   "),
    "R": ("██████ ", "██   ██", "██████ ", "██  ██ ", "██   ██"),
}


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


@dataclass(frozen=True)
class InstallProfile:
    """One install size. The extra is the pip extra installed with ``-e``."""

    name: str
    imports: tuple[str, ...]
    chromium: bool
    extra: str


CORE_PROFILE = InstallProfile(
    name="core",
    imports=CORE_IMPORTS,
    chromium=False,
    extra="",
)
WITHOUT_BROWSER_PROFILE = InstallProfile(
    name="without_browser",
    imports=CORE_IMPORTS + REGISTRATION_IMPORTS,
    chromium=False,
    extra="registration",
)
FULL_PROFILE = InstallProfile(
    name="full",
    imports=CORE_IMPORTS + REGISTRATION_IMPORTS + BROWSER_IMPORTS,
    chromium=True,
    extra="full",
)


def next_action(
    *,
    venv_current: bool,
    profile_ready: bool,
    repair: bool,
    assume_yes: bool,
) -> str:
    """Return launch, install, or ask.

    ``profile_ready`` is the core package: the virtualenv exists and its
    version and core imports match. A missing browser does not change this.
    """
    if venv_current and profile_ready and not repair:
        return "launch"
    if venv_current or repair or assume_yes:
        return "install"
    return "ask"


def install_choice(answer: str) -> InstallProfile | None:
    """Map the install prompt. ``1`` is full, ``2`` omits the browser, Enter cancels."""
    text = answer.strip()
    if text == "1":
        return FULL_PROFILE
    if text == "2":
        return WITHOUT_BROWSER_PROFILE
    return None


@dataclass(frozen=True)
class PackageStep:
    """One pip install. Only the core step stops the launcher when it fails."""

    kind: str
    requirement: str
    required: bool
    editable: bool


def dependency_name(spec: str) -> str:
    name = spec.strip()
    for separator in ("[", ">", "=", "<", "!", "~", " "):
        name = name.split(separator, 1)[0]
    return name.strip().lower()


def playwright_requirement(text: str) -> str:
    """The Playwright spec from the ``full`` extra. It is not an editable path."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"pyproject.toml could not be parsed: {exc}") from exc
    project = data.get("project")
    extras = project.get("optional-dependencies") if isinstance(project, dict) else None
    full = extras.get("full") if isinstance(extras, dict) else None
    if isinstance(full, list):
        for dep in full:
            if isinstance(dep, str) and dependency_name(dep) == "playwright":
                return dep.strip()
    raise SystemExit(
        "pyproject.toml [project.optional-dependencies] full has no playwright requirement"
    )


def package_plan(profile: InstallProfile, playwright: str) -> tuple[PackageStep, ...]:
    """Core first, then optional registration, then optional Playwright.

    Playwright is installed from its own requirement, not ``.[full]``, so a
    registration failure does not block it and a Playwright failure does not
    roll back the core install.
    """
    steps = [PackageStep("core", ".", True, True)]
    if set(REGISTRATION_IMPORTS).issubset(profile.imports):
        steps.append(PackageStep("registration", ".[registration]", False, True))
    if set(BROWSER_IMPORTS).issubset(profile.imports):
        if not playwright.strip():
            raise SystemExit(
                "pyproject.toml [project.optional-dependencies] full has no playwright requirement"
            )
        steps.append(PackageStep("playwright", playwright, False, False))
    return tuple(steps)


def package_failure_is_fatal(step: PackageStep) -> bool:
    return step.required


def fetch_browser_after_packages(requested: bool, playwright_ok: bool) -> bool:
    return requested and playwright_ok


def pip_install_argv(python: Path, requirement: str, *, editable: bool) -> list[str]:
    argv = [str(python), "-m", "pip", "install"]
    if editable:
        argv.append("-e")
    argv.append(requirement)
    return argv


def pip_install_detail(step: PackageStep) -> str:
    if step.editable:
        return f"pip install -e {step.requirement}"
    return f"pip install {step.requirement}"


def chromium_install_argv(python: Path) -> list[str]:
    return [str(python), "-m", "playwright", "install", "chromium"]


def library_install_argv(python: Path) -> list[str]:
    return [str(python), "-m", "playwright", "install-deps", "chromium"]


def library_dry_run_argv(python: Path) -> list[str]:
    return [str(python), "-m", "playwright", "install-deps", "chromium", "--dry-run"]


def forbidden_package(text: str) -> str | None:
    """Return a Docker, container, or WSL package name named in ``text``."""
    for token in re.findall(r"[a-z0-9][a-z0-9.+-]*", text.lower()):
        for name in FORBIDDEN_PACKAGES:
            if token == name or token.startswith(name + ".") or token.startswith(name + "-"):
                return name
    return None


def optional_package_detail(label: str, kinds: list[str]) -> str:
    names: list[str] = []
    if "registration" in kinds:
        names.append("registration")
    if "playwright" in kinds:
        names.append("Playwright")
    if not names:
        return label
    if len(names) == 1:
        return f"{label}; {names[0]} unavailable"
    return f"{label}; {' and '.join(names)} unavailable"


def optional_skip_message(kinds: list[str]) -> str:
    parts: list[str] = []
    if "registration" in kinds:
        parts.append("Registration lookups stay off.")
    if "playwright" in kinds:
        parts.append("Screenshots stay off.")
    parts.append("HTTP checks will still start.")
    return " ".join(parts)


def interpreter_path(platform: str) -> Path:
    if platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def system_libraries_needed(platform: str, chromium: bool) -> bool:
    return platform == "linux" and chromium


def platform_label(platform: str) -> str:
    if platform == "win32":
        return "Windows"
    if platform == "linux":
        return "Linux"
    return platform


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
    if platform in {"linux", "win32"}:
        return None
    return (
        "This launcher installs Domain Monitor on Linux or Windows.\n"
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


def presentation_enabled(*, tty: bool, no_color: bool, vt_ready: bool) -> bool:
    """Color and in-place redraw are tied: both on, or both off."""
    return bool(tty) and not no_color and bool(vt_ready)


def no_color_set() -> bool:
    return bool(os.environ.get("NO_COLOR"))


def windows_vt_ready() -> bool:
    """Turn on ANSI processing. False when this console cannot do it."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def detect_presentation() -> bool:
    try:
        tty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
    if not tty or no_color_set():
        return False
    if sys.platform == "win32" and not windows_vt_ready():
        return False
    return True


def configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        return


def compose_word(word: str) -> list[str]:
    glyphs = [GLYPHS[char] for char in word]
    height = len(glyphs[0])
    return [" ".join(glyph[index] for glyph in glyphs) for index in range(height)]


def wordmark_subtitle(version: str) -> str:
    if version:
        return f"local uplink  ·  {version}"
    return "local uplink"


def render_wordmark(version: str) -> str:
    """Block-letter title. Every line is at most PANEL_WIDTH columns, with no color."""
    rows = [
        "DOMAIN",
        *compose_word("DOMAIN"),
        "",
        "MONITOR",
        *compose_word("MONITOR"),
        "",
        wordmark_subtitle(version),
    ]
    inner = PANEL_WIDTH - 2
    framed = ["╔" + "═" * inner + "╗"]
    for row in rows:
        framed.append("║" + (" " + row)[:inner].ljust(inner) + "║")
    framed.append("╚" + "═" * inner + "╝")
    return "\n".join(framed)


def wrap_text(text: str, width: int) -> list[str]:
    if width < 1:
        return [text]
    if text == "":
        return [""]
    lines: list[str] = []
    for raw in text.splitlines():
        if raw == "":
            lines.append("")
            continue
        while len(raw) > width:
            lines.append(raw[:width])
            raw = raw[width:]
        lines.append(raw)
    return lines


def _fit(body: str, inner: int) -> str:
    return "│" + body[:inner].ljust(inner) + "│"


@dataclass
class Step:
    key: str
    label: str
    state: str = "wait"
    detail: str = ""


def make_steps(service: bool) -> list[Step]:
    rows = (
        ("platform", "Platform"),
        ("python", "Python"),
        ("venv", "Virtualenv"),
        ("packages", "Packages"),
        ("chromium", "Chromium"),
        ("libraries", "Libraries"),
        ("listen", "Listen"),
        ("start", "Service" if service else "Start"),
    )
    return [Step(key, label) for key, label in rows]


def render_panel(steps: list[Step], notes: list[str] | None = None) -> str:
    """Boxed phase list. Plain text: state words are waiting, running, done, skipped, failed."""
    inner = PANEL_WIDTH - 2
    title = " Domain Monitor "
    if len(title) > inner:
        title = title[:inner]
    lines = ["┌" + title + "─" * (inner - len(title)) + "┐"]
    for step in steps:
        state = STATE_LABEL.get(step.state, step.state)
        prefix = f"  {state:<8}{step.label:<12}"
        room = max(inner - len(prefix), 1)
        chunks = wrap_text(step.detail, room)
        lines.append(_fit(prefix + chunks[0], inner))
        indent = " " * len(prefix)
        for extra in chunks[1:]:
            lines.append(_fit(indent + extra, inner))
    if notes:
        lines.append("├" + "─" * inner + "┤")
        for note in notes:
            for piece in wrap_text(note, inner):
                lines.append(_fit(piece, inner))
    lines.append("└" + "─" * inner + "┘")
    return "\n".join(lines)


def tint_panel_line(line: str) -> str:
    for word, code in STATE_COLOR.items():
        token = f"  {word}"
        if token in line:
            return line.replace(token, f"  {code}{word}{RESET}", 1)
    return line


def status_line(step: Step) -> str:
    state = STATE_LABEL.get(step.state, step.state)
    return f"{state:<8}{step.label:<14}{step.detail}"


class LaunchConsole:
    """Wordmark once, then a progress panel.

    On a TTY the panel redraws in place until a command streams output.
    After that stream the next panel is printed below it. NO_COLOR and
    non-TTY output are plain lines with no cursor codes and no color.
    """

    def __init__(
        self,
        stream: TextIO,
        steps: list[Step],
        *,
        cursor: bool,
        color: bool,
        version: str,
    ) -> None:
        self.stream = stream
        self.steps = steps
        self.cursor = cursor
        self.color = color
        self.version = version
        self.notes: list[str] = []
        self.anchored = False
        self.occupied = 0

    def begin(self) -> None:
        art = render_wordmark(self.version)
        if self.color:
            art = CYAN + art + RESET
        self.stream.write(art + "\n")
        self.stream.flush()
        if self.cursor:
            self.anchored = False
            self.occupied = 0
            self.publish()

    def set(self, key: str, state: str, detail: str) -> None:
        step = next(item for item in self.steps if item.key == key)
        step.state = state
        step.detail = detail
        self._show(step)

    def note(self, message: str) -> None:
        running = next((step for step in self.steps if step.state == "run"), None)
        if running is None:
            self.footnote(message)
            return
        running.detail = message
        self._show(running)

    def footnote(self, text: str) -> None:
        self.notes = text.splitlines()
        if self.cursor:
            self.publish()
            return
        if text:
            self.stream.write(text.rstrip() + "\n")
        self.stream.flush()

    def show_message(self, key: str, state: str, detail: str, message: str) -> None:
        self.notes = message.splitlines()
        self.set(key, state, detail)
        if self.cursor or not message:
            return
        self.stream.write(message.rstrip() + "\n")
        self.stream.flush()

    def fail_active(self, detail: str) -> None:
        running = next((step for step in self.steps if step.state == "run"), None)
        if running is not None:
            running.state = "fail"
            running.detail = detail
            self._show(running)
            return
        if any(step.state == "fail" for step in self.steps):
            return
        waiting = next((step for step in self.steps if step.state == "wait"), None)
        target = waiting or (self.steps[-1] if self.steps else None)
        if target is None:
            self.stream.write(detail + "\n")
            self.stream.flush()
            return
        target.state = "fail"
        target.detail = detail
        self._show(target)

    def break_anchor(self) -> None:
        self.anchored = False
        self.occupied = 0

    def publish(self) -> None:
        lines = render_panel(self.steps, self.notes or None).splitlines()
        if self.color:
            lines = [tint_panel_line(line) for line in lines]
        distance = self.occupied if self.cursor and self.anchored and self.occupied else 0
        if distance:
            self.stream.write(f"\033[{distance}A")
        for line in lines:
            if distance:
                self.stream.write("\033[2K")
            self.stream.write(line + "\n")
        if distance > len(lines):
            for _ in range(distance - len(lines)):
                self.stream.write("\033[2K\n")
            self.occupied = distance
        else:
            self.occupied = len(lines)
        self.anchored = True
        self.stream.flush()

    def _show(self, step: Step) -> None:
        if self.cursor:
            self.publish()
            return
        self.stream.write(status_line(step) + "\n")
        self.stream.flush()


CONSOLE: LaunchConsole | None = None


def open_console(version: str, service: bool) -> LaunchConsole:
    global CONSOLE
    configure_stdout()
    enabled = detect_presentation()
    CONSOLE = LaunchConsole(
        sys.stdout,
        make_steps(service),
        cursor=enabled,
        color=enabled,
        version=version,
    )
    CONSOLE.begin()
    return CONSOLE


def optional_project_version() -> str:
    if not PYPROJECT.is_file():
        return ""
    try:
        return read_project_version(PYPROJECT.read_text(encoding="utf-8"))
    except SystemExit:
        return ""


def phase(key: str, state: str, detail: str) -> None:
    if CONSOLE is None:
        print(f"{STATE_LABEL.get(state, state):<8}{key:<14}{detail}", flush=True)
        return
    CONSOLE.set(key, state, detail)


def release_panel() -> None:
    if CONSOLE is not None:
        CONSOLE.break_anchor()


def say(message: str) -> None:
    if CONSOLE is None:
        print(message, flush=True)
        return
    CONSOLE.note(message)


def listen_detail(host: str, port: str) -> str:
    shown = listen_url(host, port)
    warning = open_host_warning(host, os.environ.get("DOMAIN_MONITOR_PASSWORD") or None)
    if not warning:
        return shown
    return f"{shown} — Warning: {warning}"


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


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_showing(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    release_panel()
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
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            chunks.append(line)
        return process.wait(), "".join(chunks)
    except KeyboardInterrupt:
        _stop_process(process)
        raise


def run_inherited(argv: list[str]) -> None:
    release_panel()
    process = subprocess.Popen(argv, cwd=ROOT)
    try:
        code = process.wait()
    except KeyboardInterrupt:
        _stop_process(process)
        raise
    if code != 0:
        raise subprocess.CalledProcessError(code, argv)


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
    return interpreter_path(sys.platform)


def venv_is_current() -> bool:
    python = venv_python()
    if not python.is_file():
        return False
    found = interpreter_version(python)
    return python_version_ok(found or (0, 0))


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def venv_skip_detail() -> str:
    other = os.environ.get("VIRTUAL_ENV")
    if not other:
        return "already current"
    try:
        elsewhere = Path(other).resolve() != VENV.resolve()
    except OSError:
        elsewhere = True
    if elsewhere:
        return "using .venv"
    return "already current"


def _make_writable(function, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_old_venv() -> None:
    if not VENV.exists():
        return
    phase("venv", "run", "replacing .venv")
    try:
        shutil.rmtree(VENV, onexc=_make_writable)
    except OSError:
        phase("venv", "fail", REPLACE_VENV_MESSAGE)
        print(REPLACE_VENV_MESSAGE, file=sys.stderr)
        raise SystemExit(1) from None


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
        if sys.platform == "win32":
            phase("venv", "fail", WINDOWS_VENV_MESSAGE)
            print(WINDOWS_VENV_MESSAGE, file=sys.stderr)
            raise SystemExit(1)
        package = venv_apt_package(sys.version_info)
        phase("venv", "run", f"installing {package}")
        install_apt_packages([package])
        if not ensurepip_available():
            text = f"{package} is still missing after apt install."
            phase("venv", "fail", text)
            raise SystemExit(text)
    phase("venv", "run", "creating .venv")
    run_inherited([sys.executable, "-m", "venv", str(VENV)])


def query_imports(python: Path, names: tuple[str, ...] = CORE_IMPORTS) -> bool:
    joined = ", ".join(names)
    result = subprocess.run(
        [str(python), "-c", f"import {joined}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def pip_install(requirement: str, *, editable: bool) -> None:
    run_inherited(pip_install_argv(venv_python(), requirement, editable=editable))


def extras_already_present(python: Path) -> InstallProfile:
    """The largest extra this virtualenv can already import.

    A version bump reinstalls that extra after core, so a later
    ``pip install -e .`` does not drop Playwright or whoisit.
    """
    if query_imports(python, BROWSER_IMPORTS):
        return FULL_PROFILE
    if query_imports(python, REGISTRATION_IMPORTS):
        return WITHOUT_BROWSER_PROFILE
    return CORE_PROFILE


def _package_step_needed(
    step: PackageStep,
    *,
    repair: bool,
    python: Path,
    installed: str | None,
    core_imports_ok: bool,
    version: str,
) -> bool:
    if step.kind == "core":
        return needs_pip_install(installed, core_imports_ok, version, repair)
    if step.kind == "registration":
        return repair or not query_imports(python, REGISTRATION_IMPORTS)
    if step.kind == "playwright":
        return repair or not query_imports(python, BROWSER_IMPORTS)
    return False


def ensure_packages(repair: bool, profile: InstallProfile) -> bool:
    """Install core, then optional extras. Return whether Playwright imports.

    A core failure still raises. A registration or Playwright failure is
    recorded and does not stop the launcher.
    """
    project_text = PYPROJECT.read_text(encoding="utf-8")
    version = read_project_version(project_text)
    python = venv_python()
    installed = query_installed_version(python)
    core_imports_ok = query_imports(python, CORE_IMPORTS)
    label = f"domain-monitor {version}"
    playwright = ""
    if set(BROWSER_IMPORTS).issubset(profile.imports):
        playwright = playwright_requirement(project_text)
    ran = False
    skipped: list[str] = []
    for step in package_plan(profile, playwright):
        if not _package_step_needed(
            step,
            repair=repair,
            python=python,
            installed=installed,
            core_imports_ok=core_imports_ok,
            version=version,
        ):
            continue
        ran = True
        phase("packages", "run", pip_install_detail(step))
        try:
            pip_install(step.requirement, editable=step.editable)
        except subprocess.CalledProcessError:
            if package_failure_is_fatal(step):
                raise
            skipped.append(step.kind)
    playwright_ok = query_imports(python, BROWSER_IMPORTS)
    if skipped:
        phase("packages", "fail", optional_package_detail(label, skipped))
        say(optional_skip_message(skipped))
    elif ran:
        phase("packages", "ok", label)
    else:
        phase("packages", "skip", label)
    return playwright_ok


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


def mark_chromium_deps(browsers: Path) -> None:
    browsers.mkdir(parents=True, exist_ok=True)
    (browsers / DEPS_STAMP_NAME).write_text("ok\n", encoding="utf-8")


def mark_chromium_download(browsers: Path) -> None:
    browsers.mkdir(parents=True, exist_ok=True)
    (browsers / CHROMIUM_STAMP_NAME).write_text("ok\n", encoding="utf-8")


def install_chromium_browser() -> bool:
    """Download Chromium. Return False when every attempt fails. Do not stamp."""
    argv = chromium_install_argv(venv_python())
    env = browser_env()
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        detail = (
            f"downloading into .playwright, attempt {attempt} of {DOWNLOAD_ATTEMPTS}"
        )
        if attempt == 1:
            detail += "; this can take a while"
        phase("chromium", "run", detail)
        code, output = run_showing(argv, env)
        if code == 0:
            return True
        if browser_download_interrupted(output) and attempt < DOWNLOAD_ATTEMPTS:
            wait = download_wait_seconds(attempt)
            phase("chromium", "run", f"reset, retry in {wait}s")
            time.sleep(wait)
            continue
        return False


def libraries_skip_detail(platform: str) -> str:
    if platform == "win32":
        return "not used on Windows"
    return "not part of this install"


def library_failure(detail: str, message: str) -> None:
    phase("libraries", "fail", detail)
    say(message)


def install_system_libraries() -> bool:
    """Install Chromium's libraries. Return True only when that command exits 0.

    A missing apt, a refused sudo password, a failed install, or a package
    list that names Docker or WSL leaves the stamp unwritten.
    """
    if shutil.which("apt-get") is None:
        library_failure(
            "apt-get is not available; screenshots off",
            "Chromium's libraries were not installed. Screenshots stay off. "
            "Run launch.py --repair to retry.",
        )
        return False
    phase("libraries", "run", "checking packages")
    _code, output = run_showing(library_dry_run_argv(venv_python()))
    blocked = forbidden_package(output)
    if blocked is not None:
        library_failure(
            f"refused {blocked}; screenshots off",
            f"Chromium's library install named {blocked}. "
            "That step was skipped. Screenshots stay off.",
        )
        return False
    phase("libraries", "run", "apt via sudo")
    try:
        run_as_root(
            library_install_argv(venv_python()),
            extra_env={"DEBIAN_FRONTEND": "noninteractive"},
            retry_lock=True,
        )
    except (subprocess.CalledProcessError, SystemExit):
        library_failure(
            "library install failed; screenshots off",
            "Chromium's libraries were not installed. Screenshots stay off. "
            "Run launch.py --repair to retry.",
        )
        return False
    return True


def ensure_chromium(repair: bool, *, fetch: bool) -> None:
    if not fetch:
        if chromium_present(BROWSERS):
            phase("chromium", "skip", "already present")
        else:
            phase("chromium", "skip", "screenshots off")
        phase("libraries", "skip", libraries_skip_detail(sys.platform))
        return
    if chromium_download_needed(BROWSERS, repair):
        BROWSERS.mkdir(parents=True, exist_ok=True)
        if not install_chromium_browser():
            phase("chromium", "fail", "download failed; screenshots off")
            phase("libraries", "skip", "screenshots off")
            say(
                "Chromium download failed. Screenshots stay off. "
                "Run launch.py --repair to retry."
            )
            return
        mark_chromium_download(BROWSERS)
        phase("chromium", "ok", "installed")
    else:
        phase("chromium", "skip", "already stamped")
    if not system_libraries_needed(sys.platform, True):
        phase("libraries", "skip", libraries_skip_detail(sys.platform))
        return
    if not repair and (BROWSERS / DEPS_STAMP_NAME).is_file():
        phase("libraries", "skip", "stamp present")
        return
    if install_system_libraries():
        mark_chromium_deps(BROWSERS)
        phase("libraries", "ok", "installed")


def checkout_ready() -> None:
    if not PYPROJECT.is_file() or not (ROOT / "app" / "main.py").is_file():
        text = "launch.py must stay in the Domain Monitor repository."
        phase("platform", "fail", text)
        raise SystemExit(text)


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


def install_service() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8000")
    password = os.environ.get("DOMAIN_MONITOR_PASSWORD") or None
    secret = os.environ.get("DOMAIN_MONITOR_SECRET") or None
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
    phase("listen", "ok", listen_detail(host, port))
    user, group = service_account()
    if user == "root":
        phase("start", "run", f"writing {UNIT_PATH} as root; Chromium uses --no-sandbox")
    else:
        phase("start", "run", f"writing {UNIT_PATH}")
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
    write_text_as_root(UNIT_PATH, content, mode)
    phase("start", "run", "enabling domain-monitor.service")
    systemctl("daemon-reload")
    systemctl("enable", "domain-monitor.service")
    systemctl("restart", "domain-monitor.service")
    phase("start", "ok", f"running at {listen_url(host, port)}")


def start_process() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8000")
    phase("listen", "ok", listen_detail(host, port))
    phase("start", "run", f"starting at {listen_url(host, port)}")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    python = venv_python()
    try:
        os.execv(str(python), [str(python), "-m", "app.main"])
    except OSError:
        phase("start", "fail", START_FAILED_MESSAGE)
        print(START_FAILED_MESSAGE, file=sys.stderr)
        raise SystemExit(1) from None


def profile_is_ready(profile: InstallProfile) -> bool:
    if not venv_is_current():
        return False
    version = read_project_version(PYPROJECT.read_text(encoding="utf-8"))
    python = venv_python()
    installed = query_installed_version(python)
    imports_ok = query_imports(python, profile.imports)
    if needs_pip_install(installed, imports_ok, version, repair=False):
        return False
    if profile.chromium and chromium_download_needed(BROWSERS, repair=False):
        return False
    if system_libraries_needed(sys.platform, profile.chromium):
        return (BROWSERS / DEPS_STAMP_NAME).is_file()
    return True


def show_launch_state() -> None:
    version = read_project_version(PYPROJECT.read_text(encoding="utf-8"))
    python = venv_python()
    phase("venv", "skip", venv_skip_detail())
    phase("packages", "skip", f"domain-monitor {version}")
    browser_here = query_imports(python, BROWSER_IMPORTS) and chromium_present(BROWSERS)
    if browser_here:
        phase("chromium", "skip", "already present")
    else:
        phase("chromium", "skip", "screenshots off")
    if system_libraries_needed(sys.platform, browser_here) and (BROWSERS / DEPS_STAMP_NAME).is_file():
        phase("libraries", "skip", "stamp present")
    else:
        phase("libraries", "skip", libraries_skip_detail(sys.platform))


def confirm_install(replacing: bool) -> InstallProfile | None:
    release_panel()
    if replacing:
        lead = "The existing .venv is not usable and will be replaced."
    else:
        lead = "No .venv yet."
    question = (
        f"{lead}\n"
        "1  Full install, including Chromium\n"
        "2  Without the browser\n"
        "Choose [1/2] or press Enter to cancel: "
    )
    try:
        answer = input(question)
    except EOFError:
        return None
    return install_choice(answer)


def ensure_ready(repair: bool, assume_yes: bool, without_browser: bool) -> bool:
    """Return False when the user declines. True when the process may start."""
    os.chdir(ROOT)
    checkout_ready()
    phase("platform", "ok", platform_label(sys.platform))
    if not python_version_ok(sys.version_info):
        text = (
            f"Python {version_text(sys.version_info)} is too old. "
            "Domain Monitor needs Python 3.12 or newer."
        )
        phase("python", "fail", text)
        raise SystemExit(text)
    phase("python", "ok", version_text(sys.version_info))
    current = venv_is_current()
    ready = profile_is_ready(CORE_PROFILE) if current else False
    action = next_action(
        venv_current=current,
        profile_ready=ready,
        repair=repair,
        assume_yes=assume_yes or without_browser,
    )
    profile = CORE_PROFILE
    fetch_browser = False
    if action == "ask":
        if not stdin_is_tty():
            phase("venv", "fail", NONINTERACTIVE_MESSAGE)
            print(NONINTERACTIVE_MESSAGE, file=sys.stderr)
            raise SystemExit(2)
        chosen = confirm_install(replacing=VENV.exists())
        if chosen is None:
            phase("venv", "skip", DECLINE_MESSAGE)
            return False
        profile = chosen
        fetch_browser = chosen.chromium
    elif action == "launch":
        show_launch_state()
        return True
    elif without_browser:
        profile = WITHOUT_BROWSER_PROFILE
        fetch_browser = False
    elif assume_yes or repair or not current:
        profile = FULL_PROFILE
        fetch_browser = True
    else:
        profile = extras_already_present(venv_python())
        fetch_browser = False
    replaced = False
    if not venv_is_current():
        replaced = VENV.exists()
        remove_old_venv()
        create_venv()
        phase("venv", "ok", "replaced" if replaced else "created")
    else:
        phase("venv", "skip", venv_skip_detail())
    playwright_ok = ensure_packages(repair, profile)
    fetch_browser = fetch_browser_after_packages(fetch_browser, playwright_ok)
    ensure_chromium(repair, fetch=fetch_browser)
    return True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Domain Monitor on Linux or Windows and start it."
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Reinstall packages and download Chromium.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Create .venv and install the full feature set, including Chromium, without asking.",
    )
    parser.add_argument(
        "--without-browser",
        action="store_true",
        help="Install registration lookups without Chromium, and do not ask.",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Install a systemd service and start it. Linux only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    open_console(optional_project_version(), args.service)
    message = unsupported_platform_message(sys.platform)
    if message is not None:
        if CONSOLE is None:
            print(message, file=sys.stderr)
            return 2
        CONSOLE.show_message("platform", "fail", "not Linux or Windows", message)
        return 2
    if args.service and sys.platform != "linux":
        phase("start", "fail", SERVICE_WINDOWS_MESSAGE)
        print(SERVICE_WINDOWS_MESSAGE, file=sys.stderr)
        return 2
    try:
        if not ensure_ready(args.repair, args.yes or args.repair, args.without_browser):
            return 0
        if args.service:
            install_service()
            return 0
        start_process()
    except KeyboardInterrupt:
        if CONSOLE is not None:
            CONSOLE.fail_active("cancelled")
        print(CANCEL_MESSAGE, file=sys.stderr)
        return 130
    except subprocess.CalledProcessError as exc:
        command = " ".join(str(part) for part in exc.cmd) if exc.cmd else "command"
        line = f"Command failed ({exc.returncode}): {command}"
        if CONSOLE is not None:
            CONSOLE.fail_active(f"{line}. {CONTINUE_MESSAGE}")
        print(line, file=sys.stderr)
        return exc.returncode or 1
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            return 0
        if isinstance(code, int):
            return code
        text = str(code)
        if CONSOLE is not None:
            CONSOLE.fail_active(text.splitlines()[0])
        print(text, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
