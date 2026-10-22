import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('repin', Path(__file__).parents[1] / 'scripts/repin_gitops_images.py')
repin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repin)

OLD = 'ghcr.io/strat-relay/trading-platform-runtime@sha256:' + '1' * 64
NEW = 'ghcr.io/strat-relay/trading-platform-runtime@sha256:' + '2' * 64
MANIFEST = {'images': [{'name': 'trading-platform-runtime', 'reference': NEW}]}


class FrozenReleaseTest(unittest.TestCase):
    def check_bundle(self, text, should_change):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bundle.yaml'
            path.write_text(text)
            changed = repin.repin(MANIFEST, directory)
            self.assertEqual(bool(changed), should_change)
            self.assertEqual(path.read_text(), text.replace(OLD, NEW) if should_change else text)
            self.assertEqual(repin.repin(MANIFEST, directory), [])

    def test_frozen_unquoted_bundle_including_init_and_job_is_untouched(self):
        self.check_bundle(f'# stratrelay-release: frozen\nkind: Deployment\nimage: {OLD}\n---\nkind: Job\nimage: {OLD}\n', False)

    def test_observer_without_marker_is_automatically_frozen(self):
        for process in ['strategy_library_forward', 'library_event_service', 'ict_smc_forward', 'donchian_v2_forward']:
            with self.subTest(process=process):
                self.check_bundle(f'image: {OLD}\ncommand: [python, -m, research.{process}]\n', False)

    def test_release_following_collector_still_updates(self):
        self.check_bundle(f'image: {OLD}\ncommand: [python, -m, market_data_cache.service]\n', True)

    def test_quoted_frozen_pin_stays_untouched(self):
        self.check_bundle(f'image: "{OLD}"\n', False)


if __name__ == '__main__':
    unittest.main()
