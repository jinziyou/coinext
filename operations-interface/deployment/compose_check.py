#!/usr/bin/env python3
"""Validate Coinext compose overlays without starting containers.

This is the dependency-light equivalent of `just compose-check`: it works even when the `just`
binary is not installed. If `.env` is absent, it seeds a temporary copy from `.env.example` because
compose services declare `env_file: .env`; the temporary file is removed before exit.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_COMBOS = (
    ("base", ("docker-compose.yml",)),
    ("dev", ("docker-compose.yml", "docker-compose.dev.yml")),
    ("obs", ("docker-compose.yml", "docker-compose.obs.yml")),
    ("dev+obs", ("docker-compose.yml", "docker-compose.dev.yml", "docker-compose.obs.yml")),
)


def _compose_command(files: tuple[str, ...]) -> list[str]:
    cmd = ["docker", "compose"]
    for file_name in files:
        cmd.extend(("-f", file_name))
    cmd.extend(("config", "--quiet"))
    return cmd


def main() -> int:
    env_path = ROOT / ".env"
    created_env = False
    if not env_path.exists():
        shutil.copyfile(ROOT / ".env.example", env_path)
        created_env = True

    try:
        for label, files in COMPOSE_COMBOS:
            proc = subprocess.run(
                _compose_command(files),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if proc.returncode != 0:
                sys.stderr.write(proc.stdout)
                sys.stderr.write(f"compose check failed: {label}\n")
                return proc.returncode
            print(f"compose OK: {label}")
    finally:
        if created_env:
            env_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
