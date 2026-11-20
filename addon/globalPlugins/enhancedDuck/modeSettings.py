# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Validation and cycling helpers for selectable audio ducking modes."""

from __future__ import annotations

from typing import Iterable, Optional, Tuple


MODE_COUNT = 5
DEFAULT_CYCLE_MODES = tuple(range(MODE_COUNT))


def validateCycleModes(values: Iterable, modeCount: int = MODE_COUNT) -> Optional[Tuple[int, ...]]:
	"""Return unique valid mode numbers in display order, or ``None``."""
	if isinstance(values, (str, bytes)):
		return None
	try:
		validated = set()
		for value in values:
			if isinstance(value, bool):
				return None
			mode = int(value)
			if float(value) != mode or not 0 <= mode < modeCount:
				return None
			validated.add(mode)
	except (TypeError, ValueError, OverflowError):
		return None
	if not validated:
		return None
	return tuple(sorted(validated))


def getNextCycleMode(currentMode: int, includedModes: Tuple[int, ...]) -> int:
	"""Return the next checked mode, or the first one if current is excluded."""
	if not includedModes:
		raise ValueError("At least one ducking mode must be included")
	try:
		currentIndex = includedModes.index(int(currentMode))
	except (ValueError, TypeError, OverflowError):
		return includedModes[0]
	return includedModes[(currentIndex + 1) % len(includedModes)]
