# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Configurable Windows Core Audio ducking for NVDA and the focus-aware modes."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import core
from logHandler import log

from .coreAudio import (
	AudioSessionVolume,
	AUDIO_SESSION_STATE_ACTIVE,
	_ApplicationResolver,
	_OWN_PID,
	_getForegroundPid,
	_invalidateSessionManager,
	getLivePids,
	iterAudioSessions,
)

from .volumeRecovery import VolumeRecoveryStore


DUCK_INACTIVE_WINDOWS = 3
DUCK_ACTIVE_WINDOWS = 4
# Internal mode used to replace NVDA's OUTPUTTING and ALWAYS behaviors. It is
# not added to the mode selector because those existing entries activate it.
DUCK_ALL_APPLICATIONS = 5
# Foreground PID checks are cheap. Full process/window/Core Audio discovery is
# deliberately less frequent because doing all three several times per second
# caused sustained CPU use during long-running ducking sessions.
FOCUS_CHECK_INTERVAL_MS = 100
SESSION_DISCOVERY_INTERVAL_MS = 300
# Session volume changes are discrete. Five-millisecond ticks keep individual
# changes below the perceptible stair-step cadence of the previous 25 ms ramp.
RAMP_INTERVAL_MS = 5
CONFIG_REFRESH_INTERVAL_MS = 100
RECOVERY_DISCARD_DELAY_MS = 10000
MIN_RAMP_VOLUME = 0.0001
MAX_TRANSITION_TIME_MS = 60000
MIN_DEPTH_ADJUSTMENT_STEP = 0.0001
DEFAULT_DEPTH_ADJUSTMENT_STEP = 0.05
DEFAULT_ATTACK_ADJUSTMENT_STEP_MS = 50
DEFAULT_RELEASE_ADJUSTMENT_STEP_MS = 50


def _validateDuckingValues(depth, attackTimeMs, releaseTimeMs, minDepth, minTimeMs):
	"""Return the validated triple, or ``None`` for invalid input."""
	try:
		if isinstance(depth, bool) or isinstance(attackTimeMs, bool) or isinstance(releaseTimeMs, bool):
			return None
		if isinstance(depth, str):
			# Accept the decimal comma used by many locales in the settings panel
			# while storing and displaying one scalar representation.
			depth = depth.strip().replace(",", ".")
		validatedDepth = float(depth)
		validatedAttack = int(attackTimeMs)
		validatedRelease = int(releaseTimeMs)
		# Do not silently truncate fractional transition times.
		if float(attackTimeMs) != validatedAttack or float(releaseTimeMs) != validatedRelease:
			return None
	except (TypeError, ValueError, OverflowError):
		return None
	if not math.isfinite(validatedDepth) or not minDepth <= validatedDepth <= 1.0:
		return None
	if not minTimeMs <= validatedAttack <= MAX_TRANSITION_TIME_MS:
		return None
	if not minTimeMs <= validatedRelease <= MAX_TRANSITION_TIME_MS:
		return None
	return validatedDepth, validatedAttack, validatedRelease


@dataclass(frozen=True)
class AdjustmentSteps:
	"""Validated step sizes used by the keyboard adjustment commands."""

	depth: float = DEFAULT_DEPTH_ADJUSTMENT_STEP
	attackTimeMs: int = DEFAULT_ATTACK_ADJUSTMENT_STEP_MS
	releaseTimeMs: int = DEFAULT_RELEASE_ADJUSTMENT_STEP_MS

	@classmethod
	def fromValues(cls, depth, attackTimeMs, releaseTimeMs) -> Optional["AdjustmentSteps"]:
		# A step of zero would make its keyboard command do nothing.
		values = _validateDuckingValues(
			depth, attackTimeMs, releaseTimeMs, MIN_DEPTH_ADJUSTMENT_STEP, 1,
		)
		return cls(*values) if values is not None else None


DEFAULT_ADJUSTMENT_STEPS = AdjustmentSteps()


