from pathlib import Path

import pytest

from launch import (
    DEPS_STAMP_NAME,
    apt_lock_busy,
    chromium_present,
    chromium_ready,
    needs_pip_install,
    open_host_warning,
    python_version_ok,
    read_project_version,
    render_unit,
    root_argv,
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


def test_windows_gets_development_commands():
    message = unsupported_platform_message("win32")
    assert message is not None
    assert "python -m venv .venv" in message
    assert "apt-get" not in message
    assert unsupported_platform_message("linux") is None


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
