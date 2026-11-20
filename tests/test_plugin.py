import importlib.machinery
import importlib.util
import pathlib
import sys
import types
import unittest
from enum import IntEnum
from threading import RLock
from unittest.mock import Mock, patch

from test_sessionDucker import sessionDucker


class _Mode(IntEnum):
	NONE = 0
	OUTPUTTING = 1
	ALWAYS = 2


class _ProfileSection(dict):
	def isSet(self, key):
		return key in self

	def __missing__(self, key):
		return {"duckingDepth": 20, "attackTime": 150, "releaseTime": 600}[key]


class PluginTests(unittest.TestCase):
	def setUp(self):
		self.section = _ProfileSection(
			depthAdjustmentStep=0.025,
			attackAdjustmentStepMs=25,
			releaseAdjustmentStepMs=75,
		)
		config = types.ModuleType("config")
		config.conf = {"enhancedDuck": self.section, "audio": {"audioDuckingMode": _Mode.NONE}}
		config.post_configProfileSwitch = None

		audio = self.audio = types.ModuleType("audioDucking")
		audio.AudioDuckingMode = _Mode
		audio.INITIAL_DUCKING_DELAY = 0.15
		audio._duckingRefCountLock = RLock()
		audio._duckingRefCount = 0
		audio._audioDuckingMode = _Mode.NONE
		audio._setDuckingState = Mock()
		audio._unensureDucked = Mock(side_effect=self._endOutput)
		audio.setAudioDuckingMode = Mock(side_effect=self._setNativeMode)
		audio.handlePostConfigProfileSwitch = Mock()
		audio.isAudioDuckingSupported = Mock(return_value=True)
		audio._isAudioDuckingSuspended = Mock(return_value=False)
		self.nativeState = audio._setDuckingState
		self.nativeUnensureDucked = audio._unensureDucked
		self.nativeSetMode = audio.setAudioDuckingMode

		ui = self.ui = types.ModuleType("ui")
		ui.message = Mock()
		pluginHandler = types.ModuleType("globalPluginHandler")
		pluginHandler.GlobalPlugin = object
		scriptHandler = types.ModuleType("scriptHandler")
		scriptHandler.script = lambda **_kwargs: lambda script: script
		path = (
			pathlib.Path(__file__).parents[1] / "addon/globalPlugins/enhancedDuck/__init__.py"
		)
		name = f"{sessionDucker.__package__}.pluginUnderTest"
		loader = importlib.machinery.SourceFileLoader(name, str(path))
		spec = importlib.util.spec_from_loader(name, loader, is_package=False)
		plugin = self.module = importlib.util.module_from_spec(spec)
		plugin._ = lambda text: text
		with patch.dict(sys.modules, {
			name: plugin,
			"audioDucking": audio,
			"config": config,
			"globalPluginHandler": pluginHandler,
			"globalVars": types.ModuleType("globalVars"),
			"scriptHandler": scriptHandler,
			"ui": ui,
		}):
			spec.loader.exec_module(plugin)

		self.plugin = plugin.GlobalPlugin.__new__(plugin.GlobalPlugin)
		self.ducker = self.plugin._ducker = Mock()
		self.ducker.duckingConfig = plugin._readDuckingConfig()
		self.ducker.configure.side_effect = self._configure
		self.plugin._patches = plugin._PatchState(
			self.nativeSetMode,
			self.nativeState,
			self.nativeUnensureDucked,
			audio.INITIAL_DUCKING_DELAY,
			audio.handlePostConfigProfileSwitch,
		)
		self.plugin._patchAudioPanel = Mock()
		self.plugin._registerSettingsPanel = Mock()
		self.plugin._installPatches()

	def _configure(self, settings):
		self.ducker.duckingConfig = settings

	def _setNativeMode(self, mode):
		self.audio._audioDuckingMode = mode

	def _beginOutput(self):
		self.audio._duckingRefCount += 1
		if self.audio._duckingRefCount == 1:
			self.audio._setDuckingState(True)

	def _endOutput(self, delay=True):
		self.audio._duckingRefCount -= 1
		if not self.audio._duckingRefCount:
			self.audio._setDuckingState(False)

	def test_output_references_drive_configurable_ducking_without_native_hold(self):
		self.section.update(duckingDepth=25, attackTime=150, releaseTime=600)
		self.audio.setAudioDuckingMode(_Mode.OUTPUTTING)
		self.ducker.configure.assert_called_with(sessionDucker.AudioDuckingConfig(0.25, 150, 600))
		self.ducker.stop.reset_mock()
		self._beginOutput()
		self._beginOutput()
		self.ducker.start.assert_called_once_with(self.module.DUCK_ALL_APPLICATIONS)
		self.audio._unensureDucked()
		self.ducker.stop.assert_not_called()
		self.audio._unensureDucked()
		self.ducker.stop.assert_called_once_with()
		self.assertEqual(self.nativeUnensureDucked.call_count, 2)
		self.nativeUnensureDucked.assert_called_with(delay=False)
		self.nativeState.assert_called_once_with(False)
		self.assertEqual(self.audio.INITIAL_DUCKING_DELAY, 0)

	def test_always_applies_current_configuration_and_respects_suspension(self):
		self.section.update(duckingDepth=33, attackTime=200, releaseTime=300)
		self.audio.setAudioDuckingMode(_Mode.ALWAYS)
		self.ducker.configure.assert_called_with(sessionDucker.AudioDuckingConfig(0.33, 200, 300))
		self.ducker.start.assert_called_once_with(self.module.DUCK_ALL_APPLICATIONS)
		self.audio.setAudioDuckingMode(_Mode.NONE)
		self.audio._isAudioDuckingSuspended.return_value = True
		self.audio.setAudioDuckingMode(_Mode.ALWAYS)
		self.ducker.start.assert_called_once()
		self.ducker.stop.assert_called_once_with()

	def test_gestures_use_profile_steps_and_reconfigure_without_changing_mode(self):
		self.section.update(duckingDepth=25, attackTime=150, releaseTime=600)
		self.plugin.script_decreaseDuckingDepth(None)
		self.plugin.script_increaseDuckingAttack(None)
		self.plugin.script_decreaseDuckingRelease(None)
		self.assertEqual(self.section["duckingDepth"], 22.5)
		self.assertEqual(self.section["attackTime"], 175)
		self.assertEqual(self.section["releaseTime"], 525)
		self.ducker.configure.assert_called_with(sessionDucker.AudioDuckingConfig(0.225, 175, 525))
		self.ducker.start.assert_not_called()
		self.ducker.stop.assert_not_called()
		self.nativeSetMode.assert_not_called()
		self.assertEqual(self.ui.message.call_count, 3)

	def test_existing_snapshot_keys_are_read_and_invalid_settings_keep_safe_values(self):
		self.section.update(depth="0,35", attackTimeMs=0, releaseTimeMs=725)
		config = self.module._readDuckingConfig()
		self.assertEqual(config, sessionDucker.AudioDuckingConfig(0.35, 0, 725))
		self.section["releaseTimeMs"] = -5
		self.assertIs(self.module._readDuckingConfig(config), config)
