"""Update release-following YAML images while preserving frozen research runs."""
import json
from pathlib import Path
import re
import sys

IMAGE = re.compile(r'^(?P<indent>\s*)image:\s+ghcr\.io/strat-relay/(?P<name>[a-z0-9-]+)@sha256:[0-9a-f]+\s*$')
FROZEN = re.compile(r'^\s*#\s*stratrelay-release:\s*frozen\s*$', re.MULTILINE)
RESEARCH_PROCESS = re.compile(r'research\.(?:strategy_library_forward|library_event_service|ict_smc_forward|donchian_v2_forward)\b')


def repin(manifest, app_root):
    references = {image['name']: image['reference'] for image in manifest['images']}
    changed = []
    for path in sorted(Path(app_root).rglob('*.yaml')):
        original = path.read_text()
        # A file can contain a frozen ConfigMap, Deployment and immutable Jobs.
        # Keep the complete bundle bound to its approved artifact, not just the
        # observer container. Quote style must not determine experiment safety.
        if FROZEN.search(original) or RESEARCH_PROCESS.search(original):
            continue
        updated = []
        for line in original.splitlines(keepends=True):
            match = IMAGE.match(line.rstrip('\n'))
            reference = references.get(match.group('name')) if match else None
            if reference:
                line = f"{match.group('indent')}image: {reference}" + ('\n' if line.endswith('\n') else '')
            updated.append(line)
        result = ''.join(updated)
        if result != original:
            path.write_text(result)
            changed.append(str(path))
    return changed


if __name__ == '__main__':
    for path in repin(json.loads(Path(sys.argv[1]).read_text()), Path('apps') / sys.argv[2]):
        print(path)
