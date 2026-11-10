"""The desk says when a newer job-apply has been published, and its Update button installs it (design F2): an
installed copy downloads the newest Release's installer, checks its SHA-256 and starts it; a plugin copy runs Claude's
two update commands; a copy run from the source code says a version is out, and no more. No test here reaches the
network or starts a program: the check is handed a transport, and the update a Popen and a run of its own."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time

import httpx
import pytest
from conftest import browser_available, person_browser, run

from job_apply import config, updates, window
from job_apply.desk import Desk

REPO = "https://github.com/sebob2491/Jobs"
API = "https://api.github.com/repos/sebob2491/Jobs/releases/latest"
DOWNLOAD = "https://github.com/sebob2491/Jobs/releases/download/job-apply-v0.4.4/"
STORAGE = "https://objects.githubusercontent.com/release-assets/"  # where GitHub sends a download on to
INSTALLER = b"MZ" + b"a test installer, never run " * 40
DIGEST = hashlib.sha256(INSTALLER).hexdigest()


def test_versions_are_compared_as_numbers():
    assert updates.newer("0.3.10", "0.3.9") and updates.newer("0.4.0", "0.3.70")
    assert not updates.newer("0.3.70", "0.3.70") and not updates.newer("0.3.69", "0.3.70")
    assert not updates.newer("", "0.3.70") and not updates.newer("0.3.71", "") and not updates.newer("v1", "0.3.70")


def test_the_published_manifest_is_the_repositorys_own():
    assert updates.manifest_url() == \
        "https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/.claude-plugin/plugin.json"


def _github(monkeypatch, respond):
    """httpx.AsyncClient answering every request with respond(request); the requests seen."""
    seen = []
    real = httpx.AsyncClient

    def handler(request):
        seen.append(str(request.url))
        return respond(request)

    monkeypatch.setattr(updates.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "0")
    monkeypatch.setattr(config, "plugin_version", lambda: "0.3.70")
    return seen


def test_a_newer_published_version_is_said_with_how_to_get_it(monkeypatch, loop):
    seen = _github(monkeypatch, lambda r: httpx.Response(200, json={"name": "job-apply", "version": "0.3.71"}))
    monkeypatch.setattr(updates, "find_claude", lambda: None)
    check = updates.UpdateCheck()
    run(check.refresh())
    state = check.state()
    assert state == {"current": "0.3.70", "latest": "0.3.71", "newer": True, "kind": "manual",
                     "checked_at": state["checked_at"], "progress": None, "error": None,
                     "commands": updates.UPDATE_COMMANDS,
                     "notes": "https://github.com/sebob2491/Jobs/releases/tag/job-apply-v0.3.71"}
    assert state["checked_at"] > 0
    run(check.refresh())  # not looked at again within CHECK_EVERY
    assert len(seen) == 1


@pytest.mark.parametrize("respond", [lambda r: httpx.Response(200, json={"version": "0.3.70"}),
                                     lambda r: httpx.Response(200, json={"version": "0.3.69"}),
                                     lambda r: httpx.Response(503, text="unavailable"),
                                     lambda r: httpx.Response(200, text="<html>not a manifest</html>")])
def test_nothing_is_said_for_the_same_or_an_older_version_or_a_failed_look(monkeypatch, respond, loop):
    _github(monkeypatch, respond)
    check = updates.UpdateCheck()
    run(check.refresh())
    state = check.state()
    assert state["newer"] is False and state["current"] == "0.3.70" and state["progress"] is None


def test_no_look_at_all_when_turned_off(monkeypatch, loop):
    seen = _github(monkeypatch, lambda r: httpx.Response(200, json={"version": "0.3.71"}))
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")
    check = updates.UpdateCheck()
    run(check.refresh())
    run(check.refresh(now=True))
    assert seen == [] and check.state()["latest"] is None and check.state()["checked_at"] == 0


# ------------------------------------------------------------------ the three kinds of copy, and the Release


@pytest.fixture
def copy(tmp_path, monkeypatch):
    """A plugin folder of its own (config.PLUGIN_ROOT), version 0.4.3, from `REPO`; `installed(True)` puts the
    Windows installer's bin\\uvw.exe beside it."""
    root = tmp_path / "JobDesk" / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)

    def write(repository=REPO, version="0.4.3"):
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "job-apply", "version": version, "repository": repository}), encoding="utf-8")

    def installed(yes=True):
        uvw = root.parent / "bin" / "uvw.exe"
        if yes:
            uvw.parent.mkdir(parents=True, exist_ok=True)
            uvw.write_bytes(b"")
        elif uvw.exists():
            uvw.unlink()

    write()
    monkeypatch.setattr(config, "PLUGIN_ROOT", root)
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "0")
    monkeypatch.setattr(updates, "find_claude", lambda: "/found/claude")
    write.installed = installed
    return write


def test_the_kind_is_installed_plugin_or_manual(copy):
    """installed: bin\\uvw.exe beside the plugin's folder; plugin: not that, inside Claude, with a claude command found;
    manual: anything else (run on its own, or no claude command)."""
    copy.installed(True)
    assert updates.UpdateCheck().kind(standalone=True) == "installed"
    assert updates.UpdateCheck().kind(standalone=False) == "installed"
    copy.installed(False)
    assert updates.UpdateCheck().kind(standalone=False) == "plugin"
    assert updates.UpdateCheck().kind(standalone=True) == "manual"
    assert updates.UpdateCheck().state(standalone=False)["kind"] == "plugin"


def _release(tag="job-apply-v0.4.4", names=("JobDeskSetup.exe", "JobDeskSetup.exe.sha256"), **more):
    return {"tag_name": tag, "assets": [{"name": n, "browser_download_url": f"https://evil.example/{n}"}
                                        for n in names], **more}


def _site(release=None, installer=INSTALLER, digest_line=None, seen=None, extra=None):
    """GitHub as the update meets it: the latest Release's answer, and its two files (each sent on to GitHub's storage
    first, as GitHub does). `extra` answers any other address."""
    digest_line = f"{DIGEST}  JobDeskSetup.exe\n" if digest_line is None else digest_line

    def handler(request):
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url == API:
            return httpx.Response(200, json=_release() if release is None else release)
        for name, body in (("JobDeskSetup.exe.sha256", digest_line.encode()), ("JobDeskSetup.exe", installer)):
            if url == DOWNLOAD + name:
                return httpx.Response(302, headers={"location": STORAGE + name})
            if url == STORAGE + name:
                return httpx.Response(200, content=body)
        if extra is not None:
            return extra(request)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_an_installed_copy_reads_the_newest_release_with_both_files(copy, loop):
    copy.installed(True)
    seen = []
    check = updates.UpdateCheck(transport=_site(seen=seen))
    run(check.refresh(standalone=True))
    assert seen == [API]  # (the Release, not plugin.json on main)
    state = check.state(standalone=True)
    assert (state["latest"], state["newer"], state["kind"]) == ("0.4.4", True, "installed") and state["checked_at"] > 0


