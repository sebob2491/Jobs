"""The password vault (vault.py): saved secrets in the computer's own (Windows Credential Manager, the macOS Keychain,
a Secret Service), secrets.yaml where there's none. Every test here has conftest's vault in memory (memory_vault) or
none (no_vault): none reaches the computer's own."""

import logging
import os

import httpx
import keyring.core
import pytest
from conftest import MemoryVault, run
from keyring.backends import chainer, fail, null

from job_apply import config, doctor, pipeline, vault
from job_apply.desk import Desk, _saved

SERVICE_NAMES = ("workday_password", "successfactors_password", "email_password", config.API_KEY_NAME)
KEY = "sk-ant-api03-not-a-real-key-000000000000"


def in_vault(fake: MemoryVault, name: str) -> str | None:
    return fake.store.get((vault.service(), name))


def test_a_secret_is_read_from_the_environment_then_the_file_then_the_vault(job_apply_home, memory_vault,
                                                                             monkeypatch):
    """A line in secrets.yaml is the newer (the desk takes the line out when it saves to the vault): one written by hand
    since comes before the vault's older one, without waiting for the desk's next start (the review of bf7295d)."""
    config.save_site_password("workday_password", "OLD-pass-1")
    config.secrets_path().write_text('workday_password: "NEW-by-hand"\n', encoding="utf-8")
    vault.reset()
    assert config.get_secret("workday_password") == "NEW-by-hand"
    memory_vault.set_password(vault.service(), "icims_password", "only-in-the-vault")
    assert config.get_secret("icims_password") == "only-in-the-vault"
    monkeypatch.setenv("JOB_APPLY_SECRET_WORKDAY_PASSWORD", "from-the-environment")
    assert config.get_secret("workday_password") == "from-the-environment"
    assert config.get_secret("taleo_password") is None


def test_a_saved_password_goes_to_the_vault_and_its_line_leaves_the_file(job_apply_home, memory_vault):
    path = config.secrets_path()
    path.write_text("# career sites\nworkday_password: old-one\nsuccessfactors_password: keep-me\n", encoding="utf-8")
    config.save_site_password("workday_password", "new one: #1")
    assert in_vault(memory_vault, "workday_password") == "new one: #1"
    text = path.read_text(encoding="utf-8")
    assert text == "# career sites\nsuccessfactors_password: keep-me\n" and "old-one" not in text
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert config.get_secret("workday_password") == "new one: #1"
    # (none in the file to begin with: none made)
    config.save_site_password(config.API_KEY_NAME, f" {KEY} ")
    assert in_vault(memory_vault, config.API_KEY_NAME) == KEY
    assert "anthropic" not in path.read_text(encoding="utf-8")
    # its name is listed beside the person's files, never its value
    index = (job_apply_home / vault.INDEX_FILE).read_text(encoding="utf-8")
    assert "workday_password" in index and config.API_KEY_NAME in index and "new one" not in index


def test_a_save_whose_file_change_fails_puts_the_vault_back(job_apply_home, memory_vault, monkeypatch):
    """All or nothing: the vault took the new one, but the file's older line couldn't be taken out (it would come back
    over the new one at the next start), so the vault goes back to as it was and the save fails."""
    path = config.secrets_path()
    memory_vault.set_password(vault.service(), "icims_password", "vault-old")
    path.write_text("workday_password: old-one\nicims_password: file-newer\n", encoding="utf-8")
    real = config.write_text_atomic

    def boom(where, text):
        if where.name == "secrets.yaml":
            raise OSError("No space left on device")
        real(where, text)

    monkeypatch.setattr(config, "write_text_atomic", boom)
    with pytest.raises(OSError):
        config.save_site_password("workday_password", "new one")
    assert in_vault(memory_vault, "workday_password") is None and config.get_secret("workday_password") == "old-one"
    with pytest.raises(OSError):
        config.save_site_password("icims_password", "newest")
    assert in_vault(memory_vault, "icims_password") == "vault-old"
    assert path.read_text(encoding="utf-8") == "workday_password: old-one\nicims_password: file-newer\n"


