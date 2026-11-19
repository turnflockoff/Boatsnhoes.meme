from __future__ import annotations

import json

from .gpu_probe import probe_vulkan


if __name__ == "__main__":
    print(json.dumps(probe_vulkan(), indent=2))