def test_nothing_personal_goes_with_the_check_or_the_download(copy, loop):
    """Plain downloads of public files: no cookie, no sign-in, nothing but what any client sends."""
    copy.installed(True)
    headers = []
    site = _site()

    def handler(request):
        headers.append({k.lower() for k in request.headers})
        return site.handler(request)

    _installed_update(copy, httpx.MockTransport(handler))
    assert len(headers) == 5
    assert all(h <= {"host", "accept", "accept-encoding", "connection", "user-agent"} for h in headers), headers


@pytest.mark.parametrize("answer", [
    httpx.Response(200, json=_release(names=("JobDeskSetup.exe",))),  # one file only
    httpx.Response(200, json=_release(names=("JobDeskSetup.exe.sha256",))),
    httpx.Response(200, json=_release(tag="v0.4.4")),  # a tag in another form
    httpx.Response(200, json=_release(tag="job-apply-v0.4.4-beta")),
    httpx.Response(200, json=_release(tag="job-apply-v0.4.4/../../x")),
    httpx.Response(200, json=_release(tag="job-apply-v\u0661.4.4")),  # (a digit, but not 0 to 9)
    httpx.Response(200, text="<html>not JSON</html>"),
    httpx.Response(200, json=["not", "a", "release"]),
])
def test_a_release_without_both_files_or_with_another_tag_or_not_json_is_no_update(copy, loop, answer):
    copy.installed(True)
    check = updates.UpdateCheck(transport=httpx.MockTransport(lambda r: answer))
    run(check.refresh(standalone=True))
    state = check.state(standalone=True)
    assert state["latest"] is None and state["newer"] is False and state["checked_at"] == 0


def test_the_page_checks_at_most_once_a_minute_and_by_itself_every_6_hours(copy, loop, monkeypatch):
    copy.installed(True)
    seen = []
    clock = [1_000_000.0]
    monkeypatch.setattr(updates.time, "time", lambda: clock[0])
    check = updates.UpdateCheck(transport=_site(seen=seen))
    run(check.refresh(standalone=True))
    run(check.refresh(standalone=True, now=True))
    clock[0] += 59
    run(check.refresh(standalone=True, now=True))
    assert len(seen) == 1
    clock[0] += 2
    run(check.refresh(standalone=True, now=True))
    run(check.refresh(standalone=True))  # (by itself: 6 hours after the last look)
    assert len(seen) == 2
    clock[0] += updates.CHECK_EVERY
    run(check.refresh(standalone=True))
    assert len(seen) == 3


# ------------------------------------------------------------------ updating an installed copy


@pytest.fixture(autouse=True)
def _settle(monkeypatch):
    """The installer watched for a moment, not 5 seconds, before the desk stops for it."""
    monkeypatch.setattr(updates, "INSTALLER_SETTLE", 0.05, raising=False)


class Process:
    """What Popen gives back: still running (poll None), or ended with `code`."""

    def __init__(self, code=None):
        self.code, self.polled = code, 0

    def poll(self):
        self.polled += 1
        return self.code


class Started:
    """A Popen that starts nothing: what it was handed, in order with the desk's own steps, and the record of the
    version it was for as it was then. Its installer runs on (`ends`: ended at once, with that code)."""

    def __init__(self, steps, ends=None):
        self.steps = steps
        self.calls = []
        self.records = []
        self.process = Process(ends)

    def __call__(self, args, **options):
        self.calls.append((args, options))
        self.steps.append("installer")
        record = config.home() / "updates" / "updated-to"
        self.records.append(record.read_text(encoding="utf-8") if record.exists() else None)
        return self.process


def _installed_update(copy, transport, popen=None, filling=lambda: False, steps=None, pause=None):
    copy.installed(True)
    steps = [] if steps is None else steps
    popen = popen or Started(steps)
    check = updates.UpdateCheck(transport=transport, popen=popen,
                                run=lambda *a, **k: pytest.fail("no command runs for an installed copy"))
    run(check.refresh(standalone=True))

    def paused():
        steps.append("pause")
        return lambda: steps.append("resume")

    async def go():
        await check.update_installed(filling=filling, pause=pause or paused, quit=lambda: steps.append("quit"))

    run(go())
    return check, steps, popen


def _folder():
    return config.home() / "updates"


def test_a_good_download_is_kept_and_the_installer_started_with_its_arguments_after_the_pause_then_the_desk_stops(
        copy, loop):
    _folder().mkdir(parents=True)
    (_folder() / "JobDeskSetup-0.4.2.exe").write_bytes(b"an older one")
    check, steps, popen = _installed_update(copy, _site())
    saved = _folder() / "JobDeskSetup-0.4.4.exe"
    assert saved.read_bytes() == INSTALLER
    assert sorted(p.name for p in _folder().glob("*.exe")) == ["JobDeskSetup-0.4.4.exe"]  # (older ones gone)
    assert steps == ["pause", "installer", "quit"]
    [(args, options)] = popen.calls
    assert args == [str(saved), "/SILENT", "/NOCANCEL", "/CURRENTUSER", "/NORESTART", "/RELAUNCH=1",
                    f"/LOG={_folder() / 'install-0.4.4.log'}"]
    assert not options.get("shell") and "executable" not in options
    if sys.platform == "win32":
        assert options["creationflags"] == (window.DETACHED_PROCESS | window.CREATE_NEW_PROCESS_GROUP
                                            | window.CREATE_BREAKAWAY_FROM_JOB)
    else:
        assert options["start_new_session"] is True
    assert all(options[s] is subprocess.DEVNULL for s in ("stdin", "stdout", "stderr"))
    assert check.state(standalone=True)["progress"] == "installing"
    # the version it's for recorded before it started (the installer may stop this desk at once), and kept
    assert popen.records == ["0.4.4"] and (_folder() / "updated-to").read_text(encoding="utf-8") == "0.4.4"
    assert popen.process.polled >= 2  # (watched until INSTALLER_SETTLE was up, then the desk stopped)