def test_a_save_the_vault_refuses_goes_in_the_file_over_the_older_one(job_apply_home, memory_vault, monkeypatch):
    memory_vault.set_password(vault.service(), "workday_password", "older")

    def refuse(*args):
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(memory_vault, "set_password", refuse)
    config.save_site_password("workday_password", "newer")
    assert config.read_secrets(config.secrets_path()) == {"workday_password": "newer"}
    assert in_vault(memory_vault, "workday_password") == "older"  # (left: the file's comes first)
    vault.reset()
    assert config.get_secret("workday_password") == "newer"


def test_a_save_that_fails_everywhere_keeps_the_older_password(job_apply_home, memory_vault, monkeypatch):
    """The review of bf7295d: the vault refused the new one (locked), its put took the older one out, and then the
    file's write failed too, so neither was kept. The older one stays now."""
    config.save_site_password("workday_password", "OLD-pass-1")

    def refuse(*args):
        raise RuntimeError("locked")

    def boom(*args, **kwargs):
        raise PermissionError("held by antivirus")

    with monkeypatch.context() as m, pytest.raises(PermissionError):
        m.setattr(memory_vault, "set_password", refuse)
        m.setattr(config, "write_text_atomic", boom)
        config.save_site_password("workday_password", "NEW-pass-2")
    vault.reset()
    assert config.get_secret("workday_password") == "OLD-pass-1"


def test_the_list_of_names_is_kept_in_the_vault_too(job_apply_home, memory_vault, caplog):
    """The review of bf7295d: with vault-names.json gone (or broken), a password named for a site that was moved into
    the vault escaped the scrubbing of the panel and of reports. The vault keeps the list too: either alone has every
    name."""
    config.secrets_path().write_text('honeywell_password: "Hw-secret-99!"\n', encoding="utf-8")
    assert config.move_secrets_to_vault() == {"moved": 1, "kept": 0}
    for broken in (None, "{not json", '{"names": "honeywell_password"}'):
        index = job_apply_home / vault.INDEX_FILE
        if broken is None:
            index.unlink()
        else:
            index.write_text(broken, encoding="utf-8")
        vault.reset()
        assert "Hw-secret-99!" in pipeline._secret_values(), broken
        assert config.get_secret("honeywell_password") == "Hw-secret-99!"
    # and the file alone, without the vault's list
    config.save_site_password("acme_password", "Acme-pass-4")
    del memory_vault.store[(vault.service(), vault.NAMES_ENTRY)]
    vault.reset()
    assert {"Hw-secret-99!", "Acme-pass-4"} <= pipeline._secret_values()
    # the list itself is no secret: never a value, and never read as one
    listed = in_vault(memory_vault, vault.NAMES_ENTRY) or ""
    assert "Hw-secret" not in listed and vault.get(vault.NAMES_ENTRY) is None
    assert config.get_secret(vault.NAMES_ENTRY) is None


def test_the_list_of_names_keeps_what_another_program_listed(job_apply_home, memory_vault):
    """Read again just before it's written (under a lock): a name another program listed since stays listed."""
    config.save_site_password("workday_password", "Wd-pass-1")
    other = job_apply_home / vault.INDEX_FILE
    other.write_text('{"service": "%s", "names": ["acme_password", "workday_password"]}' % vault.service().replace(
        "\\", "\\\\"), encoding="utf-8")
    config.save_site_password("icims_password", "Ic-pass-2")
    assert {"acme_password", "workday_password", "icims_password"} <= vault._index_vault(fresh=True)
    assert {"acme_password", "workday_password", "icims_password"} <= vault._index_file()[0]


def test_a_list_file_that_cant_be_written_still_lists_it_in_the_vault(job_apply_home, memory_vault, monkeypatch):
    def boom(fd, *args, **kwargs):
        os.close(fd)
        raise OSError("No space left on device")

    with monkeypatch.context() as m:
        m.setattr(os, "fdopen", boom)
        config.save_site_password("acme_password", "Acme-pass-4")
    assert in_vault(memory_vault, "acme_password") == "Acme-pass-4"
    assert "acme_password" in vault._index_vault(fresh=True)
    vault.reset()
    assert "Acme-pass-4" in pipeline._secret_values()


