import io
from pathlib import Path

import pytest

from launch import (
    DEPS_STAMP_NAME,
    CHROMIUM_STAMP_NAME,
    DOWNLOAD_IDLE_TIMEOUT,
    GLYPHS,
    LaunchConsole,
    Step,
    apt_lock_busy,
    apply_download_timeout,
    browser_download_interrupted,
    chromium_download_needed,
    chromium_present,
    chromium_ready,
    compose_word,
    interpreter_path,
    make_steps,
    needs_pip_install,
    next_action,
    open_host_warning,
    CORE_PROFILE,
    FULL_PROFILE,
    WITHOUT_BROWSER_PROFILE,
    chromium_install_argv,
    fetch_browser_after_packages,
    forbidden_package,
    install_choice,
    library_dry_run_argv,
    library_install_argv,
    optional_package_detail,
    optional_skip_message,
    package_failure_is_fatal,
    package_plan,
    playwright_requirement,
    PYPROJECT,
    NONINTERACTIVE_MESSAGE,
    presentation_enabled,
    python_version_ok,
    read_project_version,
    render_panel,
    render_unit,
    render_wordmark,
    root_argv,
    system_libraries_needed,
    unsupported_platform_message,
    venv_apt_package,
    venv_satisfies,
)


def test_python_version_gate():
    assert not python_version_ok((3, 11, 9))
    assert python_version_ok((3, 12, 0))
    assert python_version_ok((3, 13, 1))


def test_venv_satisfies_only_when_version_and_imports_match():
    assert venv_satisfies("0.1.1", True, "0.1.1")
    assert not venv_satisfies("0.1.0", True, "0.1.1")
    assert not venv_satisfies("0.1.1", False, "0.1.1")
    assert not venv_satisfies(None, True, "0.1.1")


def test_needs_pip_install_when_repair_or_unsatisfied():
    assert not needs_pip_install("0.1.1", True, "0.1.1", repair=False)
    assert needs_pip_install("0.1.1", True, "0.1.1", repair=True)
    assert needs_pip_install("0.1.0", True, "0.1.1", repair=False)
    assert needs_pip_install(None, False, "0.1.1", repair=False)


def test_project_version_reads_the_project_table():
    text = '[project]\nname = "domain-monitor"\nversion = "1.2.3"\n'
    assert read_project_version(text) == "1.2.3"


def test_project_version_rejects_a_missing_version():
    with pytest.raises(SystemExit):
        read_project_version('[project]\nname = "domain-monitor"\n')


def test_chromium_present_finds_the_linux_binary(tmp_path: Path):
    assert not chromium_present(tmp_path)
    binary = tmp_path / "chromium-1208" / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    assert chromium_present(tmp_path)
    assert not chromium_ready(tmp_path)
    (tmp_path / DEPS_STAMP_NAME).write_text("ok\n", encoding="utf-8")
    assert chromium_ready(tmp_path)


def test_chromium_present_accepts_chrome_linux(tmp_path: Path):
    binary = tmp_path / "chromium-1100" / "chrome-linux" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    assert chromium_present(tmp_path)


def test_chromium_present_ignores_a_directory_without_the_binary(tmp_path: Path):
    revision = tmp_path / "chromium-1100"
    revision.mkdir()
    (revision / "README").write_text("missing", encoding="utf-8")
    other = tmp_path / "firefox-100" / "chrome-linux"
    other.mkdir(parents=True)
    (other / "chrome").write_bytes(b"")
    assert not chromium_present(tmp_path)
    assert not chromium_present(tmp_path / "missing")


def test_windows_and_linux_are_supported():
    assert unsupported_platform_message("win32") is None
    assert unsupported_platform_message("linux") is None
    message = unsupported_platform_message("darwin")
    assert message is not None
    assert "Linux or Windows" in message
    assert "apt-get" not in message


def test_chromium_present_accepts_windows_chrome(tmp_path: Path):
    binary = tmp_path / "chromium-1208" / "chrome-win" / "chrome.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    assert chromium_present(tmp_path)


def test_open_host_warning_only_without_a_password():
    assert open_host_warning("127.0.0.1", None) is None
    assert open_host_warning("localhost", None) is None
    assert open_host_warning("::1", None) is None
    assert open_host_warning("0.0.0.0", "secret") is None
    warning = open_host_warning("0.0.0.0", None)
    assert warning is not None
    assert "DOMAIN_MONITOR_PASSWORD" in warning


