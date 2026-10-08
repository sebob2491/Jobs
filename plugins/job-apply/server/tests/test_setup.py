"""First run on the person's own computer: finding a browser to drive."""

from conftest import run

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


def test_an_unexpanded_plugin_root_is_ignored():
    """Claude Desktop passed .mcp.json's env through as the literal "${CLAUDE_PLUGIN_ROOT}":
    setup found no profile template to copy and the desk no employer list."""
    own = config.PACKAGE_DIR.parents[2]
    assert (own / "templates" / "profile.example.yaml").exists()
    assert (own / "data" / "companies.yaml").exists()
    for unusable in ("${CLAUDE_PLUGIN_ROOT}", "", None):
        assert config._plugin_root(unusable) == own
    assert config._plugin_root("/opt/job-apply") == config.Path("/opt/job-apply")


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


def test_the_email_app_password_is_never_typed_into_a_page(srv, monkeypatch):
    """The key to the person's inbox: Claude can't have it typed anywhere, even by name."""
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    for name in ("email_password", "EMAIL_PASSWORD", "email-password "):
        out = run(srv.fill_secret("1", name))
        assert out["ok"] is False and "never typed into a page" in out["error"]


def test_edge_standing_in_for_chrome_keeps_its_own_profile(tmp_path):
    from job_apply.browser import profile_dir

    chrome = config.Profile({"settings": {"browser_channel": "chrome"}}).settings
    edge = config.Profile({"settings": {"browser_channel": "msedge"}}).settings
    home = tmp_path / "browser"
    assert profile_dir(home, {"channel": "chrome"}, chrome) == home
    assert profile_dir(home, {"channel": "msedge"}, chrome) == tmp_path / "browser-msedge"
    assert profile_dir(home, {"channel": "msedge"}, edge) == home  # chosen: its profile is the usual one
    assert profile_dir(home, {}, chrome) == home


def test_a_zip_code_with_a_leading_zero_is_kept_as_written(job_apply_home):
    """Unquoted, 02134 reads as an octal number (1116) in YAML: the profile keeps 02134."""
    from job_apply import config

    (job_apply_home / "profile.yaml").write_text("personal:\n  first_name: Kim\n  address:\n    postal_code: 02134\n")
    assert config.Profile.load().get("personal.address.postal_code") == "02134"


def test_setup_status_says_whats_wrong_with_a_persons_employer_list(srv, job_apply_home):
    """A list name the plugin doesn't have, written during setup, shows at setup, not at the
    first search."""
    (job_apply_home / "companies.yaml").write_text("lists: [phoenix]\n")
    status = srv.setup_status()
    assert "no employer list named 'phoenix'" in status["employer_list_problem"]
    assert "phoenix-metro" in status["employer_lists"]
    (job_apply_home / "companies.yaml").write_text("lists: [phoenix-metro]\n")
    assert "employer_list_problem" not in srv.setup_status()
