"""The install-and-start check (scripts/mcp_handshake.py): the real server started as .mcp.json says lists the tools
server.py registers, and a broken command, a server that won't import, one that's missing a tool, says another
version or never answers each fail the check."""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("mcp_handshake", SCRIPTS / "mcp_handshake.py")
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)

SERVER_PY = hs.PLUGIN / "server" / "src" / "job_apply" / "server.py"
VERSION = json.loads((hs.PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def mcp_json(tmp_path: Path, command: str, args: list[str]) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"job-apply": {"command": command, "args": args}}}), encoding="utf-8")
    return path


def real_args(entry_point: str = "job-apply-mcp") -> list[str]:
    """The plugin's own .mcp.json args, with another entry point."""
    args = json.loads((hs.PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["job-apply"]["args"]
    return [entry_point if a == "job-apply-mcp" else a for a in args]


def test_the_expected_tools_are_read_from_server_py(tmp_path):
    names = hs.expected_tools(SERVER_PY)
    assert len(names) >= 30
    assert {"ingest_job", "open_application", "autofill", "fill_form", "submit_application"} <= names
    source = tmp_path / "server.py"
    source.write_text("@tool()\ndef a(): ...\n@tool(drives=True, name='bee')\nasync def b(): ...\n"
                      "@other()\ndef c(): ...\ndef d(): ...\n", encoding="utf-8")
    assert hs.expected_tools(source) == {"a", "bee"}


def test_the_real_server_starts_as_mcp_json_says_and_lists_its_tools(capsys):
    assert hs.main([]) == 0
    out = capsys.readouterr().out
    assert f"initialize: job-apply {VERSION}" in out
    assert f"tools/list: {len(hs.expected_tools(SERVER_PY))} tools" in out


def test_an_entry_point_that_isnt_there_fails(tmp_path, capsys):
    assert hs.main(["--mcp-json", str(mcp_json(tmp_path, "uv", real_args("job-apply-mpc")))]) == 1
    err = capsys.readouterr().err
    assert "FAILED: the server exited" in err and "job-apply-mpc" in err


def test_a_server_that_fails_to_import_fails(tmp_path, capsys, monkeypatch):
    shadow = tmp_path / "shadow" / "job_apply"  # found before the installed package: as if it had a broken import
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise ImportError('a module the server needs is broken')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shadow.parent))
    assert hs.main([]) == 1
    err = capsys.readouterr().err
    assert "FAILED: the server exited" in err and "a module the server needs is broken" in err


def test_a_command_that_isnt_on_the_path_fails(tmp_path, capsys):
    assert hs.main(["--mcp-json", str(mcp_json(tmp_path, "uv-that-isnt-there", real_args()))]) == 1
    assert "isn't on the PATH" in capsys.readouterr().err


FAKE_SERVER = """\
import json, sys
mode, names, version = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
for line in sys.stdin:
    message = json.loads(line)
    if mode == "silent" or "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "job-apply", "version": "0.0.1" if mode == "version" else version}}
    else:
        result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.mark.parametrize("mode, said", [
    ("missing", "doesn't match server.py's @tool functions: missing ['submit_application']"),
    ("version", "not job-apply " + VERSION),
    ("silent", "no answer to request 1 in time"),
])
def test_a_server_that_answers_wrong_fails_and_is_stopped(tmp_path, capsys, mode, said):
    fake = tmp_path / "fake_server.py"
    fake.write_text(FAKE_SERVER, encoding="utf-8")
    names = sorted(hs.expected_tools(SERVER_PY) - ({"submit_application"} if mode == "missing" else set()))
    started = time.monotonic()
    assert hs.main(["--mcp-json",
                    str(mcp_json(tmp_path, sys.executable, [str(fake), mode, json.dumps(names), VERSION])),
                    "--timeout", "3"]) == 1
    assert time.monotonic() - started < 30  # the silent one stopped after its 3 s, not left running
    assert said in capsys.readouterr().err


def test_a_fake_server_that_answers_right_passes(tmp_path):
    """(The fake above, answering as the real one does: so the failures above are the check's, not the fake's.)"""
    fake = tmp_path / "fake_server.py"
    fake.write_text(FAKE_SERVER, encoding="utf-8")
    names = sorted(hs.expected_tools(SERVER_PY))
    path = mcp_json(tmp_path, sys.executable, [str(fake), "right", json.dumps(names), VERSION])
    assert hs.main(["--mcp-json", str(path), "--timeout", "10"]) == 0


def test_save_tools_writes_the_tools_as_listed_by_name_for_the_evals_mocks(tmp_path):
    """--save-tools writes the tools/list answer the skills evals' stand-in shows Claude (evals/mocks/job-apply/
    _tools.json): {"tools": [{name, description, inputSchema}]}, by name, nothing else of the server's."""
    fake = tmp_path / "fake_server.py"
    fake.write_text(FAKE_SERVER, encoding="utf-8")
    names = sorted(hs.expected_tools(SERVER_PY), reverse=True)  # (listed out of order: written by name)
    path = mcp_json(tmp_path, sys.executable, [str(fake), "right", json.dumps(names), VERSION])
    saved = tmp_path / "evals" / "_tools.json"
    assert hs.main(["--mcp-json", str(path), "--timeout", "10", "--save-tools", str(saved)]) == 0
    tools = json.loads(saved.read_text(encoding="utf-8"))["tools"]
    assert [t["name"] for t in tools] == sorted(names)
    assert all(set(t) == {"name", "inputSchema"} for t in tools)  # (the fake gives no descriptions)


def test_what_the_claude_cli_says_is_shown_on_a_console_that_cant_show_it(tmp_path):
    """Windows' CI console is cp1252, and `claude plugin marketplace add` says "✔ Successfully added…": printing it
    stopped the check with a UnicodeEncodeError before the plugin was installed (the first Windows run). Shown with
    what the console can't show replaced, and the check goes on (here to a stand-in CLI's refused install)."""
    import os
    import subprocess

    fake = tmp_path / "fake_claude.py"
    fake.write_text("import sys\nif sys.argv[1:3] == ['plugin', 'marketplace']:\n"
                    "    sys.stdout.buffer.write('\\u2714 Successfully added\\n'.encode('utf-8'))\n    sys.exit(0)\n"
                    "sys.stderr.write('refused')\nsys.exit(3)\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        stub = bin_dir / "claude"
        stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "PYTHONIOENCODING": "cp1252"}
    said = subprocess.run([sys.executable, str(SCRIPTS / "mcp_handshake.py"), "--install"], capture_output=True,
                          env=env, timeout=120)
    err = said.stderr.decode("utf-8", "replace")
    assert said.returncode == 1 and "UnicodeEncodeError" not in err, err
    assert "FAILED: claude plugin install" in err and "refused" in err, err
    assert "Successfully added" in said.stdout.decode("cp1252", "replace")
