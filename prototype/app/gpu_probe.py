from __future__ import annotations

import shutil
import subprocess
from typing import Any


def probe_vulkan() -> dict[str, Any]:
    executable = shutil.which("vulkaninfo")
    if not executable:
        return {"available": False, "reason": "vulkaninfo is not installed; install inspection tools first"}
    try:
        result = subprocess.run(
            [executable, "--summary"], check=False, capture_output=True,
            text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": result.returncode == 0,
        "returncode": result.returncode,
        "summary": result.stdout[-12000:],
        "stderr": result.stderr[-4000:],
        "note": "Device enumeration is not proof of successful AI inference."
    }
