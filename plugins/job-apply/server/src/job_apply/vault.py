"""The operating system's password vault, where the desk keeps its saved secrets (each job system's password, the
email app password, the Anthropic API key, the phone alerts' topic): Windows Credential Manager, the macOS Keychain, or
a Linux desktop's Secret Service, through the `keyring` package. Each secret is an entry of the service "job-apply"
(the secret's name as its user name).

Where there's no vault (a headless Linux, CI, the nightly live check, a keyring that's locked or turned off), the
secrets stay in ~/.job-apply/secrets.yaml as before (config.get_secret reads the environment, then the vault, then the
file). Whether the vault works is found out once, by a read of an entry that's never there, and said once.

A vault can't list what it holds, so the names of the secrets put in it are kept beside the person's files
(vault-names.json: names only, never a value), for whatever has to see every secret (the scrubbing of problem reports
and the "What went in" panel: pipeline._secret_values).

Only names and counts are ever logged, never a value.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

SERVICE = "job-apply"
# Set to 1 to keep the vault out of it (the live check sets it): secrets come from the environment and secrets.yaml
OFF_SWITCH = "JOB_APPLY_NO_VAULT"
PROBE_NAME = "job-apply-probe"  # (the entry the probe asks for, which is never there)
PROBE_WAIT = 5.0  # seconds the probe waits for the vault to answer (a Secret Service that hangs is no vault)
READ_FOR = 30.0  # seconds a value read from the vault is taken as still there (the page asks every 1.5 s)
INDEX_FILE = "vault-names.json"

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
    why: str = ""  # why there's none, in plain words


class Stuck(Exception):
    """The vault holds an older value of a secret and wouldn't take a new one, nor let it go: the older one would
    still come before the file's."""


_lock = threading.RLock()
_status: Status | None = None
_read: dict[tuple[str, str], tuple[float, str | None]] = {}  # (service, name) -> (when read, value)
_said: set[str] = set()


def reset() -> None:
    """Forget what was found out (for the tests)."""
    global _status
    with _lock:
        _status = None
        _read.clear()
        _said.clear()


def service() -> str:
    """The vault's service name: "job-apply" for the person's own folder (~/.job-apply), and one of its own for any
    other JOB_APPLY_HOME (a test's, the live check's), so a folder made for a run never reads or writes the person's
    own passwords."""
    home = config.home()
    with contextlib.suppress(OSError, RuntimeError):
        if home.resolve() == Path("~/.job-apply").expanduser().resolve():
            return SERVICE
    return f"{SERVICE}:{home.resolve()}"


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
    answer: dict[str, Any] = {}

    def look() -> None:
        try:
            kr.get_password(service(), PROBE_NAME)
            answer["ok"] = True
        except Exception as e:  # (a locked keyring the person didn't unlock, a Secret Service that's gone)
            answer["error"] = e

    t = threading.Thread(target=look, name="vault-probe", daemon=True)
    t.start()
    t.join(PROBE_WAIT)
    name = vault_name(backends[0])
    if "ok" in answer:
        return Status(True, name)
    if "error" in answer:
        error = answer["error"]
        return Status(False, why=f"{name} didn't answer ({type(error).__name__}: {str(error)[:120]})")
    return Status(False, why=f"{name} didn't answer within {PROBE_WAIT:.0f} seconds")


def where() -> str:
    """Where saved secrets live on this computer, as the doctor says it."""
    s = status()
    return s.name if s.usable else "secrets.yaml (no password vault on this computer)"


def get(name: str, fresh: bool = False) -> str | None:
    """A secret's value in the vault, or None (none there, or no vault). Read again only after READ_FOR seconds,
    unless `fresh`."""
    if not status().usable:
        return None
    key = (service(), name)
    now = time.monotonic()
    with _lock:
        hit = _read.get(key)
        if hit is not None and not fresh and now - hit[0] < READ_FOR:
            return hit[1]
    try:
        value = _keyring().get_password(*key)
    except Exception as e:  # (locked since: the file's comes next)
        _say_once("read", "The password vault couldn't be read just now (%s)", type(e).__name__)
        value = None
    with _lock:
        _read[key] = (now, value)
    return value


def put(name: str, value: str) -> bool:
    """Keep a secret in the vault: True once it's there and reads back as written. False when it isn't (no vault, or
    the vault refused it or gave back something else), with nothing of it left there that would come before the
    file's. Raises Stuck when an older value stays there that the vault won't let go, and OSError when the list of
    names can't be written (the vault untouched: a value it couldn't list would escape the scrubbing of reports)."""
    if not status().usable:
        return False
    key = (service(), name)
    kr = _keyring()
    _index_add(name)  # (first: a name listed with nothing in the vault is harmless; a value not listed isn't)
    try:
        kr.set_password(*key, value)
        back = kr.get_password(*key)
    except Exception as e:
        _say_once("write", "The password vault wouldn't keep a password just now (%s); it goes in %s instead",
                  type(e).__name__, config.secrets_path().name)
        back = None
    with _lock:
        _read.pop(key, None)
    if back == value:
        with _lock:
            _read[key] = (time.monotonic(), value)
        return True
    if back is not None:
        log.warning("The password vault gave %s back other than it was written; it stays in %s", name,
                    config.secrets_path().name)
    forget(name)
    return False


def forget(name: str) -> None:
    """Take a secret out of the vault (none there is fine). Raises Stuck when one stays there."""
    if not status().usable:
        return
    key = (service(), name)
    kr = _keyring()
    try:
        kr.delete_password(*key)
    except Exception:  # (keyring's PasswordDeleteError for one that isn't there, among others)
        pass
    with _lock:
        _read.pop(key, None)
    try:
        left = kr.get_password(*key)
    except Exception:
        left = None
    if left is not None:
        raise Stuck(name)


def _index_path() -> Path:
    return config.home() / INDEX_FILE


def _index() -> set[str]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
        names = data.get("names") if isinstance(data, dict) else None
        return {str(n) for n in names or [] if isinstance(n, str)}
    except (OSError, ValueError):
        return set()


def _index_write(names: set[str]) -> None:
    config.ensure_home()
    config.write_text_atomic(_index_path(), json.dumps({"service": service(), "names": sorted(names)}, indent=1) + "\n")


def _index_add(name: str) -> None:
    names = _index()
    if name not in names:
        _index_write(names | {name})


def unlist(name: str) -> None:
    """Take a secret's name off the list of names (once forget has taken it out of the vault). A list that can't be
    written keeps it: a name listed with nothing in the vault is harmless."""
    names = _index()
    if name in names:
        with contextlib.suppress(OSError):
            _index_write(names - {name})


def names() -> set[str]:
    """Every secret's name the vault may hold: the ones put there (vault-names.json), and the ones the desk saves
    (each job system's password, the email app password, the Anthropic API key, the phone alerts' topic) should that
    file be gone."""
    return _index() | set(config.DESK_PASSWORDS) | {"email_password", config.API_KEY_NAME, config.NTFY_TOPIC_NAME}


def values() -> set[str]:
    """Every secret's value in the vault (none without one)."""
    if not status().usable:
        return set()
    return {v for name in sorted(names()) if (v := get(name))}
