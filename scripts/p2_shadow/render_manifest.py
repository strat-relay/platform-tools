from __future__ import annotations

import os
from pathlib import Path


REPLACEMENTS = {
    "REPLACE_SOURCE_SHA": "P2_SOURCE_SHA",
    "REPLACE_DEPS_SHA": "P2_DEPS_SHA",
    "REPLACE_SOURCE_COMMIT": "P2_SOURCE_COMMIT",
    "REPLACE_SOURCE_BUNDLE_SHA": "P2_SOURCE_BUNDLE_SHA",
    "REPLACE_RUNNER_IMAGE": "P2_RUNNER_IMAGE",
    "REPLACE_RUNNER_GENERATION": "P2_RUNNER_GENERATION",
    "REPLACE_RUNNER_CONFIG_SHA256": "P2_RUNNER_CONFIG_SHA256",
}


def render(template: Path) -> str:
    text = template.read_text(encoding="utf-8")
    for placeholder, variable in REPLACEMENTS.items():
        value = os.environ.get(variable)
        if not value:
            raise ValueError(f"required render variable missing: {variable}")
        text = text.replace(placeholder, value)
    if "REPLACE_" in text:
        raise ValueError("unrendered deployment placeholder remains")
    return text


if __name__ == "__main__":
    import sys
    sys.stdout.write(render(Path(sys.argv[1])))
