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

	def test_fake_guiHelper_rejects_SpinCtrlDouble_like_NVDA_does(self):
		"""Guard the guard: the fake must reproduce NVDA's own limitation.

		NVDA's ``guiHelper.associateElements`` dispatches on a fixed set of wx
		control types. ``wx.SpinCtrlDouble`` is a sibling of ``wx.SpinCtrl``,
		not a subclass, so it matches no branch and the real helper raises
		``UnboundLocalError``. If this stops raising, the panel test below
		would pass for the wrong reason.
		"""
		fakes = _makeFakeGui()
		wx = fakes["wx"]
		helper = fakes["gui.guiHelper"].BoxSizerHelper(object(), sizer=wx.BoxSizer(wx.VERTICAL))
		helper.addLabeledControl("supported:", wx.SpinCtrl, min=0, max=10, initial=1)
		with self.assertRaises(UnboundLocalError):
			helper.addLabeledControl("unsupported:", wx.SpinCtrlDouble, min=0.0, max=1.0, initial=0.5)

	def test_settings_panel_builds_all_controls(self):
		"""The Enhanced Duck category must build without raising.

		A failure here aborts ``_getCategoryPanel`` and leaves NVDA's settings
		dialog half-built, after which it cannot be reopened at all.
		"""
		fakes = _makeFakeGui()
		wx = fakes["wx"]
		settingsDialogs = fakes["gui.settingsDialogs"]
		with patch.dict(sys.modules, fakes):
			self.module.GlobalPlugin._registerSettingsPanel(self.plugin)
		# _registerSettingsPanel swallows import errors, so prove it got that far.
		self.assertEqual(len(settingsDialogs.NVDASettingsDialog.categoryClasses), 1)
		panelClass = settingsDialogs.NVDASettingsDialog.categoryClasses[0]
		self.assertIs(self.plugin._patches.settingsPanelClass, panelClass)

		panel = panelClass.__new__(panelClass)
		settingsSizer = wx.BoxSizer(wx.VERTICAL)
		panelClass.makeSettings(panel, settingsSizer)

		self.assertIsInstance(panel.depthControl, wx.SpinCtrlDouble)
		self.assertIsInstance(panel.depthAdjustmentStepControl, wx.SpinCtrlDouble)
		self.assertEqual(panel.depthControl.GetValue(), 0.2)
		self.assertEqual(panel.depthAdjustmentStepControl.GetValue(), 0.025)
		# Every row must pair its control with a label in a horizontal sizer,
		# including the two rows the add-on lays out itself.
		for control in (
			panel.depthControl,
			panel.attackTimeControl,
			panel.depthAdjustmentStepControl,
			panel.cycleModesList,
		):
			rows = [
				row for row in settingsSizer.children
				if isinstance(row, wx.BoxSizer) and control in row.children
			]
			self.assertEqual(len(rows), 1, control)
			self.assertEqual(rows[0].GetOrientation(), wx.HORIZONTAL)
			self.assertTrue(
				any(isinstance(item, wx.StaticText) for item in rows[0].children),
				control,
			)
		for name in (
			"attackTimeControl",
			"releaseTimeControl",
			"attackAdjustmentStepControl",
			"releaseAdjustmentStepControl",
		):
			self.assertIsInstance(getattr(panel, name), wx.SpinCtrl, name)
		self.assertEqual(len(panel.cycleModesList.choices), self.module.MODE_COUNT)
		self.assertTrue(panel.cycleModesList.GetCheckedItems())