@pytest.mark.parametrize("digest_line", [f"{'0' * 64}  JobDeskSetup.exe\n", "not a checksum\n",
                                         f"{DIGEST.upper()}  JobDeskSetup.exe\n", ""])
def test_a_file_whose_sha256_differs_is_deleted_and_nothing_starts(copy, loop, digest_line):
    check, steps, popen = _installed_update(copy, _site(digest_line=digest_line))
    assert steps == [] and popen.calls == []
    assert not list(_folder().glob("JobDeskSetup*"))
    state = check.state(standalone=True)
    assert state["progress"] == "failed" and state["error"]
    if digest_line.startswith("0"):
        assert state["error"] == "The download didn't arrive whole."


@pytest.mark.parametrize("digest_line", [f"{DIGEST}  JobDeskSetup.exe trailing junk",
                                         f"{DIGEST}  JobDeskSetup.exe\nmore",
                                         f"{DIGEST}  JobDeskSetup.exe.part\n", f"{DIGEST}\n", f"{DIGEST}  other.exe\n",
                                         f"{DIGEST} JobDeskSetup.exe\n"])
def test_a_checksum_file_with_the_right_sha256_in_any_other_line_is_refused(copy, loop, digest_line):
    """The checksum file is the workflow's one line exactly (the SHA-256, two spaces, JobDeskSetup.exe, a newline or
    none): the right SHA-256 with anything after it, before another name, or alone, is no checksum of this file."""
    check, steps, popen = _installed_update(copy, _site(digest_line=digest_line))
    assert steps == [] and popen.calls == []
    assert check.state(standalone=True)["error"] == "The download didn't arrive whole."


def test_a_cut_off_download_is_deleted_and_says_so(copy, loop):
    check, steps, popen = _installed_update(copy, _site(installer=INSTALLER[:-10]))
    assert steps == [] and popen.calls == [] and not any(_folder().glob("JobDeskSetup*"))
    assert check.state(standalone=True)["error"] == "The download didn't arrive whole."


class Chunks(httpx.AsyncByteStream):
    """A body sent in pieces with no length said ahead: what was read of it."""

    def __init__(self, pieces):
        self.pieces, self.read = pieces, 0

    async def __aiter__(self):
        for piece in self.pieces:
            self.read += 1
            yield piece


def test_a_file_over_the_limit_is_refused_while_it_streams_and_from_its_header(copy, loop, monkeypatch):
    monkeypatch.setattr(updates, "MAX_DOWNLOAD", 1000)
    body = Chunks([b"x" * 400] * 100)
    big = _site(extra=None)

    def handler(request):
        if str(request.url) == STORAGE + "JobDeskSetup.exe":
            return httpx.Response(200, stream=body)
        return big.handler(request)

    check, steps, popen = _installed_update(copy, httpx.MockTransport(handler))
    assert body.read < 10  # (stopped once past the limit, not read to the end)
    assert steps == [] and popen.calls == [] and not any(_folder().glob("JobDeskSetup*"))
    assert check.state(standalone=True)["progress"] == "failed"

    said = httpx.Response(200, headers={"content-length": str(updates.MAX_DOWNLOAD + 1)}, content=b"x")

    def header(request):
        return said if str(request.url) == STORAGE + "JobDeskSetup.exe" else big.handler(request)

    check, steps, popen = _installed_update(copy, httpx.MockTransport(header))
    assert steps == [] and popen.calls == [] and check.state(standalone=True)["progress"] == "failed"


def test_a_file_said_to_be_over_the_limit_is_refused_before_a_byte_of_it_is_read(copy, loop):
    """The installer's own Content-Length over MAX_DOWNLOAD refuses it before its body is read: here the body itself is
    the right installer, whole and well under the limit, so only the header can refuse it."""
    body = Chunks([INSTALLER[:500], INSTALLER[500:]])
    site = _site()

    def handler(request):
        if str(request.url) == STORAGE + "JobDeskSetup.exe":
            return httpx.Response(200, headers={"content-length": str(updates.MAX_DOWNLOAD + 1)}, stream=body)
        return site.handler(request)

    check, steps, popen = _installed_update(copy, httpx.MockTransport(handler))
    assert body.read == 0 and steps == [] and popen.calls == [] and not any(_folder().glob("JobDeskSetup*"))
    state = check.state(standalone=True)
    assert (state["progress"], state["error"]) == ("failed", updates.NOT_DOWNLOADED)


@pytest.mark.parametrize("where", ["http://objects.githubusercontent.com/JobDeskSetup.exe",
                                   "http://evil.example/JobDeskSetup.exe",
                                   "ftp://objects.githubusercontent.com/JobDeskSetup.exe"])
def test_a_redirect_to_anything_but_https_is_refused(copy, loop, where):
    seen = []
    site = _site(seen=seen)

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == DOWNLOAD + "JobDeskSetup.exe":
            return httpx.Response(302, headers={"location": where})
        return site.handler(request)

    check, steps, popen = _installed_update(copy, httpx.MockTransport(handler))
    assert where not in seen and steps == [] and popen.calls == []
    assert check.state(standalone=True)["progress"] == "failed"


def test_the_download_comes_from_the_installed_plugin_json_whatever_the_release_names(copy, loop):
    """The Release's answer names other addresses (its assets', its zipball, its page): none is ever read. The files
    come from the repository in the installed plugin.json, with the tag built again from the version alone."""
    seen = []
    release = _release(html_url="https://evil.example/page", zipball_url="https://evil.example/zip",
                       upload_url="https://evil.example/upload", url="https://evil.example/api",
                       repository="https://github.com/someone/Else")
    check, steps, popen = _installed_update(copy, _site(release=release, seen=seen))
    assert seen == [API, DOWNLOAD + "JobDeskSetup.exe.sha256", STORAGE + "JobDeskSetup.exe.sha256",
                    DOWNLOAD + "JobDeskSetup.exe", STORAGE + "JobDeskSetup.exe"]
    assert steps == ["pause", "installer", "quit"]

    # a fork's own plugin.json: its own Releases, and nothing of this one's
    copy(repository="https://github.com/someone/Fork")
    fork = []

    def forked(request):
        fork.append(str(request.url))
        url = str(request.url).replace("someone/Fork", "sebob2491/Jobs")
        return _site().handler(httpx.Request("GET", url))

    _installed_update(copy, httpx.MockTransport(forked))
    assert fork[0] == "https://api.github.com/repos/someone/Fork/releases/latest"
    assert fork[1] == "https://github.com/someone/Fork/releases/download/job-apply-v0.4.4/JobDeskSetup.exe.sha256"