def test_without_a_vault_secrets_stay_in_the_file(job_apply_home, no_vault, caplog):
    path = config.secrets_path()
    path.write_text("# mine\nworkday_password: in-the-file\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert vault.status().usable is False and vault.status().why == "no password vault on this computer"
    assert vault.where() == "secrets.yaml (no password vault on this computer)"
    assert config.move_secrets_to_vault() == {"moved": 0, "kept": 0}
    assert path.read_text(encoding="utf-8") == "# mine\nworkday_password: in-the-file\n"
    config.save_site_password("icims_password", "saved-here")
    assert "icims_password" in path.read_text(encoding="utf-8")
    assert config.get_secret("icims_password") == "saved-here"
    assert config.get_secret("workday_password") == "in-the-file"
    assert vault.values() == set() and pipeline._secret_values() >= {"saved-here", "in-the-file"}
    # said once, by where they're kept and why, and never a value
    said = [r.getMessage() for r in caplog.records if r.name == "job_apply.vault"]
    assert len(said) == 1 and "no password vault" in said[0] and "in-the-file" not in caplog.text


@pytest.mark.parametrize("backend", [fail.Keyring(), null.Keyring(), "empty chainer"])
def test_keyrings_that_keep_nothing_are_no_vault(job_apply_home, monkeypatch, backend):
    if backend == "empty chainer":
        backend = chainer.ChainerBackend()
        monkeypatch.setattr(chainer.ChainerBackend, "backends", [])
    monkeypatch.setattr(keyring.core, "_keyring_backend", backend)
    vault.reset()
    assert vault.status().usable is False
    config.save_site_password("workday_password", "in-the-file")
    assert config.read_secrets(config.secrets_path()) == {"workday_password": "in-the-file"}


def test_a_locked_vault_is_no_vault(job_apply_home, memory_vault, monkeypatch):
    def locked(*args):
        raise keyring.errors.KeyringLocked("Failed to unlock the collection!")

    monkeypatch.setattr(memory_vault, "get_password", locked)
    assert vault.status().usable is False and "KeyringLocked" in vault.status().why
    config.save_site_password("workday_password", "in-the-file")
    assert config.get_secret("workday_password") == "in-the-file"


def test_a_vault_that_stops_answering_is_out_of_use_and_said(srv, job_apply_home, memory_vault, monkeypatch):
    """The review of bf7295d: a call to the vault had no time limit after the start's check (a Secret Service locked
    since opens its unlock prompt, and the page and the worker froze on it), and a failed read came back every 30 s.
    Every call waits VAULT_WAIT at most; the first that doesn't answer puts the vault out of use for the process."""
    import threading
    import time

    config.save_site_password("workday_password", "Wd-in-the-vault")
    config.secrets_path().write_text("icims_password: Ic-in-the-file\n", encoding="utf-8")
    vault.reset()
    assert vault.status().usable
    monkeypatch.setattr(vault, "VAULT_WAIT", 0.3)
    released, asked = threading.Event(), []

    def hangs(*args):
        asked.append(args)
        released.wait(30)
        return None

    monkeypatch.setattr(memory_vault, "get_password", hangs)
    try:
        started = time.monotonic()
        assert config.get_secret("workday_password") is None
        assert time.monotonic() - started < 3
        assert vault.status().usable is False and vault.status().down
        said = vault.problem()
        assert said is not None and "didn't answer, so saved passwords in it can't be read until it's unlocked and " \
                                     "the desk restarted" in said
        # out of use: never asked again; the file is read, and a save goes there
        assert config.get_secret("icims_password") == "Ic-in-the-file" and len(asked) == 1
        config.save_site_password("taleo_password", "Ta-saved-now")
        assert config.read_secrets(config.secrets_path())["taleo_password"] == "Ta-saved-now" and len(asked) == 1
        # said on the desk's page and by the doctor
        assert Desk(srv).state()["vault_problem"] == said
        check = doctor.check_secrets()
        assert check.status == doctor.ADVICE and "didn't answer" in check.fix
        assert check.say.startswith("Your saved passwords are kept in secrets.yaml for now")
    finally:
        released.set()


def test_a_vault_out_of_use_still_has_what_it_gave_scrubbed(job_apply_home, memory_vault, monkeypatch):
    """The second review: once the vault stopped answering, values() gave nothing, so a password the desk had typed
    from it was no longer scrubbed from "What went in" or a problem report. What the vault gave or took stays scrubbed
    for the rest of the process; and a save with the vault out of use never waits on it again."""
    import threading

    config.save_site_password("workday_password", "Wd-typed-from-the-vault")
    vault.reset()
    assert config.get_secret("workday_password") == "Wd-typed-from-the-vault"  # (the desk reads it to type it)
    monkeypatch.setattr(vault, "VAULT_WAIT", 0.3)
    released, asked = threading.Event(), []

    def hangs(*args):
        asked.append(args)
        released.wait(30)

    monkeypatch.setattr(memory_vault, "get_password", hangs)
    monkeypatch.setattr(memory_vault, "set_password", hangs)
    try:
        assert config.get_secret("icims_password") is None and vault.status().down
        assert "Wd-typed-from-the-vault" in vault.values()
        assert vault.put("taleo_password", "Ta-new") is False and len(asked) == 1  # (no second wait)
        with pytest.raises(vault._Down):
            vault._set("taleo_password", "Ta-new")
        assert len(asked) == 1
    finally:
        released.set()
    vault.reset()
    assert "Wd-typed-from-the-vault" not in vault._seen


def test_a_save_from_the_page_waits_off_the_event_loop(srv, job_apply_home, memory_vault, monkeypatch):
    """The page's save runs the vault's call on a thread, not on the desk's event loop."""
    import threading

    loops = []
    real = memory_vault.set_password

    def setting(*args):
        loops.append(threading.current_thread() is threading.main_thread())
        real(*args)

    monkeypatch.setattr(memory_vault, "set_password", setting)
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                body = {"name": "workday_password", "value": "Wd-pass-1"}
                r = await c.post("/api/password", headers={"x-desk-token": desk.token}, json=body)
                assert r.json() == {"saved": True}
        finally:
            await desk.stop()

    run(go())
    assert loops and not any(loops) and config.get_secret("workday_password") == "Wd-pass-1"


def test_a_name_no_secret_is_kept_under_never_reaches_the_vault(job_apply_home, memory_vault):
    """The review of bf7295d: Windows' vault gives back whatever sits under the bare service for an empty user name."""
    memory_vault.store[(vault.service(), "")] = "under-the-bare-service"
    memory_vault.store[(vault.service(), "Workday_Password")] = "upper-case"
    for name in ("", "Workday_Password", "../x", "a b", vault.NAMES_ENTRY):
        assert vault.get(name) is None and config.get_secret(name) is None, name
        with pytest.raises(ValueError, match="isn't a name a password is saved under"):
            vault.put(name, "x")
    # (a hand-written line under such a name stays in the file, and is read there)
    config.secrets_path().write_text("Workday_Password: from-the-file\n", encoding="utf-8")
    assert config.move_secrets_to_vault() == {"moved": 0, "kept": 1}
    assert config.get_secret("Workday_Password") == "from-the-file"


def test_the_live_check_and_the_switch_keep_the_vault_out(job_apply_home, memory_vault, monkeypatch):
    monkeypatch.setenv(vault.OFF_SWITCH, "1")
    vault.reset()
    assert vault.status().usable is False and vault.OFF_SWITCH in vault.status().why
    monkeypatch.delenv(vault.OFF_SWITCH)
    monkeypatch.setenv(config.LIVE_TEST_IDENTITY_ENV, "1")
    vault.reset()
    assert vault.status().usable is False
    config.save_site_password("workday_password", "the-runs-own")
    assert memory_vault.store == {}


def test_a_folder_other_than_the_persons_never_reads_their_passwords(job_apply_home, monkeypatch, tmp_path):
    """The vault's service is "job-apply" for ~/.job-apply alone: a test's or a live check's JOB_APPLY_HOME has one of
    its own, so a run's made-up profile never gets the person's real passwords."""
    assert vault.service() != vault.SERVICE and str(job_apply_home.resolve()) in vault.service()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("JOB_APPLY_HOME")
    assert vault.service() == vault.SERVICE


def test_secrets_yaml_moves_into_the_vault_keeping_comments_and_what_couldnt_move(job_apply_home, memory_vault,
                                                                                 monkeypatch, caplog):
    path = config.secrets_path()
    path.write_text(
        "# My career-site passwords\n"
        "workday_password: 'Wd-pass-1'  # the work one\n"
        "email_password: \"abcd efgh ijkl mnop\"\n"
        "\n"
        "# hand-named for one employer\n"
        "honeywell_password: Hw-pass-2\n"
        "taleo_password: comes-back-otherwise\n"
        "infor_password: |\n"
        "  goes on below\n"
        f"anthropic_api_key: {KEY}\n", encoding="utf-8")
    memory_vault.set_password(vault.service(), "workday_password", "older-in-the-vault")
    real_set = memory_vault.set_password

    def setting(service, name, value):
        real_set(service, name, value + "?" if name == "taleo_password" else value)

    monkeypatch.setattr(memory_vault, "set_password", setting)
    with caplog.at_level(logging.INFO):
        assert config.move_secrets_to_vault() == {"moved": 4, "kept": 2}
    assert {n: in_vault(memory_vault, n) for n in ("workday_password", "email_password", "honeywell_password",
                                                   config.API_KEY_NAME)} == {
        "workday_password": "Wd-pass-1", "email_password": "abcd efgh ijkl mnop", "honeywell_password": "Hw-pass-2",
        config.API_KEY_NAME: KEY}
    # a read-back that differs and a value continued below stay in the file, which comes before the vault
    assert path.read_text(encoding="utf-8") == (
        "# My career-site passwords\n"
        "# hand-named for one employer\n"
        "taleo_password: comes-back-otherwise\n"
        "infor_password: |\n"
        "  goes on below\n")
    for name, value in (("workday_password", "Wd-pass-1"), ("honeywell_password", "Hw-pass-2"),
                        ("taleo_password", "comes-back-otherwise")):
        assert config.get_secret(name) == value
    # names and counts only
    for value in ("Wd-pass-1", "abcd", "Hw-pass-2", "comes-back", KEY):
        assert value not in caplog.text
    assert "Moved 4" in caplog.text and "2 stayed" in caplog.text
    # every one is still seen, wherever it is, the hand-named one too (by the lists of names)
    assert pipeline._secret_values() >= {"Wd-pass-1", "abcd efgh ijkl mnop", "Hw-pass-2", KEY,
                                         "comes-back-otherwise"}
    # and again: nothing more moves (the one given back otherwise is tried again, and stays)
    assert config.move_secrets_to_vault() == {"moved": 0, "kept": 2}


def test_a_vault_that_refuses_the_move_leaves_the_file_as_it_was(job_apply_home, memory_vault, monkeypatch):
    path = config.secrets_path()
    written = "# mine\nworkday_password: Wd-pass-1\nicims_password: Ic-pass-2\n"
    path.write_text(written, encoding="utf-8")

    def refuse(*args):
        raise keyring.errors.PasswordSetError("refused")

    monkeypatch.setattr(memory_vault, "set_password", refuse)
    assert config.move_secrets_to_vault() == {"moved": 0, "kept": 2}
    assert path.read_text(encoding="utf-8") == written and vault.problem() is not None
    assert config.get_secret("icims_password") == "Ic-pass-2"


def test_a_save_from_the_page_during_the_move_isnt_overwritten(job_apply_home, memory_vault, monkeypatch):
    """The review of bf7295d: the move read the file's OLD, the page saved NEW (into the vault, its line out of the
    file), and then the move put OLD back over it."""
    config.secrets_path().write_text('workday_password: "OLD-file"\nicims_password: Ic-pass-2\n', encoding="utf-8")
    real = config.read_secrets

    def read_then_save(path):
        out = real(path)
        monkeypatch.setattr(config, "read_secrets", real)
        config.save_site_password("workday_password", "NEW-from-page")  # (the page's save, mid-move)
        return out

    monkeypatch.setattr(config, "read_secrets", read_then_save)
    assert config.move_secrets_to_vault() == {"moved": 1, "kept": 0}
    vault.reset()
    assert config.get_secret("workday_password") == "NEW-from-page"
    assert in_vault(memory_vault, "workday_password") == "NEW-from-page"
    assert config.get_secret("icims_password") == "Ic-pass-2" and not config.read_secrets(config.secrets_path())


def test_a_save_and_the_move_take_turns(job_apply_home, memory_vault, monkeypatch):
    """One lock across a save, a forget and the move: a save from another thread waits for the move to finish."""
    import threading

    config.secrets_path().write_text('workday_password: "OLD-file"\n', encoding="utf-8")
    real_put, order = vault.put, []
    in_move = threading.Event()

    def put(name, value):
        order.append(value)
        if value == "OLD-file":
            in_move.set()
            threading.Event().wait(0.3)  # (the page's save is asked for now)
        return real_put(name, value)

    monkeypatch.setattr(vault, "put", put)
    saver = threading.Thread(target=lambda: in_move.wait(5) and config.save_site_password("workday_password",
                                                                                            "NEW-from-page"))
    saver.start()
    config.move_secrets_to_vault()
    saver.join(10)
    assert order == ["OLD-file", "NEW-from-page"]
    assert config.get_secret("workday_password") == "NEW-from-page"


def test_the_vaults_names_cover_the_desks_secrets_without_the_lists(job_apply_home, memory_vault):
    """keyring can't list what it holds: the lists of names, and should both be gone, the ones the desk saves (each job
    system's password, the email app password, the Anthropic API key, the phone alerts' topic)."""
    config.save_site_password("ukg_password", "Ukg-pass-3")
    (job_apply_home / vault.INDEX_FILE).unlink()
    del memory_vault.store[(vault.service(), vault.NAMES_ENTRY)]
    vault.reset()
    assert "Ukg-pass-3" in pipeline._secret_values()
    assert set(config.DESK_PASSWORDS) | {"email_password", config.API_KEY_NAME} <= vault.names()


def test_a_folder_moved_since_its_passwords_were_saved_is_said(job_apply_home, memory_vault, caplog, capsys):
    """The review of bf7295d: the vault's entries are under the folder's own service, so a folder moved since leaves
    them out of reach; the start and the doctor say so. And deleting the folder doesn't remove them: the doctor's
    --forget-passwords does."""
    config.save_site_password("workday_password", "Wd-pass-1")
    assert vault.moved_from() is None and doctor.check_secrets().status == doctor.OK
    index = job_apply_home / vault.INDEX_FILE
    data = index.read_text(encoding="utf-8").replace(vault.service().replace("\\", "\\\\"),
                                                     "job-apply:/an/old/place")
    index.write_text(data, encoding="utf-8")
    assert vault.moved_from() == "/an/old/place"
    check = doctor.check_secrets()
    said = "while this folder was at /an/old/place can't be reached from here: save them again"
    assert check.status == doctor.ADVICE and said in check.fix
    with caplog.at_level(logging.WARNING):
        config.move_secrets_to_vault()
    assert said in caplog.text
    config.save_site_password("icims_password", "Ic-pass-2")  # (saved again: the list is this folder's now)
    assert vault.moved_from() is None
    # forgetting them all: each listed one, and the lists
    assert doctor.main(["--forget-passwords"]) == 0
    assert "Removed 2 saved passwords" in capsys.readouterr().out
    assert memory_vault.store == {} and not index.exists()
    assert config.get_secret("workday_password") is None


def test_the_desk_moves_secrets_yaml_into_the_vault_at_its_start_and_says_what_is_saved(srv, job_apply_home,
                                                                                         memory_vault):
    config.secrets_path().write_text("# mine\nworkday_password: Wd-pass-1\nemail_password: abcdefghijklmnop\n",
                                     encoding="utf-8")
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                h = {"x-desk-token": desk.token}
                state = await c.get("/api/state", headers=h)
                assert state.json()["passwords"] == {
                    "workday": True, "successfactors": False, "icims": False, "applicantstack": False, "ukg": False,
                    "infor": False, "taleo": False, "neogov": False, "email": True, "anthropic": False}
                assert "Wd-pass-1" not in state.text and "abcdefgh" not in state.text
                body = {"name": "taleo_password", "value": "Taleo-pass-5"}
                assert (await c.post("/api/password", headers=h, json=body)).json() == {"saved": True}
                state = await c.get("/api/state", headers=h)
                assert state.json()["passwords"]["taleo"] is True and "Taleo-pass-5" not in state.text
        finally:
            await desk.stop()

    run(go())
    assert config.secrets_path().read_text(encoding="utf-8") == "# mine\n"
    assert in_vault(memory_vault, "workday_password") == "Wd-pass-1"
    assert in_vault(memory_vault, "taleo_password") == "Taleo-pass-5"
    assert _saved(["workday_password", "email_password", "icims_password"]) == {
        "workday_password": True, "email_password": True, "icims_password": False}


@pytest.mark.parametrize("module, said", [("keyring.backends.Windows", "Windows Credential Manager"),
                                          ("keyring.backends.macOS", "macOS Keychain")])
def test_the_doctor_says_where_saved_passwords_are_kept(job_apply_home, monkeypatch, module, said):
    monkeypatch.setattr(MemoryVault, "__module__", module)
    monkeypatch.setattr(keyring.core, "_keyring_backend", MemoryVault())
    vault.reset()
    check = doctor.check_secrets()
    assert (check.status, check.say) == (doctor.OK, f"Your saved passwords are kept in {said}")


def test_the_doctor_says_secrets_yaml_without_a_vault(job_apply_home, no_vault, caplog):
    check = doctor.check_secrets()
    assert check.say == "Your saved passwords are kept in secrets.yaml (no password vault on this computer)"
    assert check.status == doctor.OK and check.fix == "" and "job_apply.vault" not in caplog.text


TOPIC = "jobdesk-notarealtopic0000000001"


def test_forgetting_the_phone_topic_takes_it_out_of_the_vault_its_list_and_the_file(job_apply_home, memory_vault):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    assert in_vault(memory_vault, config.NTFY_TOPIC_NAME) == TOPIC and not config.secrets_path().exists()
    assert config.NTFY_TOPIC_NAME in (job_apply_home / vault.INDEX_FILE).read_text(encoding="utf-8")
    assert TOPIC in pipeline._secret_values()
    # (and a line written by hand since, which would come back at the next start's move)
    config.secrets_path().write_text(f"# mine\nworkday_password: keep-me\n{config.NTFY_TOPIC_NAME}: {TOPIC}x\n",
                                     encoding="utf-8")
    assert config.forget_secret(config.NTFY_TOPIC_NAME) is True
    assert in_vault(memory_vault, config.NTFY_TOPIC_NAME) is None
    assert config.NTFY_TOPIC_NAME not in (job_apply_home / vault.INDEX_FILE).read_text(encoding="utf-8")
    assert config.secrets_path().read_text(encoding="utf-8") == "# mine\nworkday_password: keep-me\n"
    assert config.get_secret(config.NTFY_TOPIC_NAME) is None
    assert TOPIC in pipeline._secret_values()  # (what the vault held stays scrubbed for the rest of the process)
    assert config.forget_secret(config.NTFY_TOPIC_NAME) is False  # (none left)
    with pytest.raises(ValueError):
        config.forget_secret("workday_password")  # (only the topic is forgotten from the desk)


def test_a_vault_that_wont_let_the_topic_go_says_so(job_apply_home, memory_vault, monkeypatch):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)

    def refuse(*args):
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(memory_vault, "delete_password", refuse)
    with pytest.raises(ValueError, match="wouldn't let it go"):
        config.forget_secret(config.NTFY_TOPIC_NAME)
    assert in_vault(memory_vault, config.NTFY_TOPIC_NAME) == TOPIC
    # (out of use now: turning off again says so, rather than leaving it to come back at the next start)
    with pytest.raises(ValueError, match="wouldn't let it go"):
        config.forget_secret(config.NTFY_TOPIC_NAME)


