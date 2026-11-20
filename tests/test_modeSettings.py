import importlib.util
import pathlib
import sys
import unittest


def _loadModeSettings():
	path = (
		pathlib.Path(__file__).parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "enhancedDuck"
		/ "modeSettings.py"
	)
	spec = importlib.util.spec_from_file_location("enhancedDuck_modeSettings_test", path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


modeSettings = _loadModeSettings()


class ModeSettingsTests(unittest.TestCase):
	def test_validation_returns_unique_modes_in_display_order(self):
		self.assertEqual(
			modeSettings.validateCycleModes([4, "1", 3, 1]),
			(1, 3, 4),
		)

	def test_validation_rejects_empty_or_invalid_lists(self):
		for value in ([], [5], [-1], [1.5], [True], "0, 1"):
			with self.subTest(value=value):
				self.assertIsNone(modeSettings.validateCycleModes(value))

	def test_cycle_uses_only_included_modes_and_wraps(self):
		included = (0, 2, 4)
		self.assertEqual(modeSettings.getNextCycleMode(0, included), 2)
		self.assertEqual(modeSettings.getNextCycleMode(2, included), 4)
		self.assertEqual(modeSettings.getNextCycleMode(4, included), 0)

	def test_cycle_starts_at_first_mode_when_current_is_excluded(self):
		self.assertEqual(modeSettings.getNextCycleMode(3, (1, 4)), 1)

	def test_cycle_rejects_an_empty_mode_list(self):
		with self.assertRaises(ValueError):
			modeSettings.getNextCycleMode(0, ())


if __name__ == "__main__":
	unittest.main()
