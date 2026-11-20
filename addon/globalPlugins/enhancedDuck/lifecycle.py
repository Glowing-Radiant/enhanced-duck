# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Lifecycle decisions kept independent of NVDA modules for regression tests."""


def shouldRestoreNativeDuckingOnTerminate(
	configuredMode: int,
	configurableBuiltInModes,
	shutdownTriggered: bool,
) -> bool:
	"""Restore native ducking only for a live add-on unload or plug-in reload."""
	return configuredMode in configurableBuiltInModes and not shutdownTriggered
