# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Profile configuration shared by the settings panel and keyboard commands."""

from __future__ import annotations

import config
from logHandler import log

from .modeSettings import DEFAULT_CYCLE_MODES, validateCycleModes
from .sessionDucker import (
	AdjustmentSteps,
	AudioDuckingConfig,
	DEFAULT_ADJUSTMENT_STEPS,
	MAX_TRANSITION_TIME_MS,
	MIN_DEPTH_ADJUSTMENT_STEP,
)


DEFAULT_DUCKING_CONFIG = AudioDuckingConfig(0.2, 150, 600)

CONFIG_SECTION = "enhancedDuck"
CONFIG_SPEC = {
	"duckingDepth": "float(default=20.0, min=0.0, max=100.0)",
	"attackTime": f"integer(default=150, min=0, max={MAX_TRANSITION_TIME_MS})",
	"releaseTime": f"integer(default=600, min=0, max={MAX_TRANSITION_TIME_MS})",
	"depthAdjustmentStep": f"float(default=0.05, min={MIN_DEPTH_ADJUSTMENT_STEP}, max=1.0)",
	"attackAdjustmentStepMs": f"integer(default=50, min=1, max={MAX_TRANSITION_TIME_MS})",
	"releaseAdjustmentStepMs": f"integer(default=50, min=1, max={MAX_TRANSITION_TIME_MS})",
	"includedModes": "int_list(default=list(0, 1, 2, 3, 4))",
}


def _registerConfigSpec():
	config.conf.spec[CONFIG_SECTION] = CONFIG_SPEC


def _getProfileValue(section, key, developmentKey):
	if section.isSet(developmentKey) and not section.isSet(key):
		return section[developmentKey]
	return section[key]


def _readDuckingConfig(
	fallback: AudioDuckingConfig = DEFAULT_DUCKING_CONFIG,
) -> AudioDuckingConfig:
	"""Read and defensively validate the current profile's settings."""
	try:
		section = config.conf[CONFIG_SECTION]
		# Existing releases store a percentage. Also accept the scalar and
		# explicit millisecond keys used by the development builds.
		if section.isSet("depth") and not section.isSet("duckingDepth"):
			depth = section["depth"]
		else:
			depth = float(section["duckingDepth"]) / 100.0
		duckingConfig = AudioDuckingConfig.fromValues(
			depth,
			_getProfileValue(section, "attackTime", "attackTimeMs"),
			_getProfileValue(section, "releaseTime", "releaseTimeMs"),
		)
		if duckingConfig is not None:
			return duckingConfig
	except Exception:
		pass
	log.warning("Enhanced Duck: invalid configuration; keeping the last safe settings")
	return fallback


def _readCycleModes(fallback=DEFAULT_CYCLE_MODES):
	"""Read and defensively validate modes included in the cycle command."""
	try:
		includedModes = validateCycleModes(config.conf[CONFIG_SECTION]["includedModes"])
		if includedModes is not None:
			return includedModes
	except Exception:
		pass
	log.warning("Enhanced Duck: invalid cycle mode list; keeping the last safe list")
	return fallback


def _readAdjustmentSteps(fallback: AdjustmentSteps = DEFAULT_ADJUSTMENT_STEPS) -> AdjustmentSteps:
	"""Read and defensively validate shortcut step sizes for the current profile."""
	try:
		section = config.conf[CONFIG_SECTION]
		steps = AdjustmentSteps.fromValues(
			section["depthAdjustmentStep"],
			section["attackAdjustmentStepMs"],
			section["releaseAdjustmentStepMs"],
		)
		if steps is not None:
			return steps
	except Exception:
		pass
	log.warning("Enhanced Duck: invalid adjustment steps; using the safe defaults")
	return fallback


def _writeDuckingConfig(settings: AudioDuckingConfig, previous: AudioDuckingConfig | None = None):
	section = config.conf[CONFIG_SECTION]
	if previous is None or settings.depth != previous.depth:
		section["duckingDepth"] = round(settings.depth * 100, 2)
	if previous is None or settings.attackTimeMs != previous.attackTimeMs:
		section["attackTime"] = settings.attackTimeMs
	if previous is None or settings.releaseTimeMs != previous.releaseTimeMs:
		section["releaseTime"] = settings.releaseTimeMs
