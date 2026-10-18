"""Start the plugin's MCP server the way Claude does and ask it for its tools: the check that the plugin a person
installs starts at all (CI's "install and start" job; the review of Oct 18).

    uv run --no-project python scripts/mcp_handshake.py                    # the checkout's own plugin folder
    uv run --no-project python scripts/mcp_handshake.py --install --doctor  # as a person gets it, then the doctor

It reads the plugin's `.mcp.json`, runs its command (`uv run --frozen --project ${CLAUDE_PLUGIN_ROOT}/server
job-apply-mcp`) with CLAUDE_PLUGIN_ROOT set, and speaks MCP to it over stdio: `initialize`, `notifications/initialized`,
`tools/list`. The server must name itself job-apply at plugin.json's version and list exactly the tools server.py
registers (each function under `@tool(...)`, read from the source, not imported). JOB_APPLY_HOME and the home folder
are temporary folders, so nothing reads or writes a real profile.

--install first does what a person does, in that temporary home: `claude plugin marketplace add <this checkout>` and
`claude plugin install job-apply@sebob-jobs` (no login needed), and checks the copy Claude installed. --doctor then
runs `job-apply-doctor --json` there, as the one-step installer does: on a clean machine it must say only that there's
no job-apply folder, profile or resume yet (and, on Windows, that UV_PYTHON_INSTALL_DIR isn't set, which the
installer sets), each a problem by design until setup, so it exits 1 then.

Exits 0 when all is well, 1 with the reason otherwise (a server that won't start, a timeout, a tool missing). The
server is always stopped: its input closed, then terminated, then killed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, Any

PLUGIN = Path(__file__).resolve().parents[2]  # plugins/job-apply
MARKETPLACE = PLUGIN.parents[1]  # the repository: .claude-plugin/marketplace.json
SERVER_NAME = "job-apply"
PLUGIN_ID = "job-apply@sebob-jobs"
PROTOCOL = "2025-06-18"
# What the doctor finds wrong on a machine nothing was set up on (each a problem until setup or the installer)
CLEAN_MACHINE_PROBLEMS = {"folder", "profile", "resume", "uv_python_dir"}


class Failed(Exception):
    """The check failed: the message says why."""


def expected_tools(server_py: Path) -> set[str]:
    """The tools server.py registers: each function under `@tool(...)` (its `name=` if it gives one)."""
    tree = ast.parse(server_py.read_text(encoding="utf-8"), filename=str(server_py))
    names = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name) and deco.func.id == "tool":
                named = [k.value.value for k in deco.keywords
                         if k.arg == "name" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)]
                names.add(named[0] if named else node.name)
    return names


def server_command(plugin_root: Path, mcp_json: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """The job-apply server's command line and env from .mcp.json, ${CLAUDE_PLUGIN_ROOT} filled in as Claude does."""
    path = mcp_json or plugin_root / ".mcp.json"
    try:
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        entry = servers[SERVER_NAME]
        command, args = entry["command"], list(entry.get("args", []))
        env = dict(entry.get("env", {}))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Failed(f"{path}: no mcpServers.{SERVER_NAME} with a command ({type(e).__name__}: {e})") from e

    def fill(text: str) -> str:
        text = str(text).replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
        if "${" in text:
            raise Failed(f"{path}: {text!r} names a variable this check doesn't fill")
        return text

    found = shutil.which(fill(command))
    if not found:
        raise Failed(f"{path}: its command {command!r} isn't on the PATH")
    return [found, *map(fill, args)], {k: fill(v) for k, v in env.items()}