@pytest.mark.parametrize("found", ["0.4.3", "0.4.2"])
def test_nothing_is_downloaded_or_started_when_the_version_found_isnt_later(copy, loop, found):
    seen = []
    check, steps, popen = _installed_update(copy, _site(release=_release(tag=f"job-apply-v{found}"), seen=seen))
    assert seen == [API] and steps == [] and popen.calls == []
    assert check.state(standalone=True)["progress"] == "failed"


def test_a_copy_updated_meanwhile_is_checked_again_before_the_installer_starts(copy, loop):
    """Step 4: the version is checked once more, after the download, against this copy's version as it is then."""
    def handler(request):
        if str(request.url) == STORAGE + "JobDeskSetup.exe":
            copy(version="0.4.4")  # (updated some other way while it downloaded)
        return _site().handler(request)

    check, steps, popen = _installed_update(copy, httpx.MockTransport(handler))
    assert steps == [] and popen.calls == [] and check.state(standalone=True)["progress"] == "failed"


def test_an_installer_that_wont_start_undoes_the_pause_and_the_desk_carries_on(copy, loop):
    def refuses(args, **options):
        raise OSError("can't start")

    check, steps, _ = _installed_update(copy, _site(), popen=refuses)
    assert steps == ["pause", "resume"]
    state = check.state(standalone=True)
    assert state["progress"] == "failed" and state["error"] == \
        "The installer couldn't be started. Wait a minute, then press Update now to try again."
    assert not (_folder() / "updated-to").exists()


@pytest.mark.parametrize("code", [0, 1, 2])
def test_an_installer_that_ended_at_once_never_stops_the_desk(copy, loop, code):
    """The desk stops for the installer only once it has taken over: one that has already ended (closed, or it ran
    into something at once) never began, so the queue is resumed, the record of it goes, and the desk carries on."""
    steps = []
    popen = Started(steps, ends=code)
    check, steps, _ = _installed_update(copy, _site(), popen=popen, steps=steps)
    assert steps == ["pause", "installer", "resume"]  # (no "quit")
    state = check.state(standalone=True)
    assert (state["progress"], state["error"]) == (
        "failed", "The installer stopped as soon as it started. Press Update now to try again.")
    assert not (_folder() / "updated-to").exists()
    assert (_folder() / "JobDeskSetup-0.4.4.exe").read_bytes() == INSTALLER  # (kept: it matched)


def test_an_installer_watched_until_it_has_taken_over_then_the_desk_stops(copy, loop, monkeypatch):
    """Still running INSTALLER_SETTLE seconds after it started: it has taken over, and only then the desk stops."""
    monkeypatch.setattr(updates, "INSTALLER_SETTLE", 0.6)
    steps = []
    popen = Started(steps)
    began = time.monotonic()
    _installed_update(copy, _site(), popen=popen, steps=steps)
    assert time.monotonic() - began >= 0.6 and steps == ["pause", "installer", "quit"]

    # one that ends while it's watched never began: the desk carries on
    class EndsSoon(Process):
        def poll(self):
            self.polled += 1
            return None if self.polled < 3 else 1

    steps = []
    late = Started(steps)
    late.process = EndsSoon()
    check, steps, _ = _installed_update(copy, _site(), popen=late, steps=steps)
    assert steps == ["pause", "installer", "resume"] and check.state(standalone=True)["progress"] == "failed"


def _starter(calls, refuse_breakaway=False, refuse=None):
    """A Popen that starts nothing: the flags it was handed; Windows' "access denied" for a start out of the job
    (`refuse_breakaway`), or `refuse` for any start."""
    def popen(args, **options):
        calls.append(options.get("creationflags", options.get("start_new_session")))
        if refuse is not None:
            raise refuse
        if refuse_breakaway and options["creationflags"] & window.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied")
        return Process()
    return popen


def test_the_installer_starts_out_of_the_desks_job_or_not_at_all(monkeypatch, tmp_path):
    """On Windows the installer is started out of the job the desk's process may be in (uv's launcher holds what it
    starts in one, and stop-desk.ps1 stops that process): CREATE_BREAKAWAY_FROM_JOB. A job that forbids it refuses
    with access denied: then nothing is started (inside the job, the desk's stopping could take the installer with it
    part-way), and BreakawayRefused says so. Elsewhere: a session of its own."""
    calls = []
    log, exe = tmp_path / "install.log", tmp_path / "JobDeskSetup-0.4.4.exe"
    detached = window.DETACHED_PROCESS | window.CREATE_NEW_PROCESS_GROUP
    monkeypatch.setattr(window, "_system", lambda: "windows")
    assert isinstance(updates.start_installer(exe, log, _starter(calls)), Process)
    assert calls == [detached | window.CREATE_BREAKAWAY_FROM_JOB] and window.CREATE_BREAKAWAY_FROM_JOB == 0x01000000
    calls.clear()
    with pytest.raises(window.BreakawayRefused):
        updates.start_installer(exe, log, _starter(calls, refuse_breakaway=True))
    assert calls == [detached | window.CREATE_BREAKAWAY_FROM_JOB]  # (never again inside the job)
    calls.clear()
    with pytest.raises(FileNotFoundError):  # (anything else is no job's refusal: not tried again)
        updates.start_installer(exe, log, _starter(calls, refuse=FileNotFoundError(2, "not there")))
    assert calls == [detached | window.CREATE_BREAKAWAY_FROM_JOB]
    calls.clear()
    monkeypatch.setattr(window, "_system", lambda: "linux")
    assert isinstance(updates.start_installer(exe, log, _starter(calls)), Process)  # (no such job here)
    assert calls == [True]


def test_start_detached_still_falls_back_inside_the_job_for_its_other_callers(monkeypatch):
    """start_detached's own fallback (started inside the job when Windows refuses out of it) stays as it was for any
    caller but the update's installer."""
    calls = []
    monkeypatch.setattr(window, "_system", lambda: "windows")
    detached = window.DETACHED_PROCESS | window.CREATE_NEW_PROCESS_GROUP
    process, out = window.start_detached(["x.exe"], popen=_starter(calls, refuse_breakaway=True))
    assert isinstance(process, Process) and out is False
    assert calls == [detached | window.CREATE_BREAKAWAY_FROM_JOB, detached]


