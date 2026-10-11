"""Restricted file and process tools for the model loop.

Every tool is confined to one task workspace. Tool subprocesses get a fixed
minimal environment, so model/Agent/Eyes credentials are never inherited.
Trusted-code assumption: the workspace Python can read what the container user
can read; this is documented isolation, not a security sandbox.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from trae_agent.runtime import RunRuntime
from trae_agent.workspace import Workspace, WorkspaceError, check_logical_name

MAX_LIST_ENTRIES = 1000
MAX_READ_BYTES = 65_536
MAX_WRITE_BYTES = 262_144
MAX_OUTPUT_CAPTURE = 8_192
MAX_RUN_TIMEOUT = 60.0
TRUNCATION_MARKER = "\n...[truncated]"


class ToolError(Exception):
    """Raised for rejected calls; the message is returned to the model verbatim."""


def _tool_payload(ok: bool, **fields) -> str:
    return json.dumps({"ok": ok, **fields}, ensure_ascii=False)


def list_files(workspace: Workspace, runtime: RunRuntime, args: dict) -> str:
    runtime.tool_calls += 1
    files = workspace.list_files(limit=MAX_LIST_ENTRIES)
    return _tool_payload(True, files=files, count=len(files))


def read_file(workspace: Workspace, runtime: RunRuntime, args: dict) -> str:
    runtime.tool_calls += 1
    name = _require_name(args)
    max_bytes = args.get("max_bytes", MAX_READ_BYTES)
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_READ_BYTES:
        raise ToolError(f"max_bytes must be an integer in [1, {MAX_READ_BYTES}]")
    content = workspace.read_text(name, max_bytes)
    return _tool_payload(True, path=name, content=content)


def write_file(workspace: Workspace, runtime: RunRuntime, args: dict) -> str:
    runtime.tool_calls += 1
    name = _require_name(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(f"content exceeds the {MAX_WRITE_BYTES} byte write limit")
    target = workspace.resolve(name)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(target.name + ".tmp-write")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return _tool_payload(True, path=name, bytes=len(data))


def replace_text(workspace: Workspace, runtime: RunRuntime, args: dict) -> str:
    runtime.tool_calls += 1
    name = _require_name(args)
    old, new = args.get("old"), args.get("new")
    if not isinstance(old, str) or old == "":
        raise ToolError("old must be a non-empty string")
    if not isinstance(new, str):
        raise ToolError("new must be a string")
    expected = args.get("expected_count", 1)
    if not isinstance(expected, int) or expected < 1:
        raise ToolError("expected_count must be a positive integer")
    target = workspace.resolve(name)
    if not target.is_file():
        raise ToolError(f"not a file: {name}")
    text = target.read_bytes().decode("utf-8", errors="strict")
    count = text.count(old)
    if count != expected:
        raise ToolError(
            f"old appears {count} time(s) but expected_count={expected}; nothing was replaced"
        )
    target.write_bytes(text.replace(old, new).encode("utf-8"))
    return _tool_payload(True, path=name, replaced=count, bytes=len(new))


def run_python(workspace: Workspace, runtime: RunRuntime, args: dict) -> str:
    runtime.tool_calls += 1
    name = args.get("file")
    if not isinstance(name, str):
        raise ToolError("file must be a string naming a Python file in the workspace")
    check_logical_name(name)
    script = workspace.resolve(name)
    if not script.is_file():
        raise ToolError(f"not a file: {name}")
    script_args = args.get("args", [])
    if not isinstance(script_args, list) or not all(isinstance(a, str) for a in script_args):
        raise ToolError("args must be a list of strings")
    timeout = args.get("timeout_seconds", runtime.tool_timeout_seconds)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_RUN_TIMEOUT:
        raise ToolError(f"timeout_seconds must be in (0, {MAX_RUN_TIMEOUT}]")

    command = [_python_executable(), str(script), *script_args]
    process = subprocess.Popen(
        command,
        cwd=str(workspace.root),
        env=_child_env(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    runtime.register_process(process)
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        stdout, stderr = process.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        return _tool_payload(
            True,
            path=name,
            exit_code=process.returncode,
            stdout=_bounded(stdout or b""),
            stderr=_bounded(stderr or b""),
            duration_ms=duration_ms,
            timed_out=True,
        )
    finally:
        runtime.forget_process(process)
    duration_ms = int((time.monotonic() - started) * 1000)
    return _tool_payload(
        True,
        path=name,
        exit_code=process.returncode,
        stdout=_bounded(stdout or b""),
        stderr=_bounded(stderr or b""),
        duration_ms=duration_ms,
        timed_out=False,
    )


def _python_executable() -> str:
    import sys

    return sys.executable or "python3"


def _child_env(workspace: Workspace) -> dict[str, str]:
    tmp = workspace.root / ".tmp"
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workspace.root),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _kill_process_group(process) -> None:
    import os
    import signal

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _bounded(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_CAPTURE:
        return text[:MAX_OUTPUT_CAPTURE] + TRUNCATION_MARKER
    return text


def _require_name(args: dict) -> str:
    name = args.get("path")
    if not isinstance(name, str):
        raise ToolError("path must be a string")
    try:
        check_logical_name(name)
    except WorkspaceError as error:
        raise ToolError(str(error)) from None
    return name


TOOLS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "replace_text": replace_text,
    "run_python": run_python,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the task workspace (relative paths).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the task workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "max_bytes": {
                        "type": "integer",
                        "description": f"Max bytes to return, default {MAX_READ_BYTES}",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a file in the task workspace with UTF-8 text "
                f"(max {MAX_WRITE_BYTES} bytes)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": (
                "Replace exact text occurrences in a workspace file. Refuses to write unless "
                "the number of occurrences equals expected_count (default 1)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "old": {"type": "string", "description": "Exact text to replace"},
                    "new": {"type": "string", "description": "Replacement text"},
                    "expected_count": {
                        "type": "integer",
                        "description": "Required occurrence count, default 1",
                    },
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run a Python file from the workspace with argv arguments and return the real "
                "exit code, stdout and stderr."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Relative path of the .py file"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "argv strings passed to the script",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": f"Wall-clock limit in (0, {MAX_RUN_TIMEOUT}]",
                    },
                },
                "required": ["file"],
                "additionalProperties": False,
            },
        },
    },
]
