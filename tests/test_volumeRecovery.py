import importlib.util
import json
import pathlib
import sys
import tempfile
import time
import unittest


def _loadVolumeRecovery():
	path = (
		pathlib.Path(__file__).parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "enhancedDuck"
		/ "volumeRecovery.py"
	)
	spec = importlib.util.spec_from_file_location("enhancedDuck_volumeRecovery_test", path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


volumeRecovery = _loadVolumeRecovery()


class VolumeRecoveryStoreTests(unittest.TestCase):
	def test_records_reloads_and_discards_original_volume(self):
		with tempfile.TemporaryDirectory() as temporaryDirectory:
			path = str(pathlib.Path(temporaryDirectory) / "recovery.json")
			key = (42, "audio-session")
			store = volumeRecovery.VolumeRecoveryStore(path)
			store.record(key, 0.8)

			reloaded = volumeRecovery.VolumeRecoveryStore(path)
			self.assertAlmostEqual(reloaded.get(key), 0.8)
			reloaded.discard(key)
			self.assertFalse(volumeRecovery.VolumeRecoveryStore(path))

	def test_invalid_and_expired_entries_are_ignored(self):
		with tempfile.TemporaryDirectory() as temporaryDirectory:
			path = pathlib.Path(temporaryDirectory) / "recovery.json"
			payload = {
				"version": volumeRecovery.RECOVERY_FILE_VERSION,
				"entries": [
					{
						"pid": 42,
						"sessionId": "expired",
						"volume": 0.8,
						"savedAt": time.time() - 1000,
					},
					{"pid": -1, "sessionId": "invalid", "volume": 2, "savedAt": time.time()},
				],
			}
			path.write_text(json.dumps(payload), encoding="utf-8")
			store = volumeRecovery.VolumeRecoveryStore(str(path), maxAgeSeconds=10)
			self.assertFalse(store)


if __name__ == "__main__":
	unittest.main()