def test_an_installer_windows_wont_start_outside_the_desks_job_fails_the_update_and_the_desk_stays(
        copy, loop, monkeypatch):
    """For the update there's no fallback: Windows refusing to start the installer outside the desk's job fails it with
    its own sentence, nothing was started, the queue is resumed as after any failed start, and the desk stays."""
    monkeypatch.setattr(window, "_system", lambda: "windows")
    calls = []
    check, steps, _ = _installed_update(copy, _site(), popen=_starter(calls, refuse_breakaway=True))
    assert len(calls) == 1 and steps == ["pause", "resume"]  # (no "quit")
    state = check.state(standalone=True)
    assert (state["progress"], state["error"]) == ("failed", updates.NO_BREAKAWAY)
    assert updates.NO_BREAKAWAY == ("This computer wouldn't let the desk start the installer by itself. Press What's "
                                    "new, download the installer there, and run it.")
    assert not (_folder() / "updated-to").exists()
    assert (_folder() / "JobDeskSetup-0.4.4.exe").read_bytes() == INSTALLER  # (kept, for Update now again)


def test_the_installer_is_handed_the_desks_environment_as_it_is(copy, loop, monkeypatch):
    """The installer opens the desk again and hands it its own environment: a password or key set there (made up here)
    reaches the reopened desk, as it reaches one started from the Start menu (which has what a double-clicked installer
    gets). So the installer is started with the desk's environment as it is: none given (Popen hands on the process's
    own), or one holding all of it."""
    made_up = {"JOB_APPLY_SECRET_WORKDAY": "made-up-1", "ANTHROPIC_API_KEY": "made-up-2",
               "JOB_DESK_MADE_UP_SETTING": "made-up-3"}
    for k, v in made_up.items():
        monkeypatch.setenv(k, v)
    check, steps, popen = _installed_update(copy, _site())
    assert steps == ["pause", "installer", "quit"]
    [(_, options)] = popen.calls
    handed = os.environ if options.get("env") is None else options["env"]
    assert {k: handed.get(k) for k in made_up} == made_up


def test_a_queue_paused_before_the_update_stays_paused_when_the_installer_ends_at_once(srv, copy):
    copy.installed(True)
    steps = []
    check = updates.UpdateCheck(transport=_site(), popen=Started(steps, ends=1))
    check.latest = "0.4.4"
    desk = _desk(srv, check)
    desk.standalone = True

    async def go(paused):
        desk.applier.paused = paused
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                answer = await c.post("/api/update/start", headers={"x-desk-token": desk.token}, json={})
                assert answer.status_code == 200
                for _ in range(200):
                    if check.progress == "failed":
                        break
                    await asyncio.sleep(0.02)
                return check.progress, desk.applier.paused, desk.quitting.is_set()
        finally:
            await desk.stop()

    assert run(go(True)) == ("failed", True, False)
    check.progress = None
    assert run(go(False)) == ("failed", False, False)


# ------------------------------------------------------------------ the applier held while an update quits the desk


def _held_desk(srv):
    """A desk whose applier drives nothing (its worker not started, a pass's task a stand-in), with a job ready to
    submit, one waiting on the person's sign-in, and one on a questions card."""
    from job_apply.pipeline import Run

    desk = _desk(srv, updates.UpdateCheck())
    began = []

    async def no_pass(kind, run):
        began.append((kind, run.job_id))

    desk.applier._run_task = no_pass
    jobs = [srv.add_job(url=f"https://example.com/{n}", title="FSE", company=f"Example {n}")["job"]["id"]
            for n in ("a", "b", "c")]
    a = desk.applier
    a.runs[jobs[0]] = Run(jobs[0], "FSE", "Example a", status="ready")
    a.runs[jobs[1]] = Run(jobs[1], "FSE", "Example b", status="needs_you", need="sign_in")
    a.runs[jobs[2]] = Run(jobs[2], "FSE", "Example c", status="needs_you", need="questions",
                          questions=[{"label": "Years of experience", "required": True}])
    return desk, jobs, began


def test_while_an_update_holds_the_applier_a_submit_or_a_pressed_job_is_refused_and_nothing_starts(
        srv, copy, monkeypatch):
    """From the moment an installed copy's update pauses the queue until the desk quits, nothing starts: a Submit, a
    job's Carry on, its answers, Go through them (a round), Try now each answer 400 with the sentence, nothing is
    queued or sent, and the queue's worker begins no pass, not even one pressed before the hold."""
    from job_apply import pipeline

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.01)
    desk, (ready, signing_in, asking), began = _held_desk(srv)
    a = desk.applier
    said = "The Job Desk is updating. Try again when it is back."

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                a.resume(signing_in)  # (pressed before the update: queued, and it would go while paused)
                desk._pause_for_update()
                answers = []
                for path, body in ((f"/api/job/{ready}/submit", {}), (f"/api/job/{signing_in}/resume", {}),
                                   (f"/api/job/{ready}/round", {}), (f"/api/job/{signing_in}/retry", {}),
                                   ("/api/answer", {"job_id": asking, "answers": [
                                       {"label": "Years of experience", "value": "3"}]})):
                    answer = await c.post(path, headers=h, json=body)
                    answers.append((answer.status_code, answer.json()))
                await a._tick()
                return answers
        finally:
            await desk.stop()

    answers = run(go())
    assert answers == [(400, {"error": said})] * 5
    assert pipeline.UPDATING == said
    assert a.runs[ready].status == "ready" and not a.runs[ready].submit_pressed
    assert a.runs[asking].status == "needs_you" and a.round_job is None
    assert not any(t[0] == "submit" for t in a.tasks) and began == [] and not a._slots
    assert config.saved_answers() == []


class _Tab:
    """A job's tab that's never a browser's: open, and nothing done to it."""

    def is_closed(self):
        return False

    async def bring_to_front(self):  # (brought up: noted by the test that hands it in)
        raise AssertionError("brought to the front during the update's hold")


@pytest.mark.parametrize("need", ["sign_in", "email_code"])
def test_a_tick_whose_look_the_update_lands_in_starts_nothing_and_types_nothing(srv, copy, monkeypatch, need):
    """The update's pause may land while a tick that began before it awaits a tab or the inbox: a job left waiting on
    the person, past its sign-in by the look the hold landed in, is neither carried on nor begun, and an emailed code
    found by the look the hold landed in is never typed in."""
    from job_apply import pipeline
    from job_apply.mailbox import Found

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.01)
    desk, (_, left, _), began = _held_desk(srv)
    a = desk.applier
    run_ = a.runs[left]
    run_.left, run_.need, run_.page = True, need, _Tab()
    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")
    typed = []

    async def past_pause(r):  # (the look the update's pause lands in)
        desk._pause_for_update()
        return True

    async def check_mail(r):  # (the inbox read the update's pause lands in, then the code it found)
        desk._pause_for_update()
        await a._use_mail(r, Found("code", "123456", "example.com", time.time()), r.paused_at)

    async def enter_code(r, found):
        typed.append(found.value)
        return True

    monkeypatch.setattr(a, "_past_pause", past_pause)
    monkeypatch.setattr(a, "_check_mail", check_mail)
    monkeypatch.setattr(a, "_enter_code", enter_code)

    async def go():
        await a._tick()

    run(go())
    assert a.updating and began == [] and typed == [] and not a._slots
    assert not a.tasks and left not in a.pressed and run_.status == "needs_you"


