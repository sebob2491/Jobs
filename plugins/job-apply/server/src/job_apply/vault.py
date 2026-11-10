"""The operating system's password vault, where the desk keeps its saved secrets (each job system's password, the
email app password, the Anthropic API key, the phone alerts' topic): Windows Credential Manager, the macOS Keychain, or
a Linux desktop's Secret Service, through the `keyring` package. Each secret is an entry of the service "job-apply"
(the secret's name as its user name).

Where there's no vault (a headless Linux, CI, the nightly live check, a keyring that's locked or turned off), the
secrets stay in ~/.job-apply/secrets.yaml as before. config.get_secret reads the environment, then secrets.yaml, then
the vault: the file only has a line the person wrote there (or one saved while the vault wouldn't take it), which is
the newer. Whether the vault works is found out once, by a read of an entry that's never there, and said once.

Every call to the vault waits VAULT_WAIT seconds at most, on a thread of its own (a Secret Service locked since can
hang on its unlock prompt). The first that fails or doesn't answer puts the vault out of use for the rest of the
process: secrets are read from the environment and the file, saved to the file, and the desk, the doctor and the log
say why.

A vault can't list what it holds, so the names of the secrets put in it are kept in a list, in the vault itself (an
entry of its own, NAMES_ENTRY) and beside the person's files (vault-names.json, a copy: names only, never a value);
either alone covers every name. Whatever has to see every secret reads it (the scrubbing of problem reports and the
"What went in" panel: pipeline._secret_values).

Only names and counts are ever logged, never a value.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

SERVICE = "job-apply"
# Set to 1 to keep the vault out of it (the live check sets it): secrets come from the environment and secrets.yaml
OFF_SWITCH = "JOB_APPLY_NO_VAULT"
PROBE_NAME = "job-apply-probe"  # (the entry the probe asks for, which is never there)
VAULT_WAIT = 5.0  # seconds any call to the vault waits for its answer
PROBE_WAIT = VAULT_WAIT
READ_FOR = 30.0  # seconds a value read from the vault is taken as still there (the page asks every 1.5 s)
INDEX_FILE = "vault-names.json"
NAMES_ENTRY = "job_apply_names"  # the vault's own entry holding the list of names (never a secret's name)
# A secret's name in the vault: lower-case letters, digits and _ (Windows' vault gives back whatever sits under the bare
# service for an empty user name)
NAME = re.compile(r"^[a-z0-9_]+$")
# What the desk and the doctor say when the vault stops answering
DIDNT_ANSWER = ("{vault} didn't answer, so saved passwords in it can't be read until it's unlocked and the desk "
                "restarted ({detail})")

# The vault's name as a person knows it, by the keyring backend's module
VAULT_NAMES = {
    "keyring.backends.Windows": "Windows Credential Manager",
    "keyring.backends.macOS": "macOS Keychain",
    "keyring.backends.SecretService": "your desktop's keyring (Secret Service)",
    "keyring.backends.libsecret": "your desktop's keyring (Secret Service)",
    "keyring.backends.kwallet": "KWallet",
}


@dataclass(frozen=True)
class Status:
    usable: bool
    name: str = ""  # the vault, as a person knows it ("Windows Credential Manager"); "" when there's none
    why: str = ""  # why it isn't used, in plain words
    down: bool = False  # there is one, but it failed or didn't answer (locked): a problem, not just its absence


class Stuck(Exception):
    """The vault holds a value it won't let go (or can't be reached to take it out)."""


class _Down(Exception):
    """A call to the vault failed or didn't answer: it's out of use for the rest of the process."""


_lock = threading.RLock()
_index_lock = threading.Lock()
_status: Status | None = None
_read: dict[tuple[str, str], tuple[float, str | None]] = {}  # (service, name) -> (when read, value)
_said: set[str] = set()
_seen: set[str] = set()  # every value the vault gave or took in this process: scrubbed even once it's out of use


def reset() -> None:
    """Forget what was found out (for the tests)."""
    global _status
    with _lock:
        _status = None
        _read.clear()
        _said.clear()
        _seen.clear()


def service() -> str:
    """The vault's service name: "job-apply" for the person's own folder (~/.job-apply), and one of its own for any
    other JOB_APPLY_HOME (a test's, the live check's), so a folder made for a run never reads or writes the person's
    own passwords."""
    home = config.home()
    with contextlib.suppress(OSError, RuntimeError):
        if home.resolve() == Path("~/.job-apply").expanduser().resolve():
            return SERVICE
    return f"{SERVICE}:{home.resolve()}"


def valid(name: str) -> bool:
    """A name a secret can be kept under in the vault."""
    return bool(NAME.match(name)) and name != NAMES_ENTRY


def _say_once(key: str, message: str, *args: Any) -> None:
    if key not in _said:
        _said.add(key)
        log.warning(message, *args)


def _keyring() -> Any:
    import keyring

    return keyring.get_keyring()


def _backends(kr: Any) -> list[Any]:
    """The backends that can keep a secret: none for keyring's "fail" and "null" ones, or a chainer with nothing."""
    from keyring.backends import chainer, fail, null

    nothing = (fail.Keyring, null.Keyring)
    if isinstance(kr, chainer.ChainerBackend):
        return [b for b in kr.backends if not isinstance(b, nothing)]
    return [] if isinstance(kr, nothing) else [kr]


