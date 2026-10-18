from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlatformApiImageTests(unittest.TestCase):
    def test_startup_from_only_files_copied_into_api_image(self):
        dockerfile = (ROOT / 'deploy/platform_api/Dockerfile').read_text()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for line in dockerfile.splitlines():
                if not line.startswith('COPY '):
                    continue
                *sources, destination = shlex.split(line)[1:]
                for source in sources:
                    src, dst = ROOT / source, target / destination.removeprefix('./')
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True,
                                        ignore=shutil.ignore_patterns('__pycache__'))
                    else:
                        if destination.endswith('/'):
                            dst = dst / src.name
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
            env = {k: v for k, v in os.environ.items()
                   if k not in ('PYTHONPATH', 'TRADING_POSTGRES_DSN')}
            result = subprocess.run([sys.executable, '-c',
                "from platform_api.signals import UnifiedPlatformApi; "
                "from core.strategies.families import family_for; "
                "assert family_for('ICT_JUDAS_SWING') == 'ICT'; UnifiedPlatformApi()"],
                cwd=target, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_image_carries_import_time_outcome_attribution_dependency(self):
        dockerfile = (ROOT / "deploy/platform_api/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY outcome_attribution.py ./outcome_attribution.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