def test_a_submit_waiting_its_turn_counts_as_being_filled(srv, copy):
    """A Submit the person pressed, still waiting in the applier's tasks (or a job they pressed on, which goes even
    while the queue is paused), is a job about to be filled: /api/update/start answers its 409 until it has gone."""
    copy.installed(True)
    check = updates.UpdateCheck(transport=_site(), popen=lambda *a, **k: pytest.fail("nothing starts"))
    check.latest = "0.4.4"
    desk, (ready, signing_in, _), began = _held_desk(srv)
    desk.updates = check
    desk.standalone = True

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            said = []
            async with _client(desk) as c:
                desk.applier.submit_now(ready)  # (pressed: waiting its turn, not begun)
                answer = await c.post("/api/update/start", headers=h, json={})
                said.append((answer.status_code, answer.json()))
                desk.applier.tasks.clear()
                desk.applier.resume(signing_in)  # (Carry on pressed: goes even while paused)
                answer = await c.post("/api/update/start", headers=h, json={})
                said.append((answer.status_code, answer.json()))
            return said
        finally:
            await desk.stop()

    assert run(go()) == [(409, {"error": updates.FILLING})] * 2
    assert began == [] and check.progress is None


def test_a_failed_update_lets_go_of_the_applier(srv, copy):
    """The hold is set with the update's pause, before the installer starts, and let go with it when the update fails
    (here the installer ended at once): the person's Submit goes again."""
    copy.installed(True)
    held_then = []

    def popen(args, **options):
        held_then.append(desk.applier.updating)
        return Process(1)

    check = updates.UpdateCheck(transport=_site(), popen=popen)
    check.latest = "0.4.4"
    desk, (ready, _, _), began = _held_desk(srv)
    desk.updates = check
    desk.standalone = True

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                assert (await c.post("/api/update/start", headers=h, json={})).status_code == 200
                for _ in range(300):
                    if check.progress == "failed":
                        break
                    await asyncio.sleep(0.02)
                return (await c.post(f"/api/job/{ready}/submit", headers=h, json={})).status_code
        finally:
            await desk.stop()

    assert run(go()) == 200
    assert held_then == [True] and desk.applier.updating is False and check.progress == "failed"
    assert ("submit", ready) in desk.applier.tasks


def test_a_job_begun_but_not_yet_at_its_first_step_counts_as_being_filled(srv, copy):
    """The queue makes a pass's task (the applier's _slots) before its run is running or its step holds the browser:
    from then on a job is about to be filled, and an update must wait."""
    desk = _desk(srv, updates.UpdateCheck())
    a = desk.applier
    assert not desk._filling()
    a._slots[5] = object()  # (a pass begun, not yet at its first step)
    try:
        assert a.current is None and not any(r.status == "running" for r in a.runs.values())
        assert desk._filling()
    finally:
        a._slots.clear()


# ------------------------------------------------------------------ what a failure says, and a download kept


def test_a_network_failure_says_the_desk_needs_the_internet(copy, loop):
    def offline(request):
        if str(request.url) == API:
            return _site().handler(request)
        raise httpx.ConnectError("no route", request=request)

    check, steps, popen = _installed_update(copy, httpx.MockTransport(offline))
    assert steps == [] and popen.calls == []
    assert check.state(standalone=True)["error"] == \
        "The new version couldn't be downloaded. The desk needs the internet for that."


SAVE = ("The new version couldn't be saved on this computer. Make some room on the disk, or wait a minute, then press "
        "Update now to try again.")


def test_a_failure_to_save_says_so_not_the_internet(copy, loop, monkeypatch):
    # the folder can't be made (a file where it goes)
    _folder().parent.mkdir(parents=True, exist_ok=True)
    _folder().write_text("in the way", encoding="utf-8")
    check, steps, popen = _installed_update(copy, _site())
    assert steps == [] and popen.calls == [] and check.state(standalone=True)["error"] == SAVE
    _folder().unlink()

    # a disk that's full as it writes
    class Full:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def write(self, piece):
            raise OSError(28, "No space left on device")

    real_open = updates.Path.open

    def full_open(self, mode="r", *a, **k):
        f = real_open(self, mode, *a, **k)
        return Full(f) if self.name.endswith(".part") else f

    monkeypatch.setattr(updates.Path, "open", full_open)
    check, steps, popen = _installed_update(copy, _site())
    assert steps == [] and popen.calls == [] and check.state(standalone=True)["error"] == SAVE
    assert not any(_folder().glob("JobDeskSetup*"))
    monkeypatch.setattr(updates.Path, "open", real_open)

    # the whole file can't take its name (another program, an antivirus, holds it)
    def held(a, b):
        raise PermissionError(13, "in use by another process")

    monkeypatch.setattr(updates.os, "replace", held)
    check, steps, popen = _installed_update(copy, _site())
    assert steps == [] and popen.calls == [] and check.state(standalone=True)["error"] == SAVE


def test_an_older_installer_that_cant_be_deleted_never_stops_the_update(copy, loop, monkeypatch):
    _folder().mkdir(parents=True)
    older = _folder() / "JobDeskSetup-0.4.2.exe"
    older.write_bytes(b"an older one, held by another program")
    real_unlink = updates.Path.unlink

    def unlink(self, missing_ok=False):
        if self == older:
            raise PermissionError(13, "in use by another process")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(updates.Path, "unlink", unlink)
    check, steps, _ = _installed_update(copy, _site())
    assert steps == ["pause", "installer", "quit"] and older.exists()
    assert check.state(standalone=True)["progress"] == "installing"


def test_an_installer_that_matched_is_used_again_without_downloading_it(copy, loop):
    _folder().mkdir(parents=True)
    (_folder() / "JobDeskSetup-0.4.4.exe").write_bytes(INSTALLER)
    seen = []
    check, steps, popen = _installed_update(copy, _site(seen=seen))
    assert seen == [API, DOWNLOAD + "JobDeskSetup.exe.sha256", STORAGE + "JobDeskSetup.exe.sha256"]
    assert steps == ["pause", "installer", "quit"]
    assert popen.calls[0][0][0] == str(_folder() / "JobDeskSetup-0.4.4.exe")