def vault_name(backend: Any) -> str:
    """A backend's vault as a person knows it."""
    return VAULT_NAMES.get(type(backend).__module__) or str(getattr(backend, "name", "") or type(backend).__name__)


def _run(what: str, fn: Callable[..., Any], *args: Any) -> dict[str, Any]:
    """fn(*args) on a thread of its own, VAULT_WAIT seconds at most: {"value": ...}, {"error": e}, or {} (no answer;
    the thread is left to finish, or not, by itself)."""
    answer: dict[str, Any] = {}

    def call() -> None:
        try:
            answer["value"] = fn(*args)
        except Exception as e:
            answer["error"] = e

    t = threading.Thread(target=call, name=f"vault-{what}", daemon=True)
    t.start()
    t.join(VAULT_WAIT)
    return answer


def _detail(answer: dict[str, Any]) -> str:
    error = answer.get("error")
    if error is None:
        return f"no answer within {VAULT_WAIT:.0f} seconds"
    return f"{type(error).__name__}: {str(error)[:120]}"


def _call(what: str, fn: Callable[..., Any], *args: Any) -> Any:
    """A call to the vault, VAULT_WAIT seconds at most. keyring's "isn't there" for a deletion is raised as it is; any
    other failure, or no answer, puts the vault out of use for the rest of the process (_Down)."""
    import keyring.errors

    answer = _run(what, fn, *args)
    if "value" in answer:
        return answer["value"]
    if isinstance(answer.get("error"), keyring.errors.PasswordDeleteError):
        raise answer["error"]
    _out_of_use(_detail(answer))
    raise _Down(what)


def _out_of_use(detail: str) -> None:
    global _status
    with _lock:
        name = (_status.name if _status is not None else "") or "The password vault"
        _status = Status(False, name, DIDNT_ANSWER.format(vault=name, detail=detail), down=True)
        _read.clear()
    _say_once("down", "%s", _status.why)


def status(say: bool = True) -> Status:
    """Whether the vault works on this computer, found out once (cached), and said once (in the server's log) when it
    doesn't, unless not `say` (the doctor says it its own way)."""
    global _status
    with _lock:
        if _status is None:
            _status = _probe()
            if not _status.usable and say:
                _say_once("status", "Saved passwords are kept in %s: %s", config.secrets_path(), _status.why)
        return _status


def _probe() -> Status:
    if os.environ.get(OFF_SWITCH) == "1":
        return Status(False, why=f"the password vault is turned off ({OFF_SWITCH}=1)")
    if os.environ.get(config.LIVE_TEST_IDENTITY_ENV) == "1":
        return Status(False, why="the live check's test identity never uses the person's password vault")
    try:
        kr = _keyring()
        backends = _backends(kr)
    except Exception as e:  # (keyring's own setup failed: a broken keyringrc, a missing library)
        return Status(False, why=f"the password vault couldn't be opened ({type(e).__name__})")
    if not backends:
        return Status(False, why="no password vault on this computer")
    name = vault_name(backends[0])
    # (a locked keyring the person didn't unlock, a Secret Service that's gone or hangs)
    answer = _run("probe", kr.get_password, service(), PROBE_NAME)
    if "value" in answer:
        return Status(True, name)
    return Status(False, name, DIDNT_ANSWER.format(vault=name, detail=_detail(answer)), down=True)


