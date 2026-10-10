"""Run the complete regression batch inside the AstrBot Python environment."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    package = root.name
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(root.parent), str(Path.cwd()), env.get("PYTHONPATH", ""),
    )))
    suites = (
        ("unit", ["-m", "unittest", "discover", "-s", "tests"]),
        ("astrbot", ["-m", package + ".checks.astrbot_smoke"]),
        ("delivery", ["-m", package + ".checks.result_delivery_smoke"]),
    )
    total = 0
    for name, arguments in suites:
        result = subprocess.run(
            [sys.executable, *arguments], cwd=root, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        output = result.stdout + result.stderr
        if result.returncode:
            print(f"{name}: FAILED")
            print(output)
            return result.returncode
        count = re.search(r"Ran (\d+) tests?", output)
        if not count:
            print(f"{name}: missing unittest summary")
            return 1
        total += int(count.group(1))
        print(f"{name}: {count.group(1)} tests passed")
    print(f"ALL PASSED: {total} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
