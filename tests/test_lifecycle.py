import importlib.util
import pathlib
import sys
import unittest


def _loadLifecycle():
	path = (
		pathlib.Path(__file__).parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "enhancedDuck"
		/ "lifecycle.py"
	)
	spec = importlib.util.spec_from_file_location("enhancedDuck_lifecycle_test", path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


lifecycle = _loadLifecycle()


class LifecycleTests(unittest.TestCase):
	def test_native_ducking_is_not_restored_during_nvda_shutdown(self):
		self.assertFalse(
			lifecycle.shouldRestoreNativeDuckingOnTerminate(1, {1, 2}, True),
		)

	def test_native_ducking_is_restored_during_live_plugin_reload(self):
		self.assertTrue(
			lifecycle.shouldRestoreNativeDuckingOnTerminate(1, {1, 2}, False),
		)

	def test_custom_mode_never_activates_native_ducking(self):
		self.assertFalse(
			lifecycle.shouldRestoreNativeDuckingOnTerminate(3, {1, 2}, False),
		)


if __name__ == "__main__":
	unittest.main()
