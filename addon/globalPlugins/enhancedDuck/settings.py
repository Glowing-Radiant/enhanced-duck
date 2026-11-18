# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Configuration and settings panel for the focus-aware ducking modes.

Three parameters decide what ducking sounds like, and all three are stored in
NVDA's own configuration, so they can differ per configuration profile:

* the *depth*: the volume a ducked application is lowered to, expressed as a
  percentage of the volume that application is set to in the Windows volume
  mixer, so an application the user has already turned down stays relatively
  quieter rather than being pulled up to a fixed level;
* the *attack* time: how long lowering the volume to that level takes;
* the *release* time: how long returning to the original volume takes.

A time of zero means "change the volume at once", which is how the add-on
behaved before these settings existed.
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel
from logHandler import log


#: The section of NVDA's configuration that holds this add-on's settings.
CONFIG_SECTION = "enhancedDuck"

#: Volume a ducked application is lowered to, as a percentage of its own volume.
DEFAULT_DEPTH_PERCENT = 20
DEFAULT_ATTACK_TIME_MS = 150
DEFAULT_RELEASE_TIME_MS = 600

MIN_RAMP_TIME_MS = 0
# Long enough for a deliberately slow fade, short enough that a mistyped value
# cannot leave an application quiet for what sounds like forever.
MAX_RAMP_TIME_MS = 5000

confspec = {
	"duckingDepth": "integer(default={default}, min=0, max=100)".format(default=DEFAULT_DEPTH_PERCENT),
	"attackTime": "integer(default={default}, min={min}, max={max})".format(
		default=DEFAULT_ATTACK_TIME_MS, min=MIN_RAMP_TIME_MS, max=MAX_RAMP_TIME_MS
	),
	"releaseTime": "integer(default={default}, min={min}, max={max})".format(
		default=DEFAULT_RELEASE_TIME_MS, min=MIN_RAMP_TIME_MS, max=MAX_RAMP_TIME_MS
	),
}


def initialiseConfig():
	"""Add this add-on's section to NVDA's configuration specification."""
	config.conf.spec[CONFIG_SECTION] = confspec


@dataclass(frozen=True)
class DuckingSettings:
	#: Scalar (0.0 - 1.0) a ducked application's own volume is multiplied by.
	depth: float
	#: Time taken to lower the volume to the ducked level, in milliseconds.
	attackTimeMs: int
	#: Time taken to return to the original volume, in milliseconds.
	releaseTimeMs: int


DEFAULT_SETTINGS = DuckingSettings(
	depth=DEFAULT_DEPTH_PERCENT / 100.0,
	attackTimeMs=DEFAULT_ATTACK_TIME_MS,
	releaseTimeMs=DEFAULT_RELEASE_TIME_MS,
)


def _clampPercentage(value) -> int:
	return min(100, max(0, int(value)))


def _clampRampTime(value) -> int:
	return min(MAX_RAMP_TIME_MS, max(MIN_RAMP_TIME_MS, int(value)))


def getDuckingSettings() -> DuckingSettings:
	"""Return the configured ducking parameters.

	Values are clamped rather than trusted: the configuration file can be edited
	by hand, and a depth outside 0-100 would be written straight to an audio
	session's volume. A section that cannot be read at all (a configuration
	written by a newer version of the add-on, say) falls back to the defaults, so
	ducking keeps working instead of the mode failing outright.
	"""
	try:
		section = config.conf[CONFIG_SECTION]
		return DuckingSettings(
			depth=_clampPercentage(section["duckingDepth"]) / 100.0,
			attackTimeMs=_clampRampTime(section["attackTime"]),
			releaseTimeMs=_clampRampTime(section["releaseTime"]),
		)
	except Exception:
		log.exception("Enhanced Duck: could not read the ducking settings")
		return DEFAULT_SETTINGS


class EnhancedDuckSettingsPanel(SettingsPanel):
	# Translators: The title of the Enhanced Duck category in NVDA's settings dialog.
	title = _("Enhanced Duck")

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		# Read through the same accessor the ducker uses, so an unreadable
		# configuration shows the defaults instead of failing to build the panel.
		current = getDuckingSettings()

		# Translators: The label for a setting in the Enhanced Duck settings panel.
		depthLabel = _("Ducking &depth, as a percentage of an application's own volume")
		self.depthSlider = sHelper.addLabeledControl(
			depthLabel, nvdaControls.EnhancedInputSlider, minValue=0, maxValue=100
		)
		self.depthSlider.SetValue(round(current.depth * 100))

		# Translators: The label for a setting in the Enhanced Duck settings panel.
		attackLabel = _("&Attack time, in milliseconds (time taken to lower the volume)")
		self.attackEdit = sHelper.addLabeledControl(
			attackLabel,
			nvdaControls.SelectOnFocusSpinCtrl,
			min=MIN_RAMP_TIME_MS,
			max=MAX_RAMP_TIME_MS,
			initial=current.attackTimeMs,
		)

		# Translators: The label for a setting in the Enhanced Duck settings panel.
		releaseLabel = _("&Release time, in milliseconds (time taken to restore the volume)")
		self.releaseEdit = sHelper.addLabeledControl(
			releaseLabel,
			nvdaControls.SelectOnFocusSpinCtrl,
			min=MIN_RAMP_TIME_MS,
			max=MAX_RAMP_TIME_MS,
			initial=current.releaseTimeMs,
		)

	def onSave(self):
		section = config.conf[CONFIG_SECTION]
		section["duckingDepth"] = _clampPercentage(self.depthSlider.GetValue())
		section["attackTime"] = _clampRampTime(self.attackEdit.GetValue())
		section["releaseTime"] = _clampRampTime(self.releaseEdit.GetValue())