@dataclass(frozen=True)
class AudioDuckingConfig:
	"""Validated settings used by the session ducker.

	``depth`` is a scalar multiplier: 0.25 means that a ducked session is
	lowered to 25 percent of the volume it had before ducking started.
	"""

	depth: float = 0.2
	attackTimeMs: int = 0
	releaseTimeMs: int = 0

	def adjusted(
		self,
		depthDelta: float = 0.0,
		attackDeltaMs: int = 0,
		releaseDeltaMs: int = 0,
	) -> "AudioDuckingConfig":
		"""Return a safely clamped copy for keyboard-driven adjustments."""
		depth = round(max(0.0, min(1.0, self.depth + depthDelta)), 4)
		attackTimeMs = max(0, min(MAX_TRANSITION_TIME_MS, self.attackTimeMs + attackDeltaMs))
		releaseTimeMs = max(0, min(MAX_TRANSITION_TIME_MS, self.releaseTimeMs + releaseDeltaMs))
		return AudioDuckingConfig(depth, attackTimeMs, releaseTimeMs)

	@classmethod
	def fromValues(cls, depth, attackTimeMs, releaseTimeMs) -> Optional["AudioDuckingConfig"]:
		"""Return a validated configuration, or ``None`` for invalid input."""
		values = _validateDuckingValues(depth, attackTimeMs, releaseTimeMs, 0.0, 0)
		return cls(*values) if values is not None else None


# The engine's own starting point, not the add-on's shipped defaults: those live
# in settings.DEFAULT_DUCKING_CONFIG and are always passed in by the caller.
FALLBACK_DUCKING_CONFIG = AudioDuckingConfig()


@dataclass
class _VolumeTransition:
	session: AudioSessionVolume
	startVolume: float
	targetVolume: float
	startTime: float
	durationMs: int
	restoreWhenComplete: bool


def _interpolateVolume(startVolume: float, targetVolume: float, progress: float) -> float:
	"""Ease both ends of a fade in the logarithmic amplitude domain."""
	progress = max(0.0, min(1.0, progress))
	if progress <= 0.0:
		return startVolume
	if progress >= 1.0:
		return targetVolume
	safeStart = max(MIN_RAMP_VOLUME, startVolume)
	safeTarget = max(MIN_RAMP_VOLUME, targetVolume)
	# A straight logarithmic ramp starts and stops with an audible slope change.
	# Smoothstep gives both ends zero slope while preserving the exact endpoints.
	easedProgress = progress * progress * (3.0 - 2.0 * progress)
	return math.exp(
		math.log(safeStart) + ((math.log(safeTarget) - math.log(safeStart)) * easedProgress),
	)


