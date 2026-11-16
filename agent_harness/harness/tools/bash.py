"""Bash tool: the meta-tool that gives the agent access to the shell.
"""

import shutil
import subprocess
import sys
from pathlib import PurePath

from harness.tools.filesystem import WORKSPACE
from harness.tools.registry import tool
from harness.config import BASH_TIMEOUT, ALLOW_LIST, DENY_LIST


IS_WINDOWS = sys.platform == "win32"

# Windows: prefer PowerShell 7 (pwsh) if installed, else the built-in
# Windows PowerShell. Linux/macOS: plain bash.
if IS_WINDOWS:
    SHELL = shutil.which("pwsh") or "powershell.exe"
else:
    SHELL = "bash"

# The allow-list, deny-list and timeout now live in harness/config.py.
# Tokens are lowercased before matching, so normalize the configured
# entries the same way. That way "Get-ChildItem" in config still works.
_ALLOW = {name.lower() for name in ALLOW_LIST}
_DENY = {name.lower() for name in DENY_LIST}

# Characters that separate chained commands in bash. We check the first
# token of every segment, so `pip install foo && rm -rf /` gets both
# `pip` and `rm` checked, not just `pip`.
# "\n" is included because a newline also chains commands.
_CHAIN_SEPARATORS = ("&&", "||", ";", "|", "\n")


def _first_token(segment: str) -> str:
    """Return the normalized first whitespace-separated token of a segment.

    Normalized = lowercase, directory stripped, .exe stripped, so that
    `/bin/rm`, `Python.exe` and `python` all match their plain names.
    """
    segment = segment.strip()
    if not segment:
        return ""
    token = segment.split()[0]
    name = PurePath(token.replace("\\", "/")).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _segments(command: str) -> list[str]:
    """Split a shell command on chain separators into its segments.

    This is a first-token-checker helper, not a full shell parser. It
    won't catch every edge case (backticks, $(...), complex quoting)
    but it handles the common chain patterns.
    """
    segments = [command]
    for sep in _CHAIN_SEPARATORS:
        segments = [
            piece
            for seg in segments
            for piece in seg.split(sep)
        ]
    return segments


def _check_policy(command: str) -> str | None:
    """Check the command against the allow-list and deny-list.

    Returns None if the command is permitted, or an error string if not.
    Deny-list wins on conflict — a command on both lists is rejected.
    """
    # Step 1: extract the first token from every chain segment.
    tokens = [_first_token(s) for s in _segments(command)]
    tokens = [t for t in tokens if t]  # drop empty segments

    # Step 2: deny-list check first (deny wins).
    if _DENY:
        for token in tokens:
            if token in _DENY:
                return (
                    f"[policy] '{token}' is on the deny-list — refusing to run."
                )

    # Step 3: allow-list check (empty = permissive, so skip when empty).
    if _ALLOW:
        for token in tokens:
            if token not in _ALLOW:
                return (
                    f"[policy] '{token}' is not on the allow-list — "
                    f"refusing to run. Allow-list currently permits: "
                    f"{', '.join(sorted(_ALLOW))}."
                )

    return None


def _shell_note() -> str:
    """Describe the host OS and shell so the model writes matching commands."""
    if IS_WINDOWS:
        return (
            "Host OS: Windows. Commands run in PowerShell, so use PowerShell "
            "syntax and cmdlets (Get-ChildItem, Get-Content, Select-String), "
            "not Unix commands like ls -la, grep, or cat flags. Chain "
            "commands with ; (not &&), use $env:VAR for environment "
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
    # Policy check before touching subprocess. Fast rejection —
    # no shell invoked, no side effects, just the error string returned
    # to the model so it can construct a different command.
    policy_error = _check_policy(command)
    if policy_error:
        return policy_error

    # Build the argument list for the host's shell.
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


# The docstring is built at import time so it can include the OS note and
# the policy, then the function is registered by calling the decorator manually.
_allowed = ", ".join(sorted(_ALLOW)) if _ALLOW else "any command"
_denied = ", ".join(sorted(_DENY)) if _DENY else "none"

bash.__doc__ = f"""Execute a shell command in the workspace and return its combined output.

{_shell_note()}

The command runs with the workspace as the working directory.

Commands are subject to a policy. Allowed commands: {_allowed}. Denied
commands: {_denied} (deny wins over allow). The first word of every chained
segment is checked. If the policy refuses a command, an error is returned
instead of executing it, so choose a different command.

Use this tool for anything the filesystem or git tools don't cover: running
scripts, invoking allowed utilities, or executing code the agent has
written to a file (python script.py).

Returns stdout followed by stderr (when non-empty). Exit codes other
than 0 are surfaced with a [bash exit N] prefix so the model can
recognize failures and recover.
"""
bash = tool(bash)