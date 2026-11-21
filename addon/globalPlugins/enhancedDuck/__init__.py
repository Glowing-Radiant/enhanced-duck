# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Enhanced audio ducking modes for NVDA.

NVDA ships with three audio ducking modes (none, when outputting, always).
This add-on applies configurable session-volume ducking to the outputting and
always modes, and adds two focus-aware modes that lower the per-application
volume of either the inactive windows or the active window. All four ducking
behaviors use the Windows Core Audio session API.

The two extra modes are wired into NVDA in several complementary ways so they work
whether the user changes the mode from the Audio settings panel, from the
``NVDA+shift+d`` command, or by switching configuration profiles:

* ``audioDucking.setAudioDuckingMode`` is wrapped so that the extra mode numbers
  start/stop our session ducker instead of being handed to NVDA (which only
  understands 0-2 and would raise ``ValueError``).
* ``audioDucking._setDuckingState`` is wrapped so NVDA's output reference count
  drives configurable session ducking instead of the fixed system effect.
* The registered ``post_configProfileSwitch`` handler is replaced with one that
  routes through the wrapper above.
* The Audio settings panel's ducking combo box is extended with the two extra
  entries.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable, Optional

import globalPluginHandler
from scriptHandler import script
from logHandler import log
import audioDucking
import config
import core
import globalVars
import ui

from .sessionDucker import (
	AdjustmentSteps,
	AudioDuckingConfig,
	DUCK_ACTIVE_WINDOWS,
	DUCK_ALL_APPLICATIONS,
	DUCK_INACTIVE_WINDOWS,
	MAX_TRANSITION_TIME_MS,
	MIN_DEPTH_ADJUSTMENT_STEP,
)
from .audioWorker import AudioWorkerSessionDucker
from .modeSettings import (
	getNextCycleMode,
	validateCycleModes,
)
from .lifecycle import shouldRestoreNativeDuckingOnTerminate
from .settings import (
	CONFIG_SECTION,
	_readAdjustmentSteps,
	_readCycleModes,
	_readDuckingConfig,
	_registerConfigSpec,
	_writeDuckingConfig,
)


# Mapping of every selectable mode number to its display label.
# The three built-in entries reuse NVDA's own translated strings; the enum
# members compare and hash equal to their integer values, so an ``int`` mode
# number can be used to look up any entry in this dict.
MODE_LABELS = {
	audioDucking.AudioDuckingMode.NONE: _("No ducking"),
	audioDucking.AudioDuckingMode.OUTPUTTING: _("Duck when outputting speech and sounds"),
	audioDucking.AudioDuckingMode.ALWAYS: _("Always duck"),
	# Translators: An enhanced audio ducking mode.
	DUCK_INACTIVE_WINDOWS: _("Duck inactive applications"),
	# Translators: An enhanced audio ducking mode.
	DUCK_ACTIVE_WINDOWS: _("Duck the active application"),
}

# The mode numbers that this add-on handles itself rather than NVDA.
CUSTOM_MODES = frozenset({DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS})
CONFIGURABLE_BUILT_IN_MODES = frozenset({
	int(audioDucking.AudioDuckingMode.OUTPUTTING),
	int(audioDucking.AudioDuckingMode.ALWAYS),
})
MODE_COUNT = len(MODE_LABELS)


def _isAudioDuckingSuspended():
	check = getattr(audioDucking, "_isAudioDuckingSuspended", None)
	return check is not None and check()