def test_service_unit_starts_the_venv_python():
    text = render_unit(
        user="monitor",
        group="monitor",
        root=Path("/opt/domain-monitor"),
        python=Path("/opt/domain-monitor/.venv/bin/python"),
        browsers=Path("/opt/domain-monitor/.playwright"),
        data_dir=Path("/opt/domain-monitor/data"),
        host="127.0.0.1",
        port="8000",
        password=None,
        secret=None,
    )
    assert "ExecStart=/opt/domain-monitor/.venv/bin/python -m app.main" in text
    assert "launch.py" not in text
    assert "User=monitor" in text
    assert 'Environment="PLAYWRIGHT_BROWSERS_PATH=/opt/domain-monitor/.playwright"' in text
    assert 'Environment="DATA_DIR=/opt/domain-monitor/data"' in text
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text


def test_service_unit_quotes_a_password():
    text = render_unit(
        user="monitor",
        group="monitor",
        root=Path("/opt/domain-monitor"),
        python=Path("/opt/domain-monitor/.venv/bin/python"),
        browsers=Path("/opt/domain-monitor/.playwright"),
        data_dir=Path("/opt/domain-monitor/data"),
        host="127.0.0.1",
        port="8000",
        password="a b#c",
        secret=None,
    )
    assert 'Environment="DOMAIN_MONITOR_PASSWORD=a b#c"' in text


def test_venv_package_matches_the_running_interpreter():
    assert venv_apt_package((3, 13)) == "python3.13-venv"
    assert venv_apt_package((3, 12, 3)) == "python3.12-venv"


def test_root_command_is_plain_sudo_apt():
    argv = root_argv("sudo", ["apt-get", "install", "-y", "python3.13-venv"])
    assert argv == ["sudo", "apt-get", "install", "-y", "python3.13-venv"]
    assert "env" not in argv


def test_root_command_sets_variables_inside_the_shell():
    argv = root_argv(
        "sudo",
        ["/opt/domain monitor/.venv/bin/python", "-m", "playwright", "install-deps", "chromium"],
        {"DEBIAN_FRONTEND": "noninteractive"},
    )
    assert argv[:3] == ["sudo", "bash", "-c"]
    assert "env" not in argv
    script = argv[3]
    assert script.startswith("export DEBIAN_FRONTEND=noninteractive;")
    assert "'/opt/domain monitor/.venv/bin/python'" in script
    assert "install-deps" in script


def test_apt_lock_frontend_is_retryable():
    busy = "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1000 (packagekitd)"
    assert apt_lock_busy(busy)
    assert not apt_lock_busy("E: Unable to locate package python3.13-venv")


def test_browser_download_reset_is_retryable():
    assert browser_download_interrupted("Error: read ECONNRESET")
    assert browser_download_interrupted("socket hang up")
    assert not browser_download_interrupted("Chromium 131.0 is already installed")