class FocusAwareSessionDucker:
	def __init__(
		self,
		duckingConfig: AudioDuckingConfig = FALLBACK_DUCKING_CONFIG,
		recoveryPath: Optional[str] = None,
		callLater=None,
	):
		self._callLaterFactory = callLater or core.callLater
		self._mode: Optional[int] = None
		self._callLater = None
		self._configRefreshTimer = None
		self._recoveryDiscardTimer = None
		self._lastConfigRefreshTime: Optional[float] = None
		self._lastDiscoveryTime: Optional[float] = None
		self._forceDiscovery = False
		self._lastForegroundPid: Optional[int] = None
		self._resolver = _ApplicationResolver()
		self._applicationVolumes: Dict[str, float] = {}
		self._trackedSessions: Dict[Tuple[int, str], AudioSessionVolume] = {}
		self._config = FALLBACK_DUCKING_CONFIG
		self._originalVolumes: Dict[Tuple[int, str], float] = {}
		self._desiredDucked: Dict[Tuple[int, str], bool] = {}
		self._appliedDepths: Dict[Tuple[int, str], float] = {}
		self._transitions: Dict[Tuple[int, str], _VolumeTransition] = {}
		self._pendingRecoveryDiscards: Set[Tuple[int, str]] = set()
		self._recoveryStore = VolumeRecoveryStore(recoveryPath)
		self.configure(duckingConfig)

	@property
	def duckingConfig(self) -> AudioDuckingConfig:
		return self._config

	def configure(self, duckingConfig: AudioDuckingConfig) -> bool:
		"""Apply validated settings; reject invalid objects without changing audio."""
		if not isinstance(duckingConfig, AudioDuckingConfig):
			log.warning("Enhanced Duck: ignored an invalid audio ducking configuration")
			return False
		validated = AudioDuckingConfig.fromValues(
			duckingConfig.depth,
			duckingConfig.attackTimeMs,
			duckingConfig.releaseTimeMs,
		)
		if validated is None:
			log.warning("Enhanced Duck: ignored an out-of-range audio ducking configuration")
			return False
		previous = self._config
		if validated == previous:
			return True
		self._config = validated
		if validated.depth != previous.depth:
			self._appliedDepths.clear()
		for key, transition in self._transitions.items():
			if transition.restoreWhenComplete:
				if validated.releaseTimeMs != previous.releaseTimeMs:
					self._desiredDucked[key] = True
			elif validated.attackTimeMs != previous.attackTimeMs:
				self._appliedDepths.pop(key, None)
		# Coalesce key repeats so COM discovery runs at a bounded frequency.
		if self._mode is not None or self._originalVolumes:
			self._requestConfigurationRefresh()
		return True

	def recoverStaleVolumes(self):
		"""Restore mixer values left behind if a previous NVDA process crashed."""
		keys = set(self._recoveryStore.keys())
		if not keys:
			return
		try:
			currentSessions = {session.key: session for session in iterAudioSessions(keys)}
			livePids = getLivePids()
		except Exception:
			log.exception("Enhanced Duck: could not inspect sessions for crash recovery")
			return
		discardableKeys = []
		for key in keys:
			session = currentSessions.get(key)
			originalVolume = self._recoveryStore.get(key)
			if session is None or originalVolume is None:
				# Windows never reuses a session instance id, so an entry whose
				# process has exited can never be matched again. An empty pid set
				# means the snapshot failed, so prune nothing rather than guess.
				if livePids and key[0] not in livePids:
					discardableKeys.append(key)
				continue
			try:
				session.setVolume(originalVolume)
			except Exception:
				log.exception("Enhanced Duck: could not recover an application volume")
			else:
				discardableKeys.append(key)
		self._discardRecoveryEntries(discardableKeys)

	def start(self, mode: int):
		if mode not in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS, DUCK_ALL_APPLICATIONS):
			raise ValueError("Unsupported Enhanced Duck mode")
		modeChanged = mode != self._mode
		if modeChanged:
			self._lastForegroundPid = None
		self._mode = mode
		if modeChanged:
			self._cancelConfigurationRefresh()
			self._schedule(immediate=True)

	def stop(self, immediate: bool = False):
		self._mode = None
		self._lastForegroundPid = None
		self._cancelConfigurationRefresh()
		self._cancelTimer()
		if immediate:
			self._restoreAll()
			self._flushPendingRecoveryDiscards()
			_invalidateSessionManager()
		elif self._originalVolumes:
			# Keep the timer alive until all release ramps have completed.
			self._schedule(immediate=True)

	def _requestConfigurationRefresh(self):
		if self._configRefreshTimer is not None:
			return
		now = time.monotonic()
		elapsedMs = (
			(now - self._lastConfigRefreshTime) * 1000.0
			if self._lastConfigRefreshTime is not None
			else CONFIG_REFRESH_INTERVAL_MS
		)
		delay = max(0, math.ceil(CONFIG_REFRESH_INTERVAL_MS - elapsedMs))
		self._configRefreshTimer = self._callLaterFactory(delay, self._applyPendingConfigurationRefresh)

	def _applyPendingConfigurationRefresh(self):
		self._configRefreshTimer = None
		self._lastConfigRefreshTime = time.monotonic()
		if self._mode is not None or self._originalVolumes:
			self._schedule(immediate=True)

	def _cancelConfigurationRefresh(self):
		if self._configRefreshTimer is None:
			return
		try:
			self._configRefreshTimer.Stop()
		except Exception:
			pass
		self._configRefreshTimer = None

	def _queueRecoveryDiscard(self, key: Tuple[int, str]):
		self._pendingRecoveryDiscards.add(key)
		if self._recoveryDiscardTimer is None:
			self._recoveryDiscardTimer = self._callLaterFactory(
				RECOVERY_DISCARD_DELAY_MS,
				self._flushPendingRecoveryDiscards,
			)

	def _cancelPendingRecoveryDiscard(self, key: Tuple[int, str]):
		self._pendingRecoveryDiscards.discard(key)
		if self._pendingRecoveryDiscards or self._recoveryDiscardTimer is None:
			return
		try:
			self._recoveryDiscardTimer.Stop()
		except Exception:
			pass
		self._recoveryDiscardTimer = None

	def _flushPendingRecoveryDiscards(self):
		if self._recoveryDiscardTimer is not None:
			try:
				self._recoveryDiscardTimer.Stop()
			except Exception:
				pass
			self._recoveryDiscardTimer = None
		keys = set(self._pendingRecoveryDiscards)
		if not keys:
			return
		if self._discardRecoveryEntries(keys):
			self._pendingRecoveryDiscards.difference_update(keys)
		else:
			self._recoveryDiscardTimer = self._callLaterFactory(
				RECOVERY_DISCARD_DELAY_MS,
				self._flushPendingRecoveryDiscards,
			)

	def _schedule(self, immediate: bool = False):
		self._cancelTimer()
		if immediate:
			self._forceDiscovery = True
			delay = 0
		else:
			delays = []
			if self._transitions:
				delays.append(self._getNextRampDelay())
			elapsedMs = (
				(time.monotonic() - self._lastDiscoveryTime) * 1000.0
				if self._lastDiscoveryTime is not None
				else SESSION_DISCOVERY_INTERVAL_MS
			)
			delays.append(max(1, int(SESSION_DISCOVERY_INTERVAL_MS - elapsedMs)))
			if self._mode in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS):
				delays.append(FOCUS_CHECK_INTERVAL_MS)
			delay = min(delays)
		self._callLater = self._callLaterFactory(delay, self._poll)

	def _getNextRampDelay(self, now: Optional[float] = None) -> int:
		"""Return a responsive tick delay without overshooting short ramps."""
		if not self._transitions:
			return RAMP_INTERVAL_MS
		if now is None:
			now = time.monotonic()
		remainingTimes = (
			transition.durationMs - ((now - transition.startTime) * 1000.0)
			for transition in self._transitions.values()
		)
		nearestDeadline = min(remainingTimes)
		return max(1, min(RAMP_INTERVAL_MS, math.ceil(nearestDeadline)))

	def _cancelTimer(self):
		if self._callLater is not None:
			try:
				self._callLater.Stop()
			except Exception:
				pass
			self._callLater = None

	def _poll(self):
		self._callLater = None
		modeActive = self._mode in (
			DUCK_INACTIVE_WINDOWS,
			DUCK_ACTIVE_WINDOWS,
			DUCK_ALL_APPLICATIONS,
		)
		if not modeActive and not self._originalVolumes:
			return
		try:
			now = time.monotonic()
			foregroundPid = None
			if self._mode in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS):
				foregroundPid = _getForegroundPid()
				if foregroundPid is not None and foregroundPid != self._lastForegroundPid:
					self._lastForegroundPid = foregroundPid
					self._forceDiscovery = True
				foregroundPid = self._lastForegroundPid
			timeSinceDiscoveryMs = (
				(now - self._lastDiscoveryTime) * 1000.0
				if self._lastDiscoveryTime is not None
				else SESSION_DISCOVERY_INTERVAL_MS
			)
			if self._forceDiscovery or timeSinceDiscoveryMs >= SESSION_DISCOVERY_INTERVAL_MS:
				self._forceDiscovery = False
				# Record the attempt even if device enumeration fails, preventing a
				# tight retry loop on a temporarily unavailable audio endpoint.
				self._lastDiscoveryTime = now
				self._lastConfigRefreshTime = now
				self._updateDuckedSessions(now, foregroundPid)
			else:
				# Between application-discovery polls, a ramp tick only changes
				# already-known session volumes. Heavy process snapshots and audio
				# enumeration never run at the higher smoothing cadence.
				self._advancePendingTransitions(now)
		except Exception:
			log.exception("Error while updating Enhanced Duck audio sessions")
		finally:
			if not self._originalVolumes:
				self._applicationVolumes.clear()
			modeActive = self._mode in (
				DUCK_INACTIVE_WINDOWS,
				DUCK_ACTIVE_WINDOWS,
				DUCK_ALL_APPLICATIONS,
			)
			if modeActive or self._originalVolumes:
				self._schedule()

	def _updateDuckedSessions(
		self,
		now: Optional[float] = None,
		foregroundPid: Optional[int] = None,
	):
		focusAware = self._mode in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS)
		if focusAware:
			self._resolver.beginPass()
			if foregroundPid is not None:
				self._resolver.noteApplicationPid(foregroundPid)

		# Restore dead streams while their mixer state is still writable, before
		# applying a target to any replacement stream from the same application.
		recoveryKeys = set(self._recoveryStore.keys()) - self._pendingRecoveryDiscards
		sessions = list(iterAudioSessions(set(self._originalVolumes) | recoveryKeys))
		if now is None:
			now = time.monotonic()
		currentKeys = {session.key for session in sessions}
		for staleKey in set(self._trackedSessions) - currentKeys:
			session = self._trackedSessions[staleKey]
			try:
				self._restoreSession(session)
			except Exception:
				log.debugWarning("Enhanced Duck: session disappeared before restoration", exc_info=True)
				# Gone from the enumeration, so its ledger entry can never match
				# again; discard it instead of keeping it for the backstop age.
				self._forgetSession(staleKey, restored=True)

		liveApps = set()
		for session in sorted(sessions, key=lambda session: session.state == AUDIO_SESSION_STATE_ACTIVE):
			key = session.key
			recoveredVolume = self._recoveryStore.get(key) if key in recoveryKeys else None
			if key not in self._originalVolumes and recoveredVolume is not None:
				self._originalVolumes[key] = recoveredVolume
				self._desiredDucked[key] = True
				self._applicationVolumes.setdefault(session.appKey, recoveredVolume)
			shouldDuck = False
			try:
				if session.state != AUDIO_SESSION_STATE_ACTIVE:
					if key in self._originalVolumes:
						self._trackedSessions[key] = session
						self._restoreSession(session)
					continue
				appPid = self._resolver.resolve(session.pid, foregroundPid) if focusAware else None
				shouldDuck = self._mode == DUCK_ALL_APPLICATIONS or (
					appPid not in (None, _OWN_PID)
					and foregroundPid is not None
					and (appPid == foregroundPid) == (self._mode == DUCK_ACTIVE_WINDOWS)
				)
				if shouldDuck:
					self._ensureDucked(session, now)
				elif key in self._originalVolumes:
					self._ensureRestored(session, now)
				self._advanceTransition(session, now)
			except Exception:
				log.exception("Enhanced Duck: could not adjust an audio session")
				self._resetFailedTransition(key)
			if key in self._originalVolumes:
				self._trackedSessions[key] = session
			if shouldDuck or key in self._originalVolumes:
				liveApps.add(session.appKey)
		self._applicationVolumes = {
			appKey: volume for appKey, volume in self._applicationVolumes.items() if appKey in liveApps
		}

	def _restoreSession(self, session: AudioSessionVolume):
		session.setVolume(self._originalVolumes[session.key])
		self._forgetSession(session.key, restored=True)

	def _ensureDucked(self, session: AudioSessionVolume, now: float):
		key = session.key
		wasRestored = key in self._pendingRecoveryDiscards
		if key not in self._originalVolumes:
			originalVolume = None if wasRestored else self._applicationVolumes.get(session.appKey)
			if originalVolume is None:
				originalVolume = session.getVolume()
			if not self._recordRecoveryEntry(key, originalVolume):
				# Never attenuate a session unless its original mixer value is safely
				# recoverable after a hard NVDA or audio-service failure.
				return
			self._originalVolumes[key] = originalVolume
			self._applicationVolumes[session.appKey] = originalVolume
		self._cancelPendingRecoveryDiscard(key)
		inFlight = self._transitions.get(key)
		needsNewTarget = (
			not self._desiredDucked.get(key, False)
			or self._appliedDepths.get(key) != self._config.depth
			# A release re-armed by configure() or by a failed transition leaves
			# both of those unchanged, but the fade still has to turn around.
			or (inFlight is not None and inFlight.restoreWhenComplete)
		)
		if not needsNewTarget:
			return
		targetVolume = self._originalVolumes[key] * self._config.depth
		self._beginTransition(
			session,
			targetVolume,
			self._config.attackTimeMs,
			now,
			restoreWhenComplete=False,
		)
		# Update state only after the COM calls above succeed, so a temporary
		# device failure is retried on the next discovery pass.
		self._desiredDucked[key] = True
		self._appliedDepths[key] = self._config.depth

	def _ensureRestored(self, session: AudioSessionVolume, now: float):
		key = session.key
		if not self._desiredDucked.get(key, False):
			return
		self._beginTransition(
			session,
			self._originalVolumes[key],
			self._config.releaseTimeMs,
			now,
			restoreWhenComplete=True,
		)
		if key in self._originalVolumes:
			self._desiredDucked[key] = False

	def _beginTransition(
		self,
		session: AudioSessionVolume,
		targetVolume: float,
		durationMs: int,
		now: float,
		restoreWhenComplete: bool,
	):
		startVolume = session.getVolume()
		if durationMs == 0 or math.isclose(startVolume, targetVolume, abs_tol=0.0001):
			session.setVolume(targetVolume)
			self._transitions.pop(session.key, None)
			if restoreWhenComplete:
				self._forgetSession(session.key, restored=True)
			return
		self._transitions[session.key] = _VolumeTransition(
			session=session,
			startVolume=startVolume,
			targetVolume=targetVolume,
			startTime=now,
			durationMs=durationMs,
			restoreWhenComplete=restoreWhenComplete,
		)

	def _advanceTransition(self, session: AudioSessionVolume, now: float):
		transition = self._transitions.get(session.key)
		if transition is None:
			return
		# Full discovery can produce a fresh COM wrapper for the same session.
		transition.session = session
		elapsedMs = max(0.0, (now - transition.startTime) * 1000.0)
		progress = min(1.0, elapsedMs / transition.durationMs)
		volume = _interpolateVolume(
			transition.startVolume,
			transition.targetVolume,
			progress,
		)
		session.setVolume(max(0.0, min(1.0, volume)))
		if progress >= 1.0:
			self._transitions.pop(session.key, None)
			if transition.restoreWhenComplete:
				self._forgetSession(session.key, restored=True)

	def _advancePendingTransitions(self, now: float):
		for transition in list(self._transitions.values()):
			try:
				self._advanceTransition(transition.session, now)
			except Exception:
				log.exception("Enhanced Duck: could not advance an audio volume transition")
				# A session wrapper can become invalid when an application or audio
				# device disappears. Do not repeat the failing COM call every 5 ms;
				# rediscover sessions and retry through the slower discovery path.
				self._resetFailedTransition(transition.session.key)

	def _resetFailedTransition(self, key: Tuple[int, str]):
		transition = self._transitions.pop(key, None)
		if transition is not None and transition.restoreWhenComplete:
			self._desiredDucked[key] = True
		else:
			self._appliedDepths.pop(key, None)
		self._lastDiscoveryTime = None

	def _recordRecoveryEntry(self, key: Tuple[int, str], originalVolume: float) -> bool:
		try:
			self._recoveryStore.record(key, originalVolume)
		except Exception:
			log.exception("Enhanced Duck: could not save application volume recovery data")
			return False
		return True

	def _discardRecoveryEntries(self, keys) -> bool:
		try:
			self._recoveryStore.discardMany(keys)
		except Exception:
			log.exception("Enhanced Duck: could not update application volume recovery data")
			return False
		return True

	def _forgetSession(self, key: Tuple[int, str], restored: bool = False):
		self._originalVolumes.pop(key, None)
		self._trackedSessions.pop(key, None)
		self._desiredDucked.pop(key, None)
		self._appliedDepths.pop(key, None)
		self._transitions.pop(key, None)
		if restored:
			# Keep the ledger briefly so ordinary, closely spaced NVDA utterances do
			# not perform disk I/O at every duck/release cycle. A clean unload flushes
			# immediately; after a crash, restoring an already-restored value is safe.
			self._queueRecoveryDiscard(key)

	def _restoreAll(self):
		for key, session in list(self._trackedSessions.items()):
			try:
				self._restoreSession(session)
			except Exception:
				log.exception("Enhanced Duck: could not restore an audio session")
				self._forgetSession(key)
		self._applicationVolumes.clear()
