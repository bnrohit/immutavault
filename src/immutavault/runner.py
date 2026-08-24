from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Iterable


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def run(
    command: Iterable[str],
    *,
    timeout: int = 3600,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> CommandResult:
    cmd = [str(x) for x in command]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=merged_env,
        check=False,
        input=input_text,
    )
    result = CommandResult(cmd, proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        safe = " ".join(cmd)
        raise RuntimeError(f"command failed ({proc.returncode}): {safe}\n{proc.stderr.strip()}")
    return result