def test_an_installer_kept_that_doesnt_match_is_deleted_and_downloaded_again(copy, loop):
    _folder().mkdir(parents=True)
    (_folder() / "JobDeskSetup-0.4.4.exe").write_bytes(INSTALLER[:-5] + b"bent!")
    seen = []
    check, steps, popen = _installed_update(copy, _site(seen=seen))
    assert DOWNLOAD + "JobDeskSetup.exe" in seen and STORAGE + "JobDeskSetup.exe" in seen
    assert (_folder() / "JobDeskSetup-0.4.4.exe").read_bytes() == INSTALLER
    assert steps == ["pause", "installer", "quit"]


def test_a_job_begun_during_the_download_stops_the_update_and_keeps_the_download(copy, loop):
    """The second look at the jobs being filled, just before the queue is paused: a job begun while it downloaded
    stops it (no pause, nothing started), and the download that matched is kept for the next try."""
    seen = []
    check, steps, popen = _installed_update(copy, _site(seen=seen), filling=lambda: True)
    assert steps == [] and popen.calls == []
    state = check.state(standalone=True)
    assert (state["progress"], state["error"]) == (
        "failed", "Jobs are being filled. Wait for them to finish, or pause the queue.")
    assert (_folder() / "JobDeskSetup-0.4.4.exe").read_bytes() == INSTALLER
    # the next try: no second download
    seen.clear()
    check, steps, popen = _installed_update(copy, _site(seen=seen))
    assert STORAGE + "JobDeskSetup.exe" not in seen and steps == ["pause", "installer", "quit"]


def test_an_unexpected_error_never_leaves_an_update_under_way(copy, loop):
    """Something no step expects (a bug, say) still ends the update: failed, with a sentence, never "under way" (the
    page would wait for it for ever, and the Update button would answer "already under way")."""
    def pause():
        raise RuntimeError("something unexpected")

    check, steps, popen = _installed_update(copy, _site(), pause=pause)
    state = check.state(standalone=True)
    assert not check.under_way() and popen.calls == []
    assert (state["progress"], state["error"]) == (
        "failed", "The update stopped before it began. Press Update now to try again.")

    def strange(args, **options):
        raise RuntimeError("something unexpected")

    plugin = updates.UpdateCheck(run=strange)
    run(plugin.update_plugin())
    assert not plugin.under_way() and plugin.state()["error"] == "Claude couldn't update the plugin."


def test_redirects_are_followed_here_alone_and_tls_is_never_turned_off(copy, loop, monkeypatch):
    """Every client the check and the update make follows no redirect by itself (each is looked at here: https only),
    and checks TLS as httpx does unless told not to."""
    made = []
    real = httpx.AsyncClient

    def client(**options):
        made.append(dict(options))
        return real(**options)

    monkeypatch.setattr(updates.httpx, "AsyncClient", client)
    _installed_update(copy, _site())
    assert len(made) >= 2
    for options in made:
        assert options.get("follow_redirects") is False, options
        assert options.get("verify", True) is True and "trust_env" not in options, options
        assert set(options) <= {"timeout", "follow_redirects", "transport"}, options


def test_an_update_that_didnt_finish_is_said_once_when_the_desk_starts_again(srv, copy, monkeypatch):
    """The installer was cancelled, or failed and rolled back, and opened the version before again: the record of the
    update it was started for, still later than this copy, says so once, as the update's failure."""
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")
    _folder().mkdir(parents=True)
    (_folder() / "updated-to").write_text("0.4.4", encoding="utf-8")

    async def update_of(desk):
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return (await c.get("/api/state", headers={"x-desk-token": desk.token})).json()
        finally:
            await desk.stop()

    state = run(update_of(_desk(srv, updates.UpdateCheck())))
    assert (state["update"]["progress"], state["update"]["error"]) == (
        "failed", "The update to version 0.4.4 didn't finish. Press Update now to try again.")
    assert "0.4.4" not in (state["restored"] or "")
    assert not (_folder() / "updated-to").exists()
    again = run(update_of(_desk(srv, updates.UpdateCheck())))
    assert (again["update"]["progress"], again["update"]["error"]) == (None, None)

    # a record of an older one, or of no version, says nothing
    for said in ("0.4.2", "not a version"):
        (_folder() / "updated-to").write_text(said, encoding="utf-8")
        check = updates.UpdateCheck()
        check.note_updated()
        assert (check.progress, check.updated_to) == (None, None)


# ------------------------------------------------------------------ updating a plugin copy


class Commands:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, args, **options):
        self.calls.append((args, options))
        return subprocess.CompletedProcess(args, self.codes.pop(0), stdout="said", stderr="")


def test_a_plugin_copy_runs_the_two_commands_with_the_found_claude_without_a_shell(copy, loop):
    commands = Commands([0, 0])
    check = updates.UpdateCheck(run=commands, popen=lambda *a, **k: pytest.fail("nothing is started"))
    check.latest = "0.4.4"
    run(check.update_plugin())
    assert [args for args, _ in commands.calls] == [
        ["/found/claude", "plugin", "marketplace", "update", "sebob-jobs"],
        ["/found/claude", "plugin", "update", "job-apply@sebob-jobs"]]
    for _, options in commands.calls:
        assert not options.get("shell") and options["timeout"] == 120
    state = check.state()
    assert (state["kind"], state["progress"], state["error"]) == ("plugin", "done", None)


@pytest.mark.parametrize("codes, ran", [([1], 1), ([0, 2], 2)])
def test_a_failing_command_gives_failed_and_the_commands_to_run(copy, loop, codes, ran):
    commands = Commands(codes)
    check = updates.UpdateCheck(run=commands)
    run(check.update_plugin())
    state = check.state()
    assert len(commands.calls) == ran
    assert (state["progress"], state["error"]) == ("failed", "Claude couldn't update the plugin.")
    assert state["commands"] == updates.UPDATE_COMMANDS


def test_a_command_that_runs_too_long_or_wont_start_gives_failed(copy, loop):
    def slow(args, **options):
        raise subprocess.TimeoutExpired(args, options["timeout"])

    check = updates.UpdateCheck(run=slow)
    run(check.update_plugin())
    assert check.state()["progress"] == "failed"