@dataclass
class _PatchState:
	setAudioDuckingMode: Callable
	setDuckingState: Callable
	unensureDucked: Callable
	initialDuckingDelay: float
	handlePostConfigProfileSwitch: Callable
	registeredProfileSwitchHandler: Optional[Callable] = None
	nativeProfileHandlerRemoved: bool = False
	audioPanelMakeSettings: Optional[Callable] = None
	audioPanelOnSave: Optional[Callable] = None
	settingsPanelClass: Optional[type] = None


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: The category shown for this add-on's commands in the Input Gestures dialog.
	scriptCategory = _("Audio")

	def __init__(self):
		super().__init__()
		_registerConfigSpec()
		recoveryPath = os.path.join(
			globalVars.appArgs.configPath,
			"enhancedDuck-volumeRecovery.json",
		)
		self._ducker = AudioWorkerSessionDucker(_readDuckingConfig(), recoveryPath)
		self._ducker.recoverStaleVolumes()
		self._patches = _PatchState(
			setAudioDuckingMode=audioDucking.setAudioDuckingMode,
			setDuckingState=audioDucking._setDuckingState,
			unensureDucked=audioDucking._unensureDucked,
			initialDuckingDelay=audioDucking.INITIAL_DUCKING_DELAY,
			handlePostConfigProfileSwitch=audioDucking.handlePostConfigProfileSwitch,
		)
		self._installPatches()
		self._applyConfiguredMode()

	def terminate(self):
		# Ask the COM-owning worker to restore volumes before unloading its hooks.
		try:
			configuredMode = int(config.conf["audio"]["audioDuckingMode"])
		except Exception:
			configuredMode = int(audioDucking.AudioDuckingMode.NONE)
		if not self._ducker.close():
			log.warning("Enhanced Duck audio worker did not stop within the shutdown timeout")
		restoreNativeDucking = shouldRestoreNativeDuckingOnTerminate(
			configuredMode,
			CONFIGURABLE_BUILT_IN_MODES,
			bool(getattr(core, "_hasShutdownBeenTriggered", False)) or _isAudioDuckingSuspended(),
		)
		if configuredMode in CONFIGURABLE_BUILT_IN_MODES:
			# Move NVDA's reference-counted state through NONE while our state
			# callback is still installed. After patch removal this lets the native
			# implementation start cleanly if the add-on is disabled or reloaded.
			self._setBuiltInMode(audioDucking.AudioDuckingMode.NONE)
		self._removePatches()
		if restoreNativeDucking:
			try:
				if audioDucking.isAudioDuckingSupported():
					self._patches.setAudioDuckingMode(configuredMode)
			except Exception:
				log.exception("Enhanced Duck: could not restore native ducking during unload")
		super().terminate()

	# --- Patch management ------------------------------------------------

	def _installPatches(self):
		plugin = self

		def setDuckingState(switch):
			"""Replace NVDA's fixed OS ducking with configurable session ducking."""
			if switch:
				plugin._ducker.start(DUCK_ALL_APPLICATIONS)
			else:
				plugin._ducker.stop()

		def unensureDucked(delay=True):
			"""End an output reference immediately and let releaseTimeMs provide the tail.

			NVDA normally holds every completed output reference for one second
			before calling its state callback. Keeping that hold would make the
			configured release time an additional delay rather than the complete
			recovery behavior.
			"""
			return plugin._patches.unensureDucked(delay=False)

		def setAudioDuckingMode(mode):
			mode = int(mode)
			plugin._ducker.configure(_readDuckingConfig(plugin._ducker.duckingConfig))
			if mode != audioDucking.AudioDuckingMode.NONE and _isAudioDuckingSuspended():
				return
			with audioDucking._duckingRefCountLock:
				if mode in CUSTOM_MODES:
					plugin._setBuiltInMode(audioDucking.AudioDuckingMode.NONE)
					plugin._ducker.start(mode)
					return
				plugin._setBuiltInMode(mode)
				isDucking = (
					mode == audioDucking.AudioDuckingMode.ALWAYS
					or (
						mode == audioDucking.AudioDuckingMode.OUTPUTTING
						and audioDucking._duckingRefCount > 0
					)
				)
				if isDucking:
					plugin._ducker.start(DUCK_ALL_APPLICATIONS)
				else:
					plugin._ducker.stop()

		def handlePostConfigProfileSwitch():
			if _isAudioDuckingSuspended():
				plugin._ducker.configure(_readDuckingConfig(plugin._ducker.duckingConfig))
			else:
				setAudioDuckingMode(config.conf["audio"]["audioDuckingMode"])

		# NVDA may already be ducking when add-ons load (notably in ALWAYS mode).
		# Clear that fixed native effect before replacing the state callback so it
		# cannot stack with our configurable session-volume reduction.
		with audioDucking._duckingRefCountLock:
			try:
				if audioDucking.isAudioDuckingSupported():
					self._patches.setDuckingState(False)
			except Exception:
				log.exception("Enhanced Duck: could not clear native ducking during startup")
			# AudioDucker.enable normally blocks speech for 150 ms while the native
			# Windows effect takes hold. Session ducking is asynchronous and has its
			# own configurable attack, so retaining that wait only adds latency.
			audioDucking.INITIAL_DUCKING_DELAY = 0.0
			audioDucking._setDuckingState = setDuckingState
			audioDucking._unensureDucked = unensureDucked
			audioDucking.setAudioDuckingMode = setAudioDuckingMode
		audioDucking.handlePostConfigProfileSwitch = handlePostConfigProfileSwitch
		self._replaceRegisteredProfileSwitchHandler(handlePostConfigProfileSwitch)
		self._patchAudioPanel()
		self._registerSettingsPanel()

	def _registerSettingsPanel(self):
		try:
			import wx
			from gui import guiHelper, nvdaControls, settingsDialogs
		except Exception:
			log.exception("Enhanced Duck: could not load the settings panel")
			return

		def addLabeledSpinCtrlDouble(panel, helper, labelText, **kwargs):
			"""Lay out a label and a ``wx.SpinCtrlDouble`` inside ``helper``.

			``guiHelper.addLabeledControl`` cannot build this pair.
			``guiHelper.associateElements`` dispatches on a fixed set of wx control
			types, and ``wx.SpinCtrlDouble`` is a sibling of ``wx.SpinCtrl`` rather
			than a subclass, so it matches no branch, leaves its ``sizer`` local
			unassigned and raises ``UnboundLocalError``. That aborts the whole panel,
			which in turn leaves NVDA's settings dialog half-built and unopenable.
			This mirrors the horizontal layout NVDA itself uses for a label and its
			associated control.
			"""
			label = wx.StaticText(panel, label=labelText)
			control = wx.SpinCtrlDouble(panel, **kwargs)
			sizer = wx.BoxSizer(wx.HORIZONTAL)
			sizer.Add(label, flag=wx.ALIGN_CENTER_VERTICAL)
			sizer.AddSpacer(guiHelper.SPACE_BETWEEN_ASSOCIATED_CONTROL_HORIZONTAL)
			sizer.Add(control)
			helper.addItem(sizer)
			return control

		class EnhancedDuckSettingsPanel(settingsDialogs.SettingsPanel):
			# Translators: The title of the Enhanced Duck settings category.
			title = _("Enhanced Duck")
			helpId = "AudioSettings"

			def makeSettings(panel, settingsSizer):
				settings = _readDuckingConfig()
				steps = _readAdjustmentSteps()
				helper = guiHelper.BoxSizerHelper(panel, sizer=settingsSizer)
				# Spin controls throughout: a screen reader user can arrow through
				# the range and hear each value, without having to type a decimal.
				# Translators: A multiplier from 0.00 to 1.00 for ducked application volume.
				panel.depthControl = addLabeledSpinCtrlDouble(
					panel,
					helper,
					_("Ducking volume &multiplier (0.00 to 1.00):"),
					min=0.0,
					max=1.0,
					inc=0.01,
					initial=settings.depth,
				)
				panel.depthControl.SetDigits(2)
				# Translators: How long the volume reduction transition takes, in milliseconds.
				panel.attackTimeControl = helper.addLabeledControl(
					_("&Attack time (milliseconds):"),
					nvdaControls.SelectOnFocusSpinCtrl,
					min=0,
					max=MAX_TRANSITION_TIME_MS,
					initial=settings.attackTimeMs,
				)
				# Translators: How long the volume restoration transition takes, in milliseconds.
				panel.releaseTimeControl = helper.addLabeledControl(
					_("&Release time (milliseconds):"),
					nvdaControls.SelectOnFocusSpinCtrl,
					min=0,
					max=MAX_TRANSITION_TIME_MS,
					initial=settings.releaseTimeMs,
				)
				# Translators: Step used by NVDA+minus and NVDA+equals to adjust ducking volume.
				panel.depthAdjustmentStepControl = addLabeledSpinCtrlDouble(
					panel,
					helper,
					_("Ducking volume adjustment &step (0.0001 to 1.0000):"),
					min=MIN_DEPTH_ADJUSTMENT_STEP,
					max=1.0,
					inc=0.01,
					initial=steps.depth,
				)
				panel.depthAdjustmentStepControl.SetDigits(4)
				# Translators: Step used by NVDA+Control+minus or equals to adjust attack time.
				panel.attackAdjustmentStepControl = helper.addLabeledControl(
					_("Attack adjustment ste&p (milliseconds):"),
					nvdaControls.SelectOnFocusSpinCtrl,
					min=1,
					max=MAX_TRANSITION_TIME_MS,
					initial=steps.attackTimeMs,
				)
				# Translators: Step used by NVDA+Alt+minus or equals to adjust release time.
				panel.releaseAdjustmentStepControl = helper.addLabeledControl(
					_("Release adjustment step (m&illiseconds):"),
					nvdaControls.SelectOnFocusSpinCtrl,
					min=1,
					max=MAX_TRANSITION_TIME_MS,
					initial=steps.releaseTimeMs,
				)
				panel.cycleModesList = helper.addLabeledControl(
					# Translators: Label for the checklist of modes used by NVDA+Shift+D.
					_("&Modes included when cycling with NVDA+Shift+D:"),
					nvdaControls.CustomCheckListBox,
					choices=[MODE_LABELS[mode] for mode in range(MODE_COUNT)],
				)
				for mode in _readCycleModes():
					panel.cycleModesList.Check(mode)
				panel.cycleModesList.SetSelection(0)

			def _getEnteredSettings(panel):
				return AudioDuckingConfig.fromValues(
					panel.depthControl.GetValue(),
					panel.attackTimeControl.GetValue(),
					panel.releaseTimeControl.GetValue(),
				)

			def _getEnteredAdjustmentSteps(panel):
				return AdjustmentSteps.fromValues(
					panel.depthAdjustmentStepControl.GetValue(),
					panel.attackAdjustmentStepControl.GetValue(),
					panel.releaseAdjustmentStepControl.GetValue(),
				)

			def _showValidationError(panel, message, title):
				import gui
				gui.messageBox(message, title, wx.OK | wx.ICON_ERROR, panel)

			def isValid(panel):
				if panel._getEnteredSettings() is None:
					panel._showValidationError(
						# Translators: Validation error for the ducking volume multiplier.
						_("Enter a number from 0.00 to 1.00, for example 0.25."),
						# Translators: Name of the invalid Enhanced Duck setting.
						_("Ducking volume multiplier"),
					)
					panel.depthControl.SetFocus()
					return False
				if panel._getEnteredAdjustmentSteps() is None:
					panel._showValidationError(
						# Translators: Validation error for shortcut adjustment step sizes.
						_("Enter a volume step from 0.0001 to 1.0000 and time steps from 1 to 60000 milliseconds."),
						# Translators: Name of the invalid Enhanced Duck settings.
						_("Keyboard adjustment steps"),
					)
					panel.depthAdjustmentStepControl.SetFocus()
					return False
				if not panel.cycleModesList.GetCheckedItems():
					panel._showValidationError(
						# Translators: Validation error when no cycling modes are enabled.
						_("Check at least one audio ducking mode."),
						# Translators: Name of the Enhanced Duck cycling modes setting.
						_("Modes included when cycling with NVDA+Shift+D"),
					)
					panel.cycleModesList.SetFocus()
					return False
				return True

			def onSave(panel):
				newSettings = panel._getEnteredSettings()
				steps = panel._getEnteredAdjustmentSteps()
				if newSettings is None or steps is None:
					log.warning("Enhanced Duck: settings panel rejected invalid values")
					return
				includedModes = validateCycleModes(
					panel.cycleModesList.GetCheckedItems(),
					MODE_COUNT,
				)
				if includedModes is None:
					log.warning("Enhanced Duck: settings panel rejected an empty cycle mode list")
					return
				section = config.conf[CONFIG_SECTION]
				_writeDuckingConfig(newSettings)
				section["depthAdjustmentStep"] = steps.depth
				section["attackAdjustmentStepMs"] = steps.attackTimeMs
				section["releaseAdjustmentStepMs"] = steps.releaseTimeMs
				section["includedModes"] = list(includedModes)
				plugin._ducker.configure(newSettings)

		try:
			settingsDialogs.NVDASettingsDialog.categoryClasses.append(EnhancedDuckSettingsPanel)
			self._patches.settingsPanelClass = EnhancedDuckSettingsPanel
		except Exception:
			log.exception("Enhanced Duck: could not register the settings panel")

	def _patchAudioPanel(self):
		plugin = self
		try:
			from gui import settingsDialogs
		except Exception:
			return
		audioPanel = getattr(settingsDialogs, "AudioPanel", None)
		if (
			audioPanel is None
			or not hasattr(audioPanel, "makeSettings")
			or not hasattr(audioPanel, "onSave")
		):
			return

		self._patches.audioPanelMakeSettings = audioPanel.makeSettings
		self._patches.audioPanelOnSave = audioPanel.onSave

		def makeSettings(panel, settingsSizer):
			realMode = config.conf["audio"]["audioDuckingMode"]
			# The stock panel builds a three-item combo box and immediately
			# selects the configured mode. A custom mode number would be an
			# out-of-range selection, so present NONE to the stock builder and
			# restore the real value (and selection) afterwards.
			clamped = realMode in CUSTOM_MODES
			if clamped:
				try:
					config.conf["audio"]["audioDuckingMode"] = int(audioDucking.AudioDuckingMode.NONE)
				except Exception:
					clamped = False
			try:
				plugin._patches.audioPanelMakeSettings(panel, settingsSizer)
			finally:
				if clamped:
					try:
						config.conf["audio"]["audioDuckingMode"] = realMode
					except Exception:
						pass
			try:
				duckingList = getattr(panel, "duckingList", None)
				if duckingList is not None:
					for mode in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS):
						duckingList.Append(MODE_LABELS[mode])
					# The custom modes use Core Audio sessions and remain available
					# when NVDA's built-in UI-access ducking is unsupported. Respect
					# NVDA's explicit temporary suspension state, however.
					if not _isAudioDuckingSuspended():
						duckingList.Enable()
					if 0 <= realMode < MODE_COUNT:
						duckingList.SetSelection(realMode)
			except Exception:
				log.exception("Enhanced Duck: could not extend the audio ducking settings")

		audioPanel.makeSettings = makeSettings

		def onSave(panel):
			selection = getattr(panel, "duckingList", None)
			selection = selection.GetSelection() if selection is not None else -1
			plugin._patches.audioPanelOnSave(panel)
			# The stock panel only saves a ducking choice when its built-in
			# implementation is supported. Persist our independent modes anyway.
			if (
				0 <= selection < MODE_COUNT
				and not audioDucking.isAudioDuckingSupported()
				and not _isAudioDuckingSuspended()
			):
				config.conf["audio"]["audioDuckingMode"] = selection
				audioDucking.setAudioDuckingMode(selection)

		audioPanel.onSave = onSave

	def _replaceRegisteredProfileSwitchHandler(self, handler):
		postSwitch = getattr(config, "post_configProfileSwitch", None)
		if postSwitch is None:
			return
		nativeHandler = self._patches.handlePostConfigProfileSwitch
		removed = postSwitch.unregister(nativeHandler)
		self._patches.nativeProfileHandlerRemoved = bool(removed)
		try:
			postSwitch.register(handler)
			# Keep a strong reference: extension points hold their handlers
			# weakly, so a bare closure would be collected and auto-unregistered.
			self._patches.registeredProfileSwitchHandler = handler
		except Exception:
			if self._patches.nativeProfileHandlerRemoved:
				postSwitch.register(nativeHandler)
			log.exception("Enhanced Duck: could not register its profile-switch handler")

	def _removePatches(self):
		self._restoreRegisteredProfileSwitchHandler()
		self._unregisterSettingsPanel()
		audioDucking.INITIAL_DUCKING_DELAY = self._patches.initialDuckingDelay
		audioDucking._unensureDucked = self._patches.unensureDucked
		audioDucking._setDuckingState = self._patches.setDuckingState
		audioDucking.setAudioDuckingMode = self._patches.setAudioDuckingMode
		audioDucking.handlePostConfigProfileSwitch = self._patches.handlePostConfigProfileSwitch
		if self._patches.audioPanelMakeSettings is not None:
			try:
				from gui import settingsDialogs
				settingsDialogs.AudioPanel.makeSettings = self._patches.audioPanelMakeSettings
				if self._patches.audioPanelOnSave is not None:
					settingsDialogs.AudioPanel.onSave = self._patches.audioPanelOnSave
			except Exception:
				pass

	def _unregisterSettingsPanel(self):
		panelClass = self._patches.settingsPanelClass
		if panelClass is None:
			return
		try:
			from gui import settingsDialogs
			settingsDialogs.NVDASettingsDialog.categoryClasses.remove(panelClass)
		except (ImportError, ValueError, AttributeError):
			pass
		finally:
			self._patches.settingsPanelClass = None

	def _restoreRegisteredProfileSwitchHandler(self):
		handler = self._patches.registeredProfileSwitchHandler
		if handler is None:
			return
		postSwitch = getattr(config, "post_configProfileSwitch", None)
		if postSwitch is None:
			return
		try:
			postSwitch.unregister(handler)
			if self._patches.nativeProfileHandlerRemoved:
				postSwitch.register(self._patches.handlePostConfigProfileSwitch)
		except Exception:
			pass
		finally:
			self._patches.registeredProfileSwitchHandler = None

	# --- Mode application ------------------------------------------------

	def _setBuiltInMode(self, mode):
		"""Set NVDA's built-in ducking mode, tolerating unsupported systems.

		The stock ``setAudioDuckingMode`` raises ``RuntimeError`` when ducking is
		not supported (portable copies, no UI access). The session-based custom
		modes do not depend on that support, so a failure here must not stop
		them from working.
		"""
		try:
			if (
				audioDucking.isAudioDuckingSupported()
				and audioDucking._audioDuckingMode != mode
			):
				self._patches.setAudioDuckingMode(mode)
		except Exception:
			log.exception("Enhanced Duck: could not set the built-in audio ducking mode")

	def _applyConfiguredMode(self):
		if _isAudioDuckingSuspended():
			return
		try:
			audioDucking.setAudioDuckingMode(config.conf["audio"]["audioDuckingMode"])
		except Exception:
			log.exception("Enhanced Duck: could not apply the configured audio ducking mode")
			self._ducker.stop()

	def _adjustDuckingConfig(
		self,
		depthDelta: float = 0.0,
		attackDeltaMs: int = 0,
		releaseDeltaMs: int = 0,
	) -> AudioDuckingConfig:
		"""Persist and immediately apply a bounded keyboard adjustment."""
		currentSettings = _readDuckingConfig(self._ducker.duckingConfig)
		newSettings = currentSettings.adjusted(
			depthDelta=depthDelta,
			attackDeltaMs=attackDeltaMs,
			releaseDeltaMs=releaseDeltaMs,
		)
		if newSettings != currentSettings:
			_writeDuckingConfig(newSettings, previous=currentSettings)
		# The ducker coalesces repeated keyboard adjustments and retargets the
		# active mode without tearing it down or enumerating Core Audio sessions
		# at keyboard-repeat speed.
		self._ducker.configure(newSettings)
		return newSettings

	# --- Commands --------------------------------------------------------

	@script(
		# Translators: Describes a command that lowers the ducked application volume.
		description=_("Makes audio ducking more aggressive"),
		category=scriptCategory,
		gesture="kb:NVDA+-",
	)
	def script_decreaseDuckingDepth(self, gesture):
		settings = self._adjustDuckingConfig(depthDelta=-_readAdjustmentSteps().depth)
		percentage = f"{settings.depth * 100:.2f}".rstrip("0").rstrip(".")
		# Translators: Reports the ducked application volume after a keyboard adjustment.
		ui.message(_("Ducking volume {percentage} percent").format(percentage=percentage))

	@script(
		# Translators: Describes a command that raises the ducked application volume.
		description=_("Makes audio ducking less aggressive"),
		category=scriptCategory,
		gesture="kb:NVDA+=",
	)
	def script_increaseDuckingDepth(self, gesture):
		settings = self._adjustDuckingConfig(depthDelta=_readAdjustmentSteps().depth)
		percentage = f"{settings.depth * 100:.2f}".rstrip("0").rstrip(".")
		# Translators: Reports the ducked application volume after a keyboard adjustment.
		ui.message(_("Ducking volume {percentage} percent").format(percentage=percentage))

	@script(
		# Translators: Describes a command that shortens the audio ducking attack.
		description=_("Decreases the audio ducking attack time"),
		category=scriptCategory,
		gesture="kb:NVDA+control+-",
	)
	def script_decreaseDuckingAttack(self, gesture):
		settings = self._adjustDuckingConfig(attackDeltaMs=-_readAdjustmentSteps().attackTimeMs)
		# Translators: Reports an audio ducking attack time in milliseconds.
		ui.message(_("Attack time {milliseconds} milliseconds").format(milliseconds=settings.attackTimeMs))

	@script(
		# Translators: Describes a command that lengthens the audio ducking attack.
		description=_("Increases the audio ducking attack time"),
		category=scriptCategory,
		gesture="kb:NVDA+control+=",
	)
	def script_increaseDuckingAttack(self, gesture):
		settings = self._adjustDuckingConfig(attackDeltaMs=_readAdjustmentSteps().attackTimeMs)
		# Translators: Reports an audio ducking attack time in milliseconds.
		ui.message(_("Attack time {milliseconds} milliseconds").format(milliseconds=settings.attackTimeMs))

	@script(
		# Translators: Describes a command that shortens the audio ducking release.
		description=_("Decreases the audio ducking release time"),
		category=scriptCategory,
		gesture="kb:NVDA+alt+-",
	)
	def script_decreaseDuckingRelease(self, gesture):
		settings = self._adjustDuckingConfig(releaseDeltaMs=-_readAdjustmentSteps().releaseTimeMs)
		# Translators: Reports an audio ducking release time in milliseconds.
		ui.message(_("Release time {milliseconds} milliseconds").format(milliseconds=settings.releaseTimeMs))

	@script(
		# Translators: Describes a command that lengthens the audio ducking release.
		description=_("Increases the audio ducking release time"),
		category=scriptCategory,
		gesture="kb:NVDA+alt+=",
	)
	def script_increaseDuckingRelease(self, gesture):
		settings = self._adjustDuckingConfig(releaseDeltaMs=_readAdjustmentSteps().releaseTimeMs)
		# Translators: Reports an audio ducking release time in milliseconds.
		ui.message(_("Release time {milliseconds} milliseconds").format(milliseconds=settings.releaseTimeMs))

	@script(
		# Translators: Describes the command that cycles audio ducking modes.
		description=_("Cycles through enhanced audio ducking modes"),
		category=scriptCategory,
		gesture="kb:NVDA+shift+d",
	)
	def script_cycleAudioDuckingMode(self, gesture):
		currentMode = config.conf["audio"]["audioDuckingMode"]
		nextMode = getNextCycleMode(currentMode, _readCycleModes())
		config.conf["audio"]["audioDuckingMode"] = nextMode
		if not _isAudioDuckingSuspended():
			audioDucking.setAudioDuckingMode(nextMode)
		ui.message(MODE_LABELS[nextMode])