def try_again() -> bool:
    """Whether the vault answers, asked once more when it's out of use (down): a person's press on the desk's password
    panel (Make, Show, Forget), never a loop, so a vault that hangs is waited on VAULT_WAIT seconds per press at most.
    Asked as the start's probe asks it, under the vault's lock. Once it answers, it's in use again as though it had
    never been down: problem() is None and what it holds is read afresh. Usable or not, as status() says otherwise."""
    global _status
    with _lock:
        s = status()
        if not s.down:
            return s.usable
        try:
            kr = _keyring()
            backends = _backends(kr)
        except Exception:
            return False
        if not backends:
            return False
        if "value" not in _run("probe", kr.get_password, service(), PROBE_NAME):
            return False
        _status = Status(True, vault_name(backends[0]))
        _read.clear()
        _said.discard("down")  # (should it stop answering again, that's said again)
    log.warning("%s answers again: saved passwords in it are read again", _status.name)
    return True


def problem() -> str | None:
    """Why the vault there is isn't being used (it failed or didn't answer), for the desk's page; None otherwise."""
    s = status()
    return s.why if s.down else None


def where() -> str:
    """Where saved secrets live on this computer, as the doctor says it."""
    s = status(say=False)
    if s.usable:
        return s.name
    if s.down:
        return f"secrets.yaml for now ({s.name} didn't answer)"
    return "secrets.yaml (no password vault on this computer)"


def _get(name: str, fresh: bool = False) -> str | None:
    if not status().usable:
        return None
    key = (service(), name)
    now = time.monotonic()
    with _lock:
        hit = _read.get(key)
        if hit is not None and not fresh and now - hit[0] < READ_FOR:
            return hit[1]
    try:
        value = _call("read", _keyring().get_password, *key)
    except _Down:
        return None
    with _lock:
        _read[key] = (now, value)
        if value and name != NAMES_ENTRY:
            _seen.add(value)
    return value


def get(name: str, fresh: bool = False) -> str | None:
    """A secret's value in the vault, or None (none there, no vault, or not a name one is kept under). Read again
    only after READ_FOR seconds, unless `fresh`."""
    return _get(name, fresh) if valid(name) else None


def _set(name: str, value: str) -> str | None:
    """Written, and what reads back. Raises _Down (at once when the vault is out of use: never a second wait)."""
    if not status().usable:
        raise _Down("write")
    if name != NAMES_ENTRY:
        with _lock:
            _seen.add(value)
    key = (service(), name)
    kr = _keyring()
    with _lock:
        _read.pop(key, None)
    _call("write", kr.set_password, *key, value)
    back = _call("read", kr.get_password, *key)
    with _lock:
        _read[key] = (time.monotonic(), back)
    return back


def put(name: str, value: str) -> bool:
    """Keep a secret in the vault: True once it's there (and listed) and reads back as written. False when it isn't
    (no vault, or the vault refused it, gave back something else, or stopped answering); what it held before may still
    be there, which secrets.yaml's line (written next by the caller) comes before. A ValueError for a name no secret
    is kept under."""
    if not valid(name):
        raise ValueError(f"{name!r} isn't a name a password is saved under (lower-case letters, digits and _ only)")
    if not status().usable:
        return False
    try:
        _index_add(name)  # (first: a name listed with nothing in the vault is harmless; a value not listed isn't)
        back = _set(name, value)
    except _Down:
        return False
    if back != value:
        log.warning("The password vault gave %s back other than it was written; it goes in %s instead", name,
                    config.secrets_path().name)
        return False
    return True


def forget(name: str) -> None:
    """Take a secret out of the vault (none there is fine). Raises Stuck when it stays there, or when the vault there
    is can't be reached to take it out."""
    if not valid(name):
        return
    _forget(name)


def _forget(name: str) -> None:
    import keyring.errors

    s = status()
    if not s.usable:
        if s.down:
            raise Stuck(name)
        return
    key = (service(), name)
    kr = _keyring()
    with _lock:
        _read.pop(key, None)
    try:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):  # (none there)
            _call("delete", kr.delete_password, *key)
        left = _call("read", kr.get_password, *key)
    except _Down:
        raise Stuck(name) from None
    if left is not None:
        raise Stuck(name)