def clean_env(home: Path) -> dict[str, str]:
    """This environment for a machine nothing was set up on: the home folder (HOME, USERPROFILE) and JOB_APPLY_HOME in
    the temporary folder, no Claude or Anthropic settings, no virtualenv of the caller's. uv keeps its own cache and
    Pythons where they were (so a new home doesn't mean downloading them again)."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("CLAUDE", "ANTHROPIC", "JOB_APPLY_HOME")) and k.upper() != "VIRTUAL_ENV"}
    uv = shutil.which("uv")
    for var, ask in (("UV_CACHE_DIR", ["cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["python", "dir"])):
        if uv and not env.get(var):
            try:
                said = subprocess.run([uv, *ask], capture_output=True, text=True, encoding="utf-8", timeout=60,
                                      env=env)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if said.returncode == 0 and said.stdout.strip():
                env[var] = said.stdout.strip()
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["JOB_APPLY_HOME"] = str(home / ".job-apply")
    return env


def _pump(stream: IO[str], into: queue.Queue[str | None]) -> None:
    for line in stream:
        into.put(line)
    into.put(None)


class Server:
    """The server as a child process, its output read on threads so a full pipe never stalls it."""

    def __init__(self, cmd: list[str], env: dict[str, str]) -> None:
        self.cmd = cmd
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.out: queue.Queue[str | None] = queue.Queue()
        self.err_lines: queue.Queue[str | None] = queue.Queue()
        self.readers = [threading.Thread(target=_pump, args=(stream, into), daemon=True)
                        for stream, into in ((self.proc.stdout, self.out), (self.proc.stderr, self.err_lines))]
        for reader in self.readers:
            reader.start()

    def stderr_tail(self, lines: int = 25) -> str:
        """The last lines the server wrote on stderr (once it has stopped)."""
        self.readers[1].join(timeout=5)
        said = []
        while True:
            try:
                line = self.err_lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                said.append(line.rstrip())
        return "\n".join(said[-lines:])

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise Failed(f"the server stopped taking input ({e})") from e

    def answer(self, request_id: int, deadline: float) -> dict[str, Any]:
        """The response to request_id (notifications and log lines the server sends first are passed over)."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Failed(f"no answer to request {request_id} in time")
            try:
                line = self.out.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                code = self.proc.wait(timeout=10)
                raise Failed(f"the server exited (code {code}) before answering request {request_id}")
            try:
                message = json.loads(line)
            except ValueError as e:
                raise Failed(f"the server wrote something that isn't JSON-RPC on stdout: {line.strip()[:200]!r}") from e
            if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise Failed(f"request {request_id} failed: {message['error']}")
                return dict(message.get("result") or {})

    def stop(self) -> None:
        """Input closed (a stdio server's cue to exit), then terminate, then kill."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        for stop in (None, self.proc.terminate, self.proc.kill):
            if stop:
                try:
                    stop()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                continue


def handshake(plugin_root: Path, env: dict[str, str], timeout: float, mcp_json: Path | None = None,
              server_py: Path | None = None) -> list[str]:
    """Start the server as .mcp.json says, list its tools and check them. Returns the tool names."""
    cmd, extra = server_command(plugin_root, mcp_json)
    expected = expected_tools(server_py or plugin_root / "server" / "src" / "job_apply" / "server.py")
    if len(expected) < 10:
        raise Failed(f"found only {len(expected)} @tool functions in server.py: has the decorator changed?")
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Failed(f"{manifest}: no version ({type(e).__name__}: {e})") from e
    print("Starting:", " ".join(cmd), flush=True)
    started = time.monotonic()
    try:
        server = Server(cmd, {**env, **extra, "CLAUDE_PLUGIN_ROOT": str(plugin_root)})
    except OSError as e:
        raise Failed(f"couldn't start {cmd[0]}: {e}") from e
    try:
        deadline = started + timeout
        server.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mcp_handshake", "version": "1"}}})
        init = server.answer(1, deadline)
        info = init.get("serverInfo") or {}
        print(f"initialize: {info.get('name')} {info.get('version')}, protocol {init.get('protocolVersion')}, "
              f"after {time.monotonic() - started:.1f} s", flush=True)
        if info.get("name") != SERVER_NAME or info.get("version") != version:
            raise Failed(f"the server says it's {info.get('name')} {info.get('version')}, "
                         f"not {SERVER_NAME} {version} (plugin.json)")
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = server.answer(2, deadline)
        names = [str(t.get("name")) for t in listed.get("tools", []) if isinstance(t, dict)]
        if listed.get("nextCursor"):
            raise Failed("tools/list came in pages: this check reads only the first")
        missing, extra_tools = sorted(expected - set(names)), sorted(set(names) - expected)
        if missing or extra_tools or len(names) != len(set(names)):
            raise Failed(f"tools/list doesn't match server.py's @tool functions: missing {missing}, "
                         f"not in server.py {extra_tools}, {len(names)} listed")
        print(f"tools/list: {len(names)} tools, as server.py registers them", flush=True)
        return names
    except Failed as e:
        server.stop()
        tail = server.stderr_tail()
        raise Failed(f"{e}" + (f"\nThe server's stderr (last lines):\n{tail}" if tail else "")) from None
    finally:
        server.stop()


def install(env: dict[str, str], marketplace: Path) -> Path:
    """`claude plugin marketplace add` and `install` as a person does (the home folder being env's): the installed
    copy's folder."""
    claude = shutil.which("claude")
    if not claude:
        raise Failed("the claude CLI isn't on the PATH (npm install -g @anthropic-ai/claude-code)")
    for args in (["plugin", "marketplace", "add", str(marketplace)], ["plugin", "install", PLUGIN_ID]):
        print("Running: claude", " ".join(args), flush=True)
        said = subprocess.run([claude, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=300, cwd=env["HOME"])
        print(said.stdout.strip(), flush=True)
        if said.returncode:
            raise Failed(f"claude {' '.join(args)} exited {said.returncode}: {said.stderr.strip()[-1500:]}")
    said = subprocess.run([claude, "plugin", "list", "--json"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120, cwd=env["HOME"])
    try:
        plugins = json.loads(said.stdout)
        root = Path(next(p["installPath"] for p in plugins if p.get("id") == PLUGIN_ID))
    except (ValueError, StopIteration, KeyError, TypeError) as e:
        raise Failed(f"claude plugin list --json doesn't show {PLUGIN_ID} ({type(e).__name__}): "
                     f"{said.stdout.strip()[:500]} {said.stderr.strip()[:500]}") from e
    print(f"Installed at {root}", flush=True)
    return root


def doctor(plugin_root: Path, env: dict[str, str]) -> None:
    """`job-apply-doctor --json` as the one-step installer runs it: valid JSON, and no problem but a clean machine's."""
    uv = shutil.which("uv")
    if not uv:
        raise Failed("uv isn't on the PATH")
    cmd = [uv, "run", "--quiet", "--frozen", "--project", str(plugin_root / "server"), "job-apply-doctor", "--json"]
    print("Running:", " ".join(cmd), flush=True)
    said = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                          timeout=300)
    try:
        report = json.loads(said.stdout)
        checks = {c["name"]: c for c in report["checks"]}
    except (ValueError, KeyError, TypeError) as e:
        raise Failed(f"the doctor (exit {said.returncode}) didn't print its JSON report ({type(e).__name__}): "
                     f"{said.stdout.strip()[-800:]}\n{said.stderr.strip()[-1500:]}") from e
    for check in checks.values():
        print(f"  {check['status']:8} {check['name']}: {check['say']}", flush=True)
    problems = {name for name, c in checks.items() if c["status"] == "problem"}
    wanted = {"python", "uv", "plugin", "browser", "folder", "profile", "resume"}
    if not wanted <= set(checks):
        raise Failed(f"the doctor's report lacks {sorted(wanted - set(checks))}")
    if problems - CLEAN_MACHINE_PROBLEMS:
        raise Failed(f"the doctor found problems a clean machine shouldn't have: {sorted(problems - CLEAN_MACHINE_PROBLEMS)}")
    if said.returncode != (1 if problems else 0) or report.get("ok") is not (not problems):
        raise Failed(f"the doctor exited {said.returncode} (ok: {report.get('ok')}) with problems {sorted(problems)}")
    print(f"doctor: exit {said.returncode}, the problems a clean machine has ({', '.join(sorted(problems)) or 'none'})",
          flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plugin-root", type=Path, default=PLUGIN, help="the plugin folder (default: this checkout's)")
    parser.add_argument("--mcp-json", type=Path, help="read the command from this file instead of the plugin's")
    parser.add_argument("--install", action="store_true",
                        help="first install the plugin from this checkout's marketplace with the claude CLI, and "
                             "check the installed copy")
    parser.add_argument("--doctor", action="store_true", help="then run job-apply-doctor --json and check its report")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds for the server to start and list its tools (the first start installs its "
                             "packages)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # (Windows' console is cp1252, and the CLI says "✔ Successfully added")
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")
    with tempfile.TemporaryDirectory(prefix="mcp-handshake-", ignore_cleanup_errors=True) as temp:
        env = clean_env(Path(temp))
        try:
            root = install(env, MARKETPLACE) if args.install else args.plugin_root.resolve()
            handshake(root, env, args.timeout, args.mcp_json)
            if args.doctor:
                doctor(root, env)
        except (Failed, subprocess.TimeoutExpired, OSError) as e:
            print(f"FAILED: {e}", file=sys.stderr, flush=True)
            return 1
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
