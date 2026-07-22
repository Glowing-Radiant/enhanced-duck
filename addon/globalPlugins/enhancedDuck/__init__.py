# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Enhanced audio ducking modes for NVDA.

NVDA ships with three audio ducking modes (none, when outputting, always).
This add-on adds two focus-aware modes that lower the per-application volume of
either the inactive windows or the active window, implemented on top of the
Windows Core Audio session API.

The two extra modes are wired into NVDA in three complementary ways so they work
whether the user changes the mode from the Audio settings panel, from the
``NVDA+shift+d`` command, or by switching configuration profiles:

* ``audioDucking.setAudioDuckingMode`` is wrapped so that the extra mode numbers
  start/stop our session ducker instead of being handed to NVDA (which only
  understands 0-2 and would raise ``ValueError``).
* The registered ``post_configProfileSwitch`` handler is replaced with one that
  routes through the wrapper above.
* The Audio settings panel's ducking combo box is extended with the two extra
  entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import globalPluginHandler
from scriptHandler import script
from logHandler import log
import audioDucking
import config
import ui

from .sessionDucker import (
	DUCK_ACTIVE_WINDOWS,
	DUCK_INACTIVE_WINDOWS,
	FocusAwareSessionDucker,
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
MODE_COUNT = len(MODE_LABELS)


@dataclass
class _PatchState:
	setAudioDuckingMode: Callable
	handlePostConfigProfileSwitch: Callable
	registeredProfileSwitchHandler: Optional[Callable] = None
	audioPanelMakeSettings: Optional[Callable] = None


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: The category shown for this add-on's commands in the Input Gestures dialog.
	scriptCategory = _("Audio")

	def __init__(self):
		super().__init__()
		self._ducker = FocusAwareSessionDucker()
		self._patches = _PatchState(
			setAudioDuckingMode=audioDucking.setAudioDuckingMode,
			handlePostConfigProfileSwitch=audioDucking.handlePostConfigProfileSwitch,
		)
		self._installPatches()
		self._applyConfiguredMode()

	def terminate(self):
		self._ducker.stop()
		self._removePatches()
		super().terminate()

	# --- Patch management ------------------------------------------------

	def _installPatches(self):
		plugin = self

		def setAudioDuckingMode(mode):
			mode = int(mode)
			if mode in CUSTOM_MODES:
				# Our session ducker replaces NVDA's ducking, so make sure the
				# built-in WASAPI ducking is off while a custom mode is active.
				plugin._setBuiltInMode(audioDucking.AudioDuckingMode.NONE)
				plugin._ducker.start(mode)
				return
			plugin._ducker.stop()
			plugin._setBuiltInMode(mode)

		def handlePostConfigProfileSwitch():
			setAudioDuckingMode(config.conf["audio"]["audioDuckingMode"])

		audioDucking.setAudioDuckingMode = setAudioDuckingMode
		audioDucking.handlePostConfigProfileSwitch = handlePostConfigProfileSwitch
		self._replaceRegisteredProfileSwitchHandler(handlePostConfigProfileSwitch)
		self._patchAudioPanel()

	def _patchAudioPanel(self):
		plugin = self
		try:
			from gui import settingsDialogs
		except Exception:
			return
		audioPanel = getattr(settingsDialogs, "AudioPanel", None)
		if audioPanel is None or not hasattr(audioPanel, "makeSettings"):
			return

		self._patches.audioPanelMakeSettings = audioPanel.makeSettings

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
					if 0 <= realMode < MODE_COUNT:
						duckingList.SetSelection(realMode)
			except Exception:
				log.exception("Enhanced Duck: could not extend the audio ducking settings")

		audioPanel.makeSettings = makeSettings

	def _replaceRegisteredProfileSwitchHandler(self, handler):
		postSwitch = getattr(config, "post_configProfileSwitch", None)
		if postSwitch is None:
			return
		try:
			postSwitch.unregister(self._patches.handlePostConfigProfileSwitch)
		except Exception:
			return
		try:
			postSwitch.register(handler)
			# Keep a strong reference: extension points hold their handlers
			# weakly, so a bare closure would be collected and auto-unregistered.
			self._patches.registeredProfileSwitchHandler = handler
		except Exception:
			try:
				postSwitch.register(self._patches.handlePostConfigProfileSwitch)
			except Exception:
				pass
			self._patches.registeredProfileSwitchHandler = None

	def _removePatches(self):
		self._restoreRegisteredProfileSwitchHandler()
		audioDucking.setAudioDuckingMode = self._patches.setAudioDuckingMode
		audioDucking.handlePostConfigProfileSwitch = self._patches.handlePostConfigProfileSwitch
		if self._patches.audioPanelMakeSettings is not None:
			try:
				from gui import settingsDialogs
				settingsDialogs.AudioPanel.makeSettings = self._patches.audioPanelMakeSettings
			except Exception:
				pass

	def _restoreRegisteredProfileSwitchHandler(self):
		handler = self._patches.registeredProfileSwitchHandler
		if handler is None:
			return
		postSwitch = getattr(config, "post_configProfileSwitch", None)
		if postSwitch is None:
			return
		try:
			postSwitch.unregister(handler)
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
			if audioDucking.isAudioDuckingSupported():
				self._patches.setAudioDuckingMode(mode)
		except Exception:
			log.exception("Enhanced Duck: could not set the built-in audio ducking mode")

	def _applyConfiguredMode(self):
		try:
			audioDucking.setAudioDuckingMode(config.conf["audio"]["audioDuckingMode"])
		except Exception:
			log.exception("Enhanced Duck: could not apply the configured audio ducking mode")
			self._ducker.stop()

	# --- Commands --------------------------------------------------------

	@script(
		# Translators: Describes the command that cycles audio ducking modes.
		description=_("Cycles through enhanced audio ducking modes"),
		category=scriptCategory,
		gesture="kb:NVDA+shift+d",
	)
	def script_cycleAudioDuckingMode(self, gesture):
		currentMode = config.conf["audio"]["audioDuckingMode"]
		if currentMode < 0 or currentMode >= MODE_COUNT:
			currentMode = 0
		nextMode = (currentMode + 1) % MODE_COUNT
		config.conf["audio"]["audioDuckingMode"] = nextMode
		audioDucking.setAudioDuckingMode(nextMode)
		ui.message(MODE_LABELS[nextMode])
