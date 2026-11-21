# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Crash-safe storage for application volumes changed by Enhanced Duck."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import time
from typing import Dict, Iterable, Optional, Tuple


RecoveryKey = Tuple[int, str]
RECOVERY_FILE_VERSION = 1
# Entries are pruned by process liveness; this is only a backstop for a
# ledger left behind by a machine that never came back up.
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class _RecoveryEntry:
	volume: float
	savedAt: float


class VolumeRecoveryStore:
	"""Persist original session volumes before any attenuation is applied."""

	def __init__(
		self,
		path: Optional[str],
		maxAgeSeconds: int = DEFAULT_MAX_AGE_SECONDS,
	):
		self._path = path
		self._maxAgeSeconds = maxAgeSeconds
		self._entries: Dict[RecoveryKey, _RecoveryEntry] = {}
		self._load()

	def __bool__(self) -> bool:
		return bool(self._entries)

	def keys(self):
		return self._entries.keys()

	def get(self, key: RecoveryKey) -> Optional[float]:
		entry = self._entries.get(key)
		return entry.volume if entry is not None else None

	def record(self, key: RecoveryKey, volume: float):
		"""Record an original volume, retaining its first-seen timestamp."""
		entry = self._entries.get(key)
		if entry is not None and math.isclose(entry.volume, volume, abs_tol=0.000001):
			return
		savedAt = entry.savedAt if entry is not None else time.time()
		self._entries[key] = _RecoveryEntry(float(volume), savedAt)
		try:
			self._write()
		except Exception:
			if entry is None:
				self._entries.pop(key, None)
			else:
				self._entries[key] = entry
			raise

	def discard(self, key: RecoveryKey):
		self.discardMany((key,))

	def discardMany(self, keys: Iterable[RecoveryKey]):
		removedEntries = {}
		for key in keys:
			entry = self._entries.pop(key, None)
			if entry is not None:
				removedEntries[key] = entry
		if not removedEntries:
			return
		try:
			self._write()
		except Exception:
			# Keep memory consistent with the still-current on-disk ledger so a
			# later restore can retry removing these entries safely.
			self._entries.update(removedEntries)
			raise

	def _load(self):
		if not self._path:
			return
		try:
			with open(self._path, "r", encoding="utf-8") as recoveryFile:
				payload = json.load(recoveryFile)
		except FileNotFoundError:
			return
		except (OSError, ValueError, TypeError):
			return
		if not isinstance(payload, dict) or payload.get("version") != RECOVERY_FILE_VERSION:
			return
		now = time.time()
		rawEntries = payload.get("entries", ())
		if not isinstance(rawEntries, list):
			return
		for rawEntry in rawEntries:
			try:
				pid = int(rawEntry["pid"])
				sessionId = str(rawEntry["sessionId"])
				volume = float(rawEntry["volume"])
				savedAt = float(rawEntry["savedAt"])
			except (KeyError, TypeError, ValueError, OverflowError):
				continue
			if pid <= 0 or not sessionId or not math.isfinite(volume) or not 0.0 <= volume <= 1.0:
				continue
			if not math.isfinite(savedAt) or now - savedAt > self._maxAgeSeconds:
				continue
			self._entries[(pid, sessionId)] = _RecoveryEntry(volume, savedAt)

	def _write(self):
		if not self._path:
			return
		payload = {
			"version": RECOVERY_FILE_VERSION,
			"entries": [
				{
					"pid": key[0],
					"sessionId": key[1],
					"volume": entry.volume,
					"savedAt": entry.savedAt,
				}
				for key, entry in sorted(self._entries.items())
			],
		}
		temporaryPath = f"{self._path}.tmp"
		with open(temporaryPath, "w", encoding="utf-8") as recoveryFile:
			json.dump(payload, recoveryFile, ensure_ascii=False, separators=(",", ":"))
			recoveryFile.flush()
			# This file only earns its keep after an unclean exit, so reach the disk.
			os.fsync(recoveryFile.fileno())
		os.replace(temporaryPath, self._path)