def _makeFakeGui():
	"""Build stand-ins for ``wx`` and NVDA's ``gui`` modules.

	``associateElements`` mirrors the real dispatch in NVDA's ``gui.guiHelper``,
	including its failure mode, so the settings panel is exercised against the
	same type restrictions it meets at runtime.
	"""
	wx = types.ModuleType("wx")
	wx.VERTICAL = 8
	wx.HORIZONTAL = 4
	wx.ALIGN_CENTER_VERTICAL = 2048

	class Sizer:
		def __init__(self, orient=wx.VERTICAL):
			self.orient = orient
			self.children = []

		def Add(self, item, **kwargs):
			self.children.append(item)
			return item

		def AddSpacer(self, space):
			self.children.append(space)

		def GetOrientation(self):
			return self.orient

	class Window:
		def __init__(self, parent=None, **kwargs):
			self.parent = parent
			self.kwargs = kwargs

		def SetFocus(self):
			pass

	class StaticText(Window):
		def __init__(self, parent=None, label="", **kwargs):
			super().__init__(parent, label=label, **kwargs)
			self.label = label

	class _ValueCtrl(Window):
		def __init__(self, parent=None, **kwargs):
			super().__init__(parent, **kwargs)
			self.value = kwargs.get("initial", 0)

		def GetValue(self):
			return self.value

	class SpinCtrl(_ValueCtrl):
		pass

	# A sibling of SpinCtrl in wxPython, not a subclass. That is the whole bug.
	class SpinCtrlDouble(_ValueCtrl):
		def __init__(self, parent=None, **kwargs):
			super().__init__(parent, **kwargs)
			self.digits = None

		def SetDigits(self, digits):
			self.digits = digits

	class ListBox(Window):
		def __init__(self, parent=None, choices=(), **kwargs):
			super().__init__(parent, choices=choices, **kwargs)
			self.choices = list(choices)
			self.checked = set()
			self.selection = -1

		def Check(self, index, check=True):
			if check:
				self.checked.add(index)
			else:
				self.checked.discard(index)

		def GetCheckedItems(self):
			return tuple(sorted(self.checked))

		def SetSelection(self, index):
			self.selection = index

	class TextCtrl(Window):
		pass

	class Choice(Window):
		pass

	class CheckBox(Window):
		pass

	for cls in (Window, StaticText, SpinCtrl, SpinCtrlDouble, ListBox, TextCtrl, Choice, CheckBox):
		setattr(wx, cls.__name__, cls)
	wx.BoxSizer = Sizer

	# --- gui.guiHelper ---------------------------------------------------
	guiHelper = types.ModuleType("gui.guiHelper")
	guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL = 10
	guiHelper.SPACE_BETWEEN_VERTICAL_DIALOG_ITEMS = 10
	_horizontalCtrlTypes = (Choice, ListBox, TextCtrl, SpinCtrl, CheckBox)

	def associateElements(firstElement, secondElement):
		if isinstance(firstElement, StaticText) and isinstance(secondElement, _horizontalCtrlTypes):
			sizer = Sizer(wx.HORIZONTAL)
			sizer.Add(firstElement, flag=wx.ALIGN_CENTER_VERTICAL)
			sizer.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
			sizer.Add(secondElement)
			return sizer
		# NVDA's own helper assigns no sizer on this path and falls off the end.
		raise UnboundLocalError(
			"cannot access local variable 'sizer' where it is not associated with a value"
		)

	guiHelper.associateElements = associateElements

	class BoxSizerHelper:
		def __init__(self, parent, sizer=None, orientation=None):
			self.parent = parent
			self.sizer = sizer if sizer is not None else Sizer(orientation or wx.VERTICAL)

		def addLabeledControl(self, labelText, wxCtrlClass, **kwargs):
			label = StaticText(self.parent, label=labelText)
			control = wxCtrlClass(self.parent, **kwargs)
			self.sizer.Add(associateElements(label, control))
			return control

		def addItem(self, item, **kwargs):
			if self.sizer.GetOrientation() == wx.HORIZONTAL:
				raise NotImplementedError("horizontal BoxSizerHelper not implemented")
			self.sizer.Add(item)
			return item

	guiHelper.BoxSizerHelper = BoxSizerHelper

	# --- gui.nvdaControls ------------------------------------------------
	nvdaControls = types.ModuleType("gui.nvdaControls")

	class SelectOnFocusSpinCtrl(SpinCtrl):
		pass

	class CustomCheckListBox(ListBox):
		pass

	nvdaControls.SelectOnFocusSpinCtrl = SelectOnFocusSpinCtrl
	nvdaControls.CustomCheckListBox = CustomCheckListBox

	# --- gui.settingsDialogs ---------------------------------------------
	settingsDialogs = types.ModuleType("gui.settingsDialogs")

	class SettingsPanel:
		pass

	class NVDASettingsDialog:
		categoryClasses = []

	settingsDialogs.SettingsPanel = SettingsPanel
	settingsDialogs.NVDASettingsDialog = NVDASettingsDialog

	gui = types.ModuleType("gui")
	gui.__path__ = []
	gui.guiHelper = guiHelper
	gui.nvdaControls = nvdaControls
	gui.settingsDialogs = settingsDialogs

	return {
		"wx": wx,
		"gui": gui,
		"gui.guiHelper": guiHelper,
		"gui.nvdaControls": nvdaControls,
		"gui.settingsDialogs": settingsDialogs,
	}
