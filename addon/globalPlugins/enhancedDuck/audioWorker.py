# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Run all Core Audio session work in one dedicated COM apartment.

Only immutable configuration values and mode numbers cross the thread boundary.
No Core Audio interface is ever passed to NVDA's UI thread.
"""

from __future__ import annotations

import heapq
import threading
import time
from typing import Callable, Optional

import comtypes
from logHandler import log

from .sessionDucker import (
	AudioDuckingConfig,
	DUCK_ACTIVE_WINDOWS,
	DUCK_ALL_APPLICATIONS,
	DUCK_INACTIVE_WINDOWS,
	FocusAwareSessionDucker,
)


class _WorkerTimer:
	def __init__(self, callback: Callable):
		self.callback = callback
		self.cancelled = False

	def Stop(self):
		self.cancelled = True


class AudioWorkerSessionDucker:
	"""Non-blocking main-thread facade for the worker-owned session ducker."""

	def __init__(
		self,
		duckingConfig: AudioDuckingConfig,
		recoveryPath: Optional[str] = None,
		engineFactory=FocusAwareSessionDucker,
		initializeCom=None,
		uninitializeCom=None,
	):
		self._lock = threading.Lock()
		self._wake = threading.Event()
		self._ready = threading.Event()
		self._exited = threading.Event()
		self._engineFactory = engineFactory
		self._initializeCom = initializeCom or (
			lambda: comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
		)
		self._uninitializeCom = uninitializeCom or comtypes.CoUninitialize
		self._recoveryPath = recoveryPath
		self._config = duckingConfig
		self._desiredMode: Optional[int] = None
		self._version = 0
		self._closing = False
		self._startupError = None
		self._timers = []
		self._timerSequence = 0
		self._thread = threading.Thread(
			target=self._run,
			name="EnhancedDuckAudio",
			daemon=True,
		)
		self._thread.start()

	@property
	def duckingConfig(self) -> AudioDuckingConfig:
		with self._lock:
			return self._config

	def recoverStaleVolumes(self, timeout: float = 5.0) -> bool:
		"""Wait for startup recovery, before NVDA's ducking hooks are installed."""
		if not self._ready.wait(timeout):
			self.close(timeout=0.1)
			raise RuntimeError("Enhanced Duck audio worker did not initialize")
		if self._startupError is not None:
			raise RuntimeError("Enhanced Duck audio worker initialization failed") from self._startupError
		return True

	def configure(self, duckingConfig: AudioDuckingConfig) -> bool:
		validated = (
			AudioDuckingConfig.fromValues(
				duckingConfig.depth,
				duckingConfig.attackTimeMs,
				duckingConfig.releaseTimeMs,
			)
			if isinstance(duckingConfig, AudioDuckingConfig)
			else None
		)
		if validated is None:
			return False
		with self._lock:
			if self._closing or self._exited.is_set():
				return False
			if validated == self._config:
				return True
			self._config = validated
			self._version += 1
		self._wake.set()
		return True

	def start(self, mode: int):
		if mode not in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS, DUCK_ALL_APPLICATIONS):
			raise ValueError("Unsupported Enhanced Duck mode")
		with self._lock:
			if self._closing or self._exited.is_set() or mode == self._desiredMode:
				return
			self._desiredMode = mode
			self._version += 1
		self._wake.set()

	def stop(self, immediate: bool = False):
		if immediate:
			# Synchronous restoration is only needed when the add-on unloads.
			return self.close()
		# Ordinary output activity never waits for COM or disk I/O.
		with self._lock:
			if self._closing or self._exited.is_set() or self._desiredMode is None:
				return
			self._desiredMode = None
			self._version += 1
		self._wake.set()

	def close(self, timeout: float = 3.0) -> bool:
		with self._lock:
			self._closing = True
		self._wake.set()
		return self._exited.wait(timeout)

	def _callLater(self, delayMs: int, callback: Callable) -> _WorkerTimer:
		"""Schedule a callback on the worker that owns all COM session objects."""
		handle = _WorkerTimer(callback)
		self._timerSequence += 1
		heapq.heappush(
			self._timers,
			(time.monotonic() + max(0, delayMs) / 1000.0, self._timerSequence, handle),
		)
		# A cancelled recovery-discard timer may be ten seconds in the future.
		# Repeated short speech output would otherwise retain arbitrarily many
		# dead timers and bound engine callbacks until their deadlines pass.
		if len(self._timers) >= 128 and len(self._timers) % 64 == 0:
			self._timers = [item for item in self._timers if not item[2].cancelled]
			heapq.heapify(self._timers)
		return handle

	def _run(self):
		initializedCom = False
		engine = None
		try:
			self._initializeCom()
			initializedCom = True
			initialConfig = self._config
			engine = self._engineFactory(
				initialConfig,
				self._recoveryPath,
				callLater=self._callLater,
			)
			engine.recoverStaleVolumes()
			self._ready.set()
			appliedVersion = 0
			appliedConfig = initialConfig
			appliedMode = None
			while True:
				# Clear before reading desired state: commands arriving afterward
				# leave the event set and cannot be lost before the next wait.
				self._wake.clear()
				with self._lock:
					closing = self._closing
					version = self._version
					config = self._config
					mode = self._desiredMode
				if closing:
					break
				if version != appliedVersion:
					if config != appliedConfig:
						engine.configure(config)
						appliedConfig = config
					if mode != appliedMode:
						if mode is None:
							engine.stop()
						else:
							engine.start(mode)
						appliedMode = mode
					appliedVersion = version
				while self._timers and self._timers[0][2].cancelled:
					heapq.heappop(self._timers)
				if self._timers and self._timers[0][0] <= time.monotonic():
					_, _, handle = heapq.heappop(self._timers)
					if not handle.cancelled:
						try:
							handle.callback()
						except Exception:
							log.exception("Enhanced Duck audio worker callback failed")
					continue
				timeout = (
					max(0.0, self._timers[0][0] - time.monotonic())
					if self._timers else None
				)
				self._wake.wait(timeout)
		except Exception as error:
			if not self._ready.is_set():
				self._startupError = error
			log.exception("Enhanced Duck audio worker failed")
		finally:
			# A popped timer's local handle can retain a bound engine method.
			# Release those references before uninitializing its COM apartment.
			handle = None
			self._timers.clear()
			if engine is not None:
				try:
					engine.stop(immediate=True)
				except Exception:
					log.exception("Enhanced Duck could not restore volumes during worker shutdown")
			self._timers.clear()
			engine = None
			try:
				if initializedCom:
					try:
						self._uninitializeCom()
					except Exception:
						log.exception("Enhanced Duck could not uninitialize the audio worker COM apartment")
			finally:
				self._ready.set()
				self._exited.set()