def test_no_claude_command_makes_the_kind_manual(copy, monkeypatch):
    monkeypatch.setattr(updates, "find_claude", lambda: None)
    assert updates.UpdateCheck().kind(standalone=False) == "manual"


# ------------------------------------------------------------------ the two routes


def _client(desk):
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False)


def _desk(srv, check):
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.updates = check
    return desk


def test_api_update_start_refuses_in_the_four_cases_with_its_sentence(srv, copy, monkeypatch):
    copy.installed(True)
    check = updates.UpdateCheck(transport=_site(), popen=lambda *a, **k: pytest.fail("nothing starts"),
                                run=lambda *a, **k: pytest.fail("nothing runs"))
    check._tried_at = time.time() - 120  # (looked 2 minutes ago: not by itself again, but the page may ask)
    desk = _desk(srv, check)
    desk.standalone = True

    async def go():
        await desk.start(port=0, open_browser=False)
        said = []
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                async def start():
                    answer = await c.post("/api/update/start", headers=h, json={})
                    said.append((answer.status_code, answer.json()))

                assert (await c.post("/api/update/start", json={})).status_code == 403
                assert (await c.post("/api/update/check", json={})).status_code == 403
                await start()  # nothing newer (not looked yet)
                checked = await c.post("/api/update/check", headers=h, json={})
                assert checked.status_code == 200 and checked.json()["newer"] is True
                assert checked.json()["kind"] == "installed"
                desk.applier.current = 7  # a job being filled
                await start()
                desk.applier.current = None
                check.progress = "downloading"  # an update under way
                await start()
                check.progress = None
                copy.installed(False)
                desk.updates = updates.UpdateCheck(transport=_site())
                desk.updates.latest = "0.4.4"
                await start()  # manual: run on its own, from the source
        finally:
            await desk.stop()
        return said

    assert run(go()) == [
        (409, {"error": "There's no newer version to update to."}),
        (409, {"error": "Jobs are being filled. Wait for them to finish, or pause the queue."}),
        (409, {"error": "An update is already under way."}),
        (409, {"error": "This copy isn't one the desk can update by itself."})]


def test_api_update_start_takes_nothing_from_the_page_pauses_the_queue_starts_the_installer_and_quits(srv, copy):
    """What the page sends is never read: an address, a path or a version in it changes nothing."""
    copy.installed(True)
    steps = []
    popen = Started(steps)
    check = updates.UpdateCheck(transport=_site(), popen=popen)
    check.latest = "0.4.4"
    desk = _desk(srv, check)
    desk.standalone = True

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                answer = await c.post("/api/update/start", headers=h,
                                      json={"url": "https://evil.example/x.exe", "path": "C:/x.exe",
                                            "version": "9.9.9"})
                assert (answer.status_code, answer.json()) == (200, {"started": True})
                for _ in range(200):
                    if desk.quitting.is_set():
                        break
                    await asyncio.sleep(0.02)
                state = (await c.get("/api/state", headers=h)).json()["update"]
                return state, desk.applier.paused, desk.quitting.is_set()
        finally:
            await desk.stop()

    state, paused, quitting = run(go())
    assert paused and quitting and state["progress"] == "installing"
    [(args, _)] = popen.calls
    assert args[0] == str(config.home() / "updates" / "JobDeskSetup-0.4.4.exe")


def test_the_desk_says_once_that_it_was_updated_with_the_queue_back(srv, copy, monkeypatch):
    """After an installed copy updated and reopened, the notice about the queue that came back paused says so."""
    copy(version="0.4.4")
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")
    (config.home() / "updates").mkdir(parents=True)
    (config.home() / "updates" / "updated-to").write_text("0.4.4", encoding="utf-8")

    async def state_of(desk):
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return (await c.get("/api/state", headers={"x-desk-token": desk.token})).json()["restored"]
        finally:
            await desk.stop()

    desk = _desk(srv, updates.UpdateCheck())
    monkeypatch.setattr(desk.applier, "restored_notice", lambda: "2 jobs from before the desk restarted are back.")
    assert run(state_of(desk)) == "The Job Desk was updated to 0.4.4. 2 jobs from before the desk restarted are back."
    again = _desk(srv, updates.UpdateCheck())
    monkeypatch.setattr(again.applier, "restored_notice", lambda: "2 jobs from before the desk restarted are back.")
    assert run(state_of(again)) == "2 jobs from before the desk restarted are back."


@pytest.mark.skipif(not browser_available(), reason="no browser")
def test_the_desk_page_says_a_newer_version_is_out(srv, monkeypatch):
    monkeypatch.setattr(config, "plugin_version", lambda: "0.3.70")
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.updates.latest = "0.3.71"

    async def go(latest):
        desk.updates.latest = latest
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_function("() => document.querySelector('#notices') !== null")
                await page.wait_for_timeout(1500)
                text = await page.inner_text("#notices")
                await browser.close()
                return text
        finally:
            await desk.stop()

    text = run(go("0.3.71"))
    assert "Version 0.3.71 is ready (you have 0.3.70). Update" in text, text
    # (F2: the notice names no command, and its link leads to the update card: test_desk_update_page.py)
    assert "claude plugin marketplace update sebob-jobs" not in text and "claude plugin update" not in text
    for same in ("0.3.70", None):  # (the state's update is always there now: the notice only when newer)
        assert "is ready" not in run(go(same))



@pytest.mark.parametrize("latest, current, notes", [
    ("0.3.71", "0.3.70", "https://github.com/sebob2491/Jobs/releases/tag/job-apply-v0.3.71"),
    ("0.3.70", "0.3.70", None),  # nothing newer: nothing to read about
    (None, "0.3.70", None),  # never looked
])
def test_whats_new_is_the_newer_versions_release_page_and_nothing_else(monkeypatch, latest, current, notes):
    """The state's `notes` (the card's "What's new"): the Release page of the newer version, built from the installed
    plugin.json's repository and the version alone; None when nothing is newer."""
    monkeypatch.setattr(config, "plugin_version", lambda: current)
    check = updates.UpdateCheck()
    check.latest = latest
    assert check.state()["notes"] == notes


@pytest.mark.parametrize("version", ["0.3.71/../../evil", "0.3.71?x=1", "v0.3.71", "", "0.3.71#a", "0.3.71 ", "0.3.x"])
def test_whats_new_is_never_built_from_anything_but_a_versions_digits(version):
    assert updates.notes_url(version) is None


def test_whats_new_needs_a_repository_the_plugin_names(monkeypatch):
    monkeypatch.setattr(updates, "repository", lambda: None)
    assert updates.notes_url("0.3.71") is None
