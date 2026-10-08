"""First run on the person's own computer: finding a browser to drive."""

from job_apply import config, server
from job_apply.browser import launch_attempts


def test_without_chrome_edge_comes_before_a_download(monkeypatch):
    """Every Windows computer has Microsoft Edge: tried before Playwright's own Chromium,
    which needs `playwright install chromium` first."""
    monkeypatch.delenv("JOB_APPLY_CHROMIUM_PATH", raising=False)
    chrome = config.Profile({"settings": {"browser_channel": "chrome"}}).settings
    assert launch_attempts(chrome) == [{"channel": "chrome"}, {"channel": "msedge"}, {}]
    edge = config.Profile({"settings": {"browser_channel": "msedge"}}).settings
    assert launch_attempts(edge) == [{"channel": "msedge"}, {}]
    bundled = config.Profile({"settings": {"browser_channel": "chromium"}}).settings
    assert launch_attempts(bundled) == [{}]
    monkeypatch.setenv("JOB_APPLY_CHROMIUM_PATH", "/opt/chromium")
    assert launch_attempts(chrome) == [{"executable_path": "/opt/chromium"}]


def test_chrome_installed_for_one_user_is_found(monkeypatch, tmp_path):
    """Chrome's Windows installer puts it in the user's AppData when they can't install for
    everyone; setup_status said "no Chrome" there."""
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.setattr(server.Path, "home", lambda: tmp_path)
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    assert not server.chrome_installed()
    exe = tmp_path / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert server.chrome_installed()