# ------------------------------------------------------------------ the list of names
def _index_path() -> Path:
    return config.home() / INDEX_FILE


def _index_file() -> tuple[set[str], str | None]:
    """vault-names.json's names, and the service they were written under."""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), None
    if not isinstance(data, dict):
        return set(), None
    names = data.get("names")
    names = names if isinstance(names, list) else []
    stored = data.get("service")
    return {n for n in names if isinstance(n, str) and valid(n)}, stored if isinstance(stored, str) else None


def _index_vault(fresh: bool = False) -> set[str]:
    """The vault's own list of names."""
    try:
        names = json.loads(_get(NAMES_ENTRY, fresh) or "[]")
    except ValueError:
        return set()
    return {n for n in names if isinstance(n, str) and valid(n)} if isinstance(names, list) else set()


def _index() -> set[str]:
    return _index_file()[0] | _index_vault()


def _index_write(names: set[str]) -> None:
    """Both lists: the vault's (raises _Down), then the file's copy (one that can't be written is said once)."""
    listed = json.dumps(sorted(names))
    if _set(NAMES_ENTRY, listed) != listed:  # (given back otherwise: a value put now wouldn't be listed)
        raise _Down("names")
    try:
        config.ensure_home()
        config.write_text_atomic(_index_path(), json.dumps({"service": service(), "names": sorted(names)},
                                                           indent=1) + "\n")
    except OSError as e:
        _say_once("index", "The copy of the vault's list of names (%s) couldn't be written (%s)", INDEX_FILE,
                  type(e).__name__)


def _index_add(name: str) -> None:
    with _index_lock:  # (read again just before writing, so another program's names written since stay too)
        names = _index_file()[0] | _index_vault(fresh=True)
        if name not in names or name not in _index_file()[0]:
            _index_write(names | {name})


def unlist(name: str) -> None:
    """Take a secret's name off the lists of names (once forget has taken it out of the vault). Lists that can't be
    written keep it: a name listed with nothing in the vault is harmless."""
    if not status().usable:
        return
    with _index_lock:
        names = _index_file()[0] | _index_vault(fresh=True)
        if name in names:
            with contextlib.suppress(_Down):
                _index_write(names - {name})


def names() -> set[str]:
    """Every secret's name the vault may hold: the ones listed (in the vault and in vault-names.json), and the ones the
    desk saves (each job system's password, the email app password, the Anthropic API key, the phone alerts' topic)."""
    return _index() | set(config.DESK_PASSWORDS) | {"email_password", config.API_KEY_NAME, config.NTFY_TOPIC_NAME}


def values() -> set[str]:
    """Every secret's value in the vault, and every one it gave or took in this process (kept for scrubbing even once
    the vault is out of use); none without one."""
    if not status().usable:
        with _lock:
            return set(_seen)
    found = {v for name in sorted(names()) if (v := get(name))}
    with _lock:
        return found | _seen


def moved_from() -> str | None:
    """Where this folder was when the vault's list of names was written, if that was somewhere else: the passwords
    saved then are under that folder's service, which this one doesn't reach. None otherwise."""
    stored = _index_file()[1]
    if not stored or stored == service():
        return None
    return "~/.job-apply" if stored == SERVICE else stored.removeprefix(f"{SERVICE}:")


def moved_warning() -> str | None:
    """moved_from in plain words."""
    old = moved_from()
    if old is None:
        return None
    return (f"Passwords saved in the password vault while this folder was at {old} can't be reached from here: save "
            "them again on the desk's Site passwords card.")


def forget_all() -> int:
    """Take every listed secret of this folder out of the vault, and the lists of names with them (deleting the folder
    doesn't: the vault keeps them). How many were there. Raises Stuck when one stays."""
    if not status().usable:
        if status().down:
            raise Stuck("the password vault")
        return 0
    gone = 0
    for name in sorted(names()):
        if get(name, fresh=True) is not None:
            forget(name)
            gone += 1
    with _index_lock:
        _forget(NAMES_ENTRY)
        with contextlib.suppress(OSError):
            _index_path().unlink()
    return gone
