"""Bash tool: the meta-tool that gives the agent access to the shell.
"""

import shutil
import subprocess
import sys

from harness.tools.filesystem import WORKSPACE
from harness.tools.registry import tool


# Per-command timeout. Longer than git's 10s because bash covers real
# work (installs, network calls, running scripts) that legitimately
# takes 30-60 seconds.
BASH_TIMEOUT = 60

IS_WINDOWS = sys.platform == "win32"

# Windows: prefer PowerShell 7 (pwsh) if installed, else the built-in
# Windows PowerShell. Linux/macOS: plain bash.
if IS_WINDOWS:
    SHELL = shutil.which("pwsh") or "powershell.exe"
else:
    SHELL = "bash"


def _shell_note() -> str:
    """Describe the host OS and shell so the model writes matching commands."""
    if IS_WINDOWS:
        return (
            "Host OS: Windows. Commands run in PowerShell, so use PowerShell "
            "syntax and cmdlets (Get-ChildItem, Get-Content, Select-String, "
            "Remove-Item), not Unix commands like ls -la, grep, or cat flags. "
            "Chain commands with ; (not &&), use $env:VAR for environment "
            "variables, and use relative paths."
        )
    os_name = "macOS" if sys.platform == "darwin" else "Linux"
    return f"Host OS: {os_name}. Commands run in bash, so use Unix/bash syntax."


def _combine_output(stdout: str, stderr: str) -> str:
    """Combine stdout and stderr into a single string for the model.

    stdout comes first (usually the primary output). stderr is appended
    when non-empty with a marker so the model can tell them apart without
    us structuring the return as a dict.
    """
    # Step 1: normalize whitespace on both streams.
    stdout = stdout.strip()
    stderr = stderr.strip()

    # Step 2: return only what's non-empty; add a marker between them
    # so the model can distinguish output stream from error stream.
    if stdout and stderr:
        return f"{stdout}\n--- stderr ---\n{stderr}"
    return stdout or stderr


def bash(command: str) -> str:
    # Step 0: build the argument list for the host's shell.
    if IS_WINDOWS:
        # Force UTF-8 output so non-ASCII text isn't garbled by the
        # Windows OEM code page.
        full_command = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
        args = [SHELL, "-NoProfile", "-NonInteractive", "-Command", full_command]
    else:
        args = [SHELL, "-c", command]

    try:
        # Step 1: run the command with the workspace as the working directory.
        result = subprocess.run(
            args,
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            encoding="utf-8",   # don't depend on the Windows locale encoding
            errors="replace",   # bad bytes become ? instead of raising
            timeout=BASH_TIMEOUT,
            check=False,  # non-zero exits are surfaced as text, not raised
        )
    except subprocess.TimeoutExpired:
        # Step 2a: on timeout, tell the model explicitly what happened and
        # what its next move should be.
        return (
            f"[bash timeout after {BASH_TIMEOUT}s] Command was killed. "
            f"If this was expected to take longer, consider running it "
            f"in pieces or breaking the work up."
        )

    # Step 2b: assemble the output the model will see.
    output = _combine_output(result.stdout, result.stderr) or "(no output)"

    # Step 3: prepend a failure marker on non-zero exit, so the model
    # can see success/failure at a glance without parsing exit codes.
    if result.returncode != 0:
        return f"[bash exit {result.returncode}] {output}"

    return output


# The docstring is built at import time so it can include the OS note,
# then the function is registered by calling the decorator manually.
bash.__doc__ = f"""Execute a shell command in the workspace and return its combined output.

{_shell_note()}

The command runs with the workspace as the working directory.

Use this tool for anything the filesystem or git tools don't cover: running
scripts, invoking system utilities, installing packages (pip install ...),
or executing code the agent has written to a file (python script.py,
node script.js).

Returns stdout followed by stderr (when non-empty). Exit codes other
than 0 are surfaced with a [bash exit N] prefix so the model can
recognize failures and recover.
"""
bash = tool(bash)