def test_forgetting_the_phone_topic_without_a_vault_edits_the_file(job_apply_home, no_vault):
    config.secrets_path().write_text("# mine\nworkday_password: keep-me\n", encoding="utf-8")
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    assert config.read_secrets(config.secrets_path())[config.NTFY_TOPIC_NAME] == TOPIC
    assert config.forget_secret(config.NTFY_TOPIC_NAME) is True
    assert config.secrets_path().read_text(encoding="utf-8") == "# mine\nworkday_password: keep-me\n"


def test_the_phone_topic_moves_into_the_vault_and_is_scrubbed_like_a_password(job_apply_home, memory_vault):
    config.secrets_path().write_text(f"# phone\n{config.NTFY_TOPIC_NAME}: {TOPIC}\n", encoding="utf-8")
    assert config.move_secrets_to_vault() == {"moved": 1, "kept": 0}
    assert in_vault(memory_vault, config.NTFY_TOPIC_NAME) == TOPIC
    assert config.secrets_path().read_text(encoding="utf-8") == "# phone\n"
    assert config.get_secret(config.NTFY_TOPIC_NAME) == TOPIC
    # found with no list of names too: it's one of the desk's own
    (job_apply_home / vault.INDEX_FILE).unlink()
    vault.reset()
    assert config.NTFY_TOPIC_NAME in vault.names() and TOPIC in pipeline._secret_values()