def test_download_timeout_is_applied_only_when_unset():
    missing: dict[str, str] = {}
    apply_download_timeout(missing)
    assert missing["PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"] == DOWNLOAD_IDLE_TIMEOUT
    chosen = {"PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "1000"}
    apply_download_timeout(chosen)
    assert chosen["PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"] == "1000"


def test_wordmark_names_the_app_and_fits():
    text = render_wordmark("1.2.3")
    assert "DOMAIN" in text
    assert "MONITOR" in text
    assert "local uplink" in text
    assert "1.2.3" in text
    assert "\033" not in text
    for line in text.splitlines():
        assert len(line) <= 76


def test_glyphs_are_rectangular():
    for rows in GLYPHS.values():
        assert len({len(row) for row in rows}) == 1
    for word in ("DOMAIN", "MONITOR"):
        art = compose_word(word)
        assert max(len(row) for row in art) <= 74


def test_panel_states():
    steps = [
        Step("python", "Python", "ok", "3.13.1"),
        Step("venv", "Virtualenv", "skip", "already current"),
        Step("packages", "Packages", "run", "pip install -e ."),
        Step("chromium", "Chromium", "fail", "download failed"),
    ]
    text = render_panel(steps)
    assert "done" in text
    assert "skipped" in text
    assert "running" in text
    assert "failed" in text
    assert "3.13.1" in text
    assert "already current" in text
    assert "pip install -e ." in text
    assert "download failed" in text
    assert "\033" not in text
    for line in text.splitlines():
        assert len(line) <= 76


def test_panel_wraps_a_long_detail():
    steps = [Step("listen", "Listen", "ok", "http://127.0.0.1:8000 — Warning: " + ("x" * 180))]
    text = render_panel(steps)
    assert "Warning:" in text
    assert text.count("x") == 180
    for line in text.splitlines():
        assert len(line) <= 76


def test_service_panel_names_the_unit_phase():
    text = render_panel(make_steps(True))
    assert "Service" in text
    assert "Start" not in text


def test_plain_console_has_no_escape_codes():
    stream = io.StringIO()
    console = LaunchConsole(
        stream, make_steps(False), cursor=False, color=False, version="0.1.5"
    )
    console.begin()
    console.set("python", "ok", "3.13.1")
    console.set("venv", "skip", "already current")
    text = stream.getvalue()
    assert "\033" not in text
    assert "DOMAIN" in text
    assert "MONITOR" in text
    assert "done" in text
    assert "3.13.1" in text
    assert "skipped" in text
    assert "already current" in text


def test_panel_redraws_in_place_until_output_streams():
    stream = io.StringIO()
    steps = make_steps(False)
    console = LaunchConsole(stream, steps, cursor=True, color=False, version="0.1.5")
    console.begin()
    assert "\033" not in stream.getvalue()
    console.set("python", "ok", "3.13.1")
    height = len(render_panel(steps).splitlines())
    text = stream.getvalue()
    assert f"\033[{height}A" in text
    assert text.count("DOMAIN") == 1
    console.break_anchor()
    console.set("venv", "run", "creating .venv")
    text = stream.getvalue()
    assert text.count("DOMAIN") == 1
    assert "creating .venv" in text


def test_noninteractive_install_message_fits_the_panel():
    steps = make_steps(False)
    for step in steps:
        if step.key == "venv":
            step.state = "fail"
            step.detail = NONINTERACTIVE_MESSAGE
    text = render_panel(steps)
    assert "failed" in text
    assert "--yes" in text
    for line in text.splitlines():
        assert len(line) <= 76


def test_interpreter_path_follows_the_platform():
    assert interpreter_path("win32").parts[-3:] == (".venv", "Scripts", "python.exe")
    assert interpreter_path("linux").parts[-3:] == (".venv", "bin", "python")


def test_install_choice_is_full_or_without_the_browser():
    assert install_choice("1") is FULL_PROFILE
    assert install_choice("2") is WITHOUT_BROWSER_PROFILE
    assert install_choice("") is None
    assert install_choice("\n") is None
    assert install_choice("y") is None
    assert install_choice("yes") is None


def test_without_browser_profile_does_not_require_playwright():
    assert "playwright" not in WITHOUT_BROWSER_PROFILE.imports
    assert "whoisit" in WITHOUT_BROWSER_PROFILE.imports
    assert "whois" in WITHOUT_BROWSER_PROFILE.imports
    assert WITHOUT_BROWSER_PROFILE.chromium is False
    assert "playwright" in FULL_PROFILE.imports
    assert FULL_PROFILE.chromium is True
    assert "playwright" not in CORE_PROFILE.imports


def test_package_plan_installs_core_before_optional_extras():
    spec = playwright_requirement(PYPROJECT.read_text(encoding="utf-8"))
    assert spec.startswith("playwright")
    core = package_plan(CORE_PROFILE, spec)
    without = package_plan(WITHOUT_BROWSER_PROFILE, spec)
    full = package_plan(FULL_PROFILE, spec)
    assert [step.kind for step in core] == ["core"]
    assert [step.kind for step in without] == ["core", "registration"]
    assert [step.kind for step in full] == ["core", "registration", "playwright"]
    assert [step.requirement for step in without] == [".", ".[registration]"]
    assert full[-1].requirement == spec
    assert full[-1].editable is False
    assert ".[full]" not in [step.requirement for step in full]
    for step in full:
        assert package_failure_is_fatal(step) is step.required
    assert package_failure_is_fatal(full[0])
    assert not package_failure_is_fatal(full[1])
    assert not package_failure_is_fatal(full[2])


def test_optional_package_failure_still_starts_http_checks():
    assert not fetch_browser_after_packages(True, False)
    assert fetch_browser_after_packages(True, True)
    assert not fetch_browser_after_packages(False, True)
    detail = optional_package_detail("domain-monitor 0.1.5", ["registration", "playwright"])
    assert "registration" in detail
    assert "Playwright" in detail
    message = optional_skip_message(["registration"])
    assert "Registration lookups stay off." in message
    assert "HTTP checks will still start." in message
    both = optional_skip_message(["registration", "playwright"])
    assert "Screenshots stay off." in both
    assert "HTTP checks will still start." in both


def test_browser_commands_install_chromium_only():
    python = Path("/opt/domain-monitor/.venv/bin/python")
    commands = (
        chromium_install_argv(python),
        library_install_argv(python),
        library_dry_run_argv(python),
    )
    for argv in commands:
        lowered = [part.lower() for part in argv]
        assert "webkit" not in lowered
        assert "firefox" not in lowered
        assert "--with-deps" not in lowered
        assert "docker" not in " ".join(lowered)
        assert "wsl" not in " ".join(lowered)
    assert chromium_install_argv(python)[-2:] == ["install", "chromium"]
    assert library_install_argv(python)[-2:] == ["install-deps", "chromium"]
    assert library_dry_run_argv(python)[-3:] == ["install-deps", "chromium", "--dry-run"]
    assert not system_libraries_needed("win32", True)


def test_library_dry_run_refuses_container_tools():
    assert forbidden_package("Inst docker.io (28.0)") == "docker.io"
    assert forbidden_package("docker-ce docker-ce-cli") == "docker-ce"
    assert forbidden_package("containerd.io") == "containerd"
    assert forbidden_package("Install podman") == "podman"
    assert forbidden_package("wsl --install") == "wsl"
    assert forbidden_package("Please update the Docker image") == "docker"
    chromium_libs = "\n".join(
        (
            "Inst libnss3",
            "Inst libgbm1",
            "Inst libgtk-3-0",
            "Inst fonts-liberation",
            "Inst xvfb",
        )
    )
    assert forbidden_package(chromium_libs) is None


def test_next_action_does_not_install_for_a_missing_browser():
    # profile_ready is the core package. A missing Chromium binary is not part of it.
    assert (
        next_action(venv_current=True, profile_ready=True, repair=False, assume_yes=False)
        == "launch"
    )


def test_next_action_asks_only_without_a_current_venv():
    assert (
        next_action(venv_current=True, profile_ready=True, repair=False, assume_yes=False)
        == "launch"
    )
    assert (
        next_action(venv_current=True, profile_ready=False, repair=False, assume_yes=False)
        == "install"
    )
    assert (
        next_action(venv_current=True, profile_ready=True, repair=True, assume_yes=False)
        == "install"
    )
    assert (
        next_action(venv_current=False, profile_ready=False, repair=False, assume_yes=True)
        == "install"
    )
    assert (
        next_action(venv_current=False, profile_ready=False, repair=True, assume_yes=False)
        == "install"
    )
    assert (
        next_action(venv_current=False, profile_ready=False, repair=False, assume_yes=False)
        == "ask"
    )


def test_system_libraries_are_linux_chromium_only():
    assert system_libraries_needed("linux", True)
    assert not system_libraries_needed("linux", False)
    assert not system_libraries_needed("win32", True)


def test_failure_marks_the_running_phase():
    stream = io.StringIO()
    console = LaunchConsole(
        stream, make_steps(False), cursor=False, color=False, version="0.1.5"
    )
    console.set("packages", "run", "pip install -e .")
    console.fail_active("Command failed (1): pip")
    packages = next(step for step in console.steps if step.key == "packages")
    assert packages.state == "fail"
    assert "Command failed (1): pip" in stream.getvalue()
    before = stream.getvalue()
    console.fail_active("Command failed (1): pip")
    assert stream.getvalue() == before


def test_presentation_is_plain_without_a_tty_or_color():
    assert presentation_enabled(tty=True, no_color=False, vt_ready=True)
    assert not presentation_enabled(tty=False, no_color=False, vt_ready=True)
    assert not presentation_enabled(tty=True, no_color=True, vt_ready=True)
    assert not presentation_enabled(tty=True, no_color=False, vt_ready=False)


def test_partial_chromium_is_downloaded_again(tmp_path: Path):
    binary = tmp_path / "chromium-1208" / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    assert chromium_download_needed(tmp_path, repair=False)
    (tmp_path / CHROMIUM_STAMP_NAME).write_text("ok\n", encoding="utf-8")
    assert not chromium_download_needed(tmp_path, repair=False)
    assert chromium_download_needed(tmp_path, repair=True)
