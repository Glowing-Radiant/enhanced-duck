import ctypes
import importlib
import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class _GUID(ctypes.Structure):
	_fields_ = [("bytes", ctypes.c_ubyte * 16)]

	def __init__(self, _value=None):
		super().__init__()


class _IUnknown(ctypes.Structure):
	_fields_ = []


class _Timer:
	def __init__(self, delay=None):
		self.delay = delay
		self.stopped = False

	def Stop(self):
		self.stopped = True


class _Log:
	def debugWarning(self, *args, **kwargs):
		pass

	def exception(self, *args, **kwargs):
		pass

	def warning(self, *args, **kwargs):
		pass


def _loadSessionDucker():
	core = types.ModuleType("core")
	core.scheduledTimers = []

	def callLater(delay, *args, **kwargs):
		timer = _Timer(delay)
		core.scheduledTimers.append(timer)
		return timer

	core.callLater = callLater
	logHandler = types.ModuleType("logHandler")
	logHandler.log = _Log()
	comtypes = types.ModuleType("comtypes")
	comtypes.CLSCTX_ALL = 1
	comtypes.COMMETHOD = lambda *args, **kwargs: None
	comtypes.GUID = _GUID
	comtypes.HRESULT = ctypes.c_long
	comtypes.IUnknown = _IUnknown
	comtypesClient = types.ModuleType("comtypes.client")
	comtypesClient.CreateObject = lambda *args, **kwargs: None
	sys.modules.update({
		"core": core,
		"logHandler": logHandler,
		"comtypes": comtypes,
		"comtypes.client": comtypesClient,
	})
	path = (
		pathlib.Path(__file__).parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "enhancedDuck"
		/ "sessionDucker.py"
	)
	packageName = "enhancedDuck_sessionDucker_test"
	package = types.ModuleType(packageName)
	package.__path__ = [str(path.parent)]
	sys.modules[packageName] = package
	spec = importlib.util.spec_from_file_location(f"{packageName}.sessionDucker", path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


sessionDucker = _loadSessionDucker()
coreAudio = importlib.import_module(f"{sessionDucker.__package__}.coreAudio")


class _FakeSession:
	def __init__(self, volume=1.0, state=None):
		self.pid = 42
		self.key = (self.pid, "session")
		self.appKey = "application"
		self.value = volume
		self.history = []
		self.state = (
			sessionDucker.AUDIO_SESSION_STATE_ACTIVE
			if state is None
			else state
		)

	def getVolume(self):
		return self.value

	def setVolume(self, value):
		self.value = float(value)
		self.history.append(self.value)


class AudioDuckingConfigTests(unittest.TestCase):
	def test_keyboard_adjustments_use_configured_steps_and_clamp(self):
		settings = sessionDucker.AudioDuckingConfig(0.1, 2, 10)
		self.assertEqual(
			settings.adjusted(depthDelta=-0.025),
			sessionDucker.AudioDuckingConfig(0.075, 2, 10),
		)
		self.assertEqual(
			settings.adjusted(attackDeltaMs=25),
			sessionDucker.AudioDuckingConfig(0.1, 27, 10),
		)
		self.assertEqual(
			settings.adjusted(releaseDeltaMs=75),
			sessionDucker.AudioDuckingConfig(0.1, 2, 85),
		)
		self.assertEqual(
			sessionDucker.AudioDuckingConfig(0.0, 0, 0).adjusted(-1, -1, -1),
			sessionDucker.AudioDuckingConfig(0.0, 0, 0),
		)
		self.assertEqual(
			sessionDucker.AudioDuckingConfig(1.0, 60000, 60000).adjusted(1, 1, 1),
			sessionDucker.AudioDuckingConfig(1.0, 60000, 60000),
		)

	def test_accepts_multiplier_and_millisecond_values(self):
		result = sessionDucker.AudioDuckingConfig.fromValues("0.25", "150", 600)
		self.assertEqual(
			result,
			sessionDucker.AudioDuckingConfig(0.25, 150, 600),
		)
		self.assertEqual(
			sessionDucker.AudioDuckingConfig.fromValues("0,25", 150, 600),
			result,
		)

	def test_rejects_invalid_values(self):
		invalidValues = (
			(-0.01, 0, 0),
			(1.01, 0, 0),
			(float("nan"), 0, 0),
			(0.2, -1, 0),
			(0.2, 1.5, 0),
			(0.2, 0, sessionDucker.MAX_TRANSITION_TIME_MS + 1),
			(True, 0, 0),
		)
		for values in invalidValues:
			with self.subTest(values=values):
				self.assertIsNone(sessionDucker.AudioDuckingConfig.fromValues(*values))


class AdjustmentStepsTests(unittest.TestCase):
	def test_accepts_custom_steps_and_decimal_comma(self):
		self.assertEqual(
			sessionDucker.AdjustmentSteps.fromValues("0,025", "25", 75),
			sessionDucker.AdjustmentSteps(0.025, 25, 75),
		)

	def test_attack_and_release_default_to_the_same_step(self):
		self.assertEqual(
			sessionDucker.DEFAULT_ADJUSTMENT_STEPS,
			sessionDucker.AdjustmentSteps(0.05, 50, 50),
		)

	def test_rejects_steps_that_cannot_change_a_value(self):
		invalidValues = (
			(0, 10, 50),
			(sessionDucker.MIN_DEPTH_ADJUSTMENT_STEP / 10, 10, 50),
			(1.01, 10, 50),
			(float("nan"), 10, 50),
			(0.05, 0, 50),
			(0.05, 1.5, 50),
			(0.05, 10, sessionDucker.MAX_TRANSITION_TIME_MS + 1),
			(True, 10, 50),
		)
		for values in invalidValues:
			with self.subTest(values=values):
				self.assertIsNone(sessionDucker.AdjustmentSteps.fromValues(*values))


class VolumeTransitionTests(unittest.TestCase):
	def test_repeated_start_does_not_restart_an_active_mode(self):
		ducker = sessionDucker.FocusAwareSessionDucker()
		ducker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
		firstTimer = ducker._callLater
		ducker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
		self.assertIs(firstTimer, ducker._callLater)
		self.assertFalse(firstTimer.stopped)

	def test_repeated_configuration_changes_are_coalesced(self):
		ducker = sessionDucker.FocusAwareSessionDucker()
		ducker._mode = sessionDucker.DUCK_ALL_APPLICATIONS
		ducker._lastConfigRefreshTime = 10.0
		originalMonotonic = sessionDucker.time.monotonic
		try:
			sessionDucker.time.monotonic = lambda: 10.01
			ducker.configure(sessionDucker.AudioDuckingConfig(0.3, 50, 50))
			refreshTimer = ducker._configRefreshTimer
			for index in range(1000):
				ducker.configure(
					sessionDucker.AudioDuckingConfig(0.31 + ((index % 60) / 100), 50, 50),
				)
			self.assertIs(refreshTimer, ducker._configRefreshTimer)
			self.assertGreater(refreshTimer.delay, 0)
			self.assertLessEqual(refreshTimer.delay, sessionDucker.CONFIG_REFRESH_INTERVAL_MS)
			self.assertFalse(refreshTimer.stopped)
		finally:
			sessionDucker.time.monotonic = originalMonotonic

	def test_release_time_change_does_not_block_a_reduck_mid_fade(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 100, 1000),
		)
		ducker._mode = sessionDucker.DUCK_ALL_APPLICATIONS
		session = _FakeSession(0.8)
		with patch.object(sessionDucker, "iterAudioSessions", return_value=[session]):
			ducker._updateDuckedSessions(0.0)
			ducker._updateDuckedSessions(0.2)
			self.assertAlmostEqual(session.value, 0.2)
			# Output ends, so a release fade starts.
			ducker._mode = None
			ducker._updateDuckedSessions(0.3)
			ducker._updateDuckedSessions(0.5)
			# NVDA+alt+equals during that fade changes the release time only,
			# which must not strand the session on its way back to full volume.
			ducker.configure(sessionDucker.AudioDuckingConfig(0.25, 100, 1500))
			ducker._mode = sessionDucker.DUCK_ALL_APPLICATIONS
			ducker._updateDuckedSessions(0.55)
			transition = ducker._transitions[session.key]
			self.assertFalse(transition.restoreWhenComplete)
			self.assertAlmostEqual(transition.targetVolume, 0.2)
			ducker._updateDuckedSessions(2.5)
			self.assertAlmostEqual(session.value, 0.2)

	def test_recovery_entries_for_dead_processes_are_discarded(self):
		with tempfile.TemporaryDirectory() as directory:
			path = f"{directory}/recovery.json"
			store = sessionDucker.VolumeRecoveryStore(path)
			store.record((4242, "gone"), 0.9)
			store.record((4243, "still-running"), 0.7)
			ducker = sessionDucker.FocusAwareSessionDucker(recoveryPath=path)
			with patch.object(sessionDucker, "iterAudioSessions", return_value=[]):
				with patch.object(sessionDucker, "getLivePids", return_value={4243}):
					ducker.recoverStaleVolumes()
			self.assertEqual(
				set(sessionDucker.VolumeRecoveryStore(path).keys()),
				{(4243, "still-running")},
			)

	def test_a_failed_snapshot_prunes_no_recovery_entries(self):
		with tempfile.TemporaryDirectory() as directory:
			path = f"{directory}/recovery.json"
			sessionDucker.VolumeRecoveryStore(path).record((4242, "unknown"), 0.9)
			ducker = sessionDucker.FocusAwareSessionDucker(recoveryPath=path)
			with patch.object(sessionDucker, "iterAudioSessions", return_value=[]):
				with patch.object(sessionDucker, "getLivePids", return_value=set()):
					ducker.recoverStaleVolumes()
			self.assertEqual(
				set(sessionDucker.VolumeRecoveryStore(path).keys()),
				{(4242, "unknown")},
			)

	def test_vanished_session_does_not_leave_a_recovery_entry(self):
		class VanishingSession(_FakeSession):
			def setVolume(self, value):
				raise OSError("audio session expired")

		with tempfile.TemporaryDirectory() as directory:
			path = f"{directory}/recovery.json"
			ducker = sessionDucker.FocusAwareSessionDucker(
				sessionDucker.AudioDuckingConfig(0.25, 0, 0),
				recoveryPath=path,
			)
			ducker._mode = sessionDucker.DUCK_ALL_APPLICATIONS
			session = _FakeSession(0.8)
			with patch.object(sessionDucker, "iterAudioSessions", return_value=[session]):
				ducker._updateDuckedSessions(0.0)
			self.assertIn(session.key, ducker._recoveryStore.keys())
			# The process exits, so the tracked session both disappears from the
			# enumeration and can no longer be written to.
			ducker._trackedSessions[session.key] = VanishingSession(0.2)
			with patch.object(sessionDucker, "iterAudioSessions", return_value=[]):
				ducker._updateDuckedSessions(0.1)
			ducker._flushPendingRecoveryDiscards()
			self.assertEqual(set(ducker._recoveryStore.keys()), set())

	def test_ramps_use_five_millisecond_ticks(self):
		self.assertEqual(sessionDucker.RAMP_INTERVAL_MS, 5)

	def test_ramp_midpoint_is_perceptually_even(self):
		self.assertAlmostEqual(
			sessionDucker._interpolateVolume(1.0, 0.25, 0.5),
			0.5,
		)
		self.assertEqual(sessionDucker._interpolateVolume(1.0, 0.0, 1.0), 0.0)

	def test_ramp_eases_both_endpoints_without_reversing_direction(self):
		volumes = [
			sessionDucker._interpolateVolume(1.0, 0.2, index / 100)
			for index in range(101)
		]
		self.assertEqual(volumes[0], 1.0)
		self.assertEqual(volumes[-1], 0.2)
		self.assertTrue(all(left >= right for left, right in zip(volumes, volumes[1:])))
		self.assertLess(1.0 - volumes[1], volumes[50] - volumes[51])
		self.assertLess(volumes[99] - volumes[100], volumes[50] - volumes[51])

	def test_failed_volume_transition_is_not_retried_every_five_milliseconds(self):
		class FailingSession(_FakeSession):
			def setVolume(self, value):
				raise OSError("audio session expired")

		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 100, 100),
		)
		session = FailingSession()
		ducker._beginTransition(session, 0.25, 100, 10.0, False)
		ducker._lastDiscoveryTime = 10.0
		ducker._advancePendingTransitions(10.05)
		self.assertFalse(ducker._transitions)
		self.assertIsNone(ducker._lastDiscoveryTime)

	def test_stable_focus_uses_cheap_checks_and_focus_change_forces_discovery(self):
		ducker = sessionDucker.FocusAwareSessionDucker()
		ducker._mode = sessionDucker.DUCK_INACTIVE_WINDOWS
		ducker._lastDiscoveryTime = 10.0
		ducker._lastForegroundPid = 42
		ducker._foregroundPidKnown = True
		updates = []
		originalMonotonic = sessionDucker.time.monotonic
		originalGetForegroundPid = sessionDucker._getForegroundPid
		try:
			sessionDucker.time.monotonic = lambda: 10.1
			sessionDucker._getForegroundPid = lambda: 42
			ducker._updateDuckedSessions = lambda *args: updates.append(args)
			ducker._poll()
			self.assertEqual(updates, [])
			self.assertLessEqual(ducker._callLater.delay, sessionDucker.FOCUS_CHECK_INTERVAL_MS)

			sessionDucker._getForegroundPid = lambda: 43
			ducker._poll()
			self.assertEqual(len(updates), 1)
			self.assertEqual(updates[0][1], 43)
		finally:
			sessionDucker.time.monotonic = originalMonotonic
			sessionDucker._getForegroundPid = originalGetForegroundPid

	def test_process_name_cache_is_bounded_and_prunable(self):
		cache = coreAudio._processNameCache
		cache.clear()
		try:
			for pid in range(coreAudio.MAX_PROCESS_NAME_CACHE_ENTRIES + 50):
				coreAudio._cacheProcessName(pid, f"process{pid}.exe")
			self.assertEqual(len(cache), coreAudio.MAX_PROCESS_NAME_CACHE_ENTRIES)
			coreAudio._pruneProcessNameCache({300, 301})
			self.assertEqual(set(cache), {300, 301})
		finally:
			cache.clear()

	def test_inactive_sessions_are_only_kept_while_tracked(self):
		key = (42, "session")
		self.assertTrue(
			coreAudio._shouldIncludeAudioSession(
				sessionDucker.AUDIO_SESSION_STATE_ACTIVE,
				key,
				set(),
			),
		)
		self.assertFalse(
			coreAudio._shouldIncludeAudioSession(
				coreAudio.AUDIO_SESSION_STATE_INACTIVE,
				key,
				set(),
			),
		)
		self.assertTrue(
			coreAudio._shouldIncludeAudioSession(
				coreAudio.AUDIO_SESSION_STATE_INACTIVE,
				key,
				{key},
			),
		)
		self.assertTrue(
			coreAudio._shouldIncludeAudioSession(
				coreAudio.AUDIO_SESSION_STATE_EXPIRED,
				key,
				{key},
			),
		)

	def test_short_ramp_uses_its_deadline_instead_of_full_tick_interval(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.5, 2, 10),
		)
		session = _FakeSession()
		ducker._ensureDucked(session, 10.0)
		self.assertEqual(ducker._getNextRampDelay(10.0), 2)
		self.assertEqual(ducker._getNextRampDelay(10.0015), 1)

	def test_all_applications_mode_ducks_sessions_without_visible_windows(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.5, 0, 0),
		)
		session = _FakeSession(0.8)
		originalIterator = sessionDucker.iterAudioSessions
		try:
			sessionDucker.iterAudioSessions = lambda *_args, **_kwargs: iter((session,))
			ducker._mode = sessionDucker.DUCK_ALL_APPLICATIONS
			ducker._updateDuckedSessions(1.0)
			self.assertAlmostEqual(session.value, 0.4)
			ducker._mode = None
			ducker._updateDuckedSessions(2.0)
			self.assertAlmostEqual(session.value, 0.8)
		finally:
			sessionDucker.iterAudioSessions = originalIterator

	def test_inactive_tracked_session_is_restored_and_forgotten(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.5, 0, 0),
		)
		session = _FakeSession(0.8)
		ducker._ensureDucked(session, 1.0)
		self.assertAlmostEqual(session.value, 0.4)
		session.state = coreAudio.AUDIO_SESSION_STATE_INACTIVE
		originalIterator = sessionDucker.iterAudioSessions
		try:
			sessionDucker.iterAudioSessions = lambda *_args, **_kwargs: iter((session,))
			ducker._mode = sessionDucker.DUCK_ALL_APPLICATIONS
			ducker._updateDuckedSessions(2.0)
			self.assertAlmostEqual(session.value, 0.8)
			self.assertNotIn(session.key, ducker._originalVolumes)
		finally:
			sessionDucker.iterAudioSessions = originalIterator

	def test_replacement_session_uses_original_application_volume(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 0, 600),
		)
		original = _FakeSession(0.8)
		replacement = _FakeSession(0.2)
		replacement.key = (replacement.pid, "replacement")
		sessions = [original]
		with patch.object(sessionDucker, "iterAudioSessions", side_effect=lambda _keys: iter(sessions)):
			ducker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
			ducker._updateDuckedSessions(1.0)
			self.assertAlmostEqual(original.value, 0.2)
			original.state = coreAudio.AUDIO_SESSION_STATE_EXPIRED
			sessions.append(replacement)
			ducker._updateDuckedSessions(2.0)
			self.assertAlmostEqual(original.value, 0.8)
			self.assertAlmostEqual(replacement.value, 0.2)
			ducker.stop(immediate=True)
			self.assertAlmostEqual(replacement.value, 0.8)

	def test_release_time_change_retargets_a_release_after_output_ends(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 0, 600),
		)
		session = _FakeSession(0.8)
		with patch.object(sessionDucker, "iterAudioSessions", return_value=[session]):
			ducker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
			ducker._updateDuckedSessions(1.0)
			ducker.stop()
			ducker._updateDuckedSessions(2.0)
			ducker._advanceTransition(session, 2.150)
			self.assertLess(session.value, 0.8)
			ducker.configure(sessionDucker.AudioDuckingConfig(0.25, 0, 0))
			self.assertIsNotNone(ducker._configRefreshTimer)
			ducker._updateDuckedSessions(2.2)
			self.assertAlmostEqual(session.value, 0.8)

	def test_original_volume_is_recovered_after_a_simulated_crash(self):
		with tempfile.TemporaryDirectory() as temporaryDirectory:
			recoveryPath = str(pathlib.Path(temporaryDirectory) / "recovery.json")
			ducker = sessionDucker.FocusAwareSessionDucker(
				sessionDucker.AudioDuckingConfig(0.5, 0, 0),
				recoveryPath,
			)
			session = _FakeSession(0.8)
			ducker._ensureDucked(session, 1.0)
			self.assertAlmostEqual(session.value, 0.4)

			restartedDucker = sessionDucker.FocusAwareSessionDucker(
				sessionDucker.AudioDuckingConfig(0.5, 0, 0),
				recoveryPath,
			)
			originalIterator = sessionDucker.iterAudioSessions
			try:
				sessionDucker.iterAudioSessions = lambda *_args, **_kwargs: iter((session,))
				restartedDucker.recoverStaleVolumes()
				self.assertAlmostEqual(session.value, 0.8)
				self.assertFalse(restartedDucker._recoveryStore)
			finally:
				sessionDucker.iterAudioSessions = originalIterator

	def test_session_is_not_ducked_when_recovery_data_cannot_be_saved(self):
		class FailingRecoveryStore:
			def record(self, key, volume):
				raise OSError("disk unavailable")

		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.5, 0, 0),
		)
		ducker._recoveryStore = FailingRecoveryStore()
		session = _FakeSession(0.8)
		ducker._ensureDucked(session, 1.0)
		self.assertAlmostEqual(session.value, 0.8)
		self.assertNotIn(session.key, ducker._originalVolumes)

	def test_closely_spaced_ducking_cycles_reuse_one_recovery_write(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.5, 0, 0),
		)
		writeCount = 0

		def countWrite():
			nonlocal writeCount
			writeCount += 1

		ducker._recoveryStore._write = countWrite
		session = _FakeSession(0.8)
		ducker._ensureDucked(session, 1.0)
		ducker._ensureRestored(session, 2.0)
		pendingTimer = ducker._recoveryDiscardTimer
		self.assertIn(session.key, ducker._pendingRecoveryDiscards)

		ducker._ensureDucked(session, 3.0)
		self.assertEqual(writeCount, 1)
		self.assertNotIn(session.key, ducker._pendingRecoveryDiscards)
		self.assertTrue(pendingTimer.stopped)

	def test_depth_is_relative_to_original_session_volume(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 0, 0),
		)
		session = _FakeSession(0.8)
		ducker._ensureDucked(session, 1.0)
		self.assertAlmostEqual(session.value, 0.2)
		ducker._ensureRestored(session, 2.0)
		self.assertAlmostEqual(session.value, 0.8)
		self.assertNotIn(session.key, ducker._originalVolumes)

	def test_attack_release_and_reversal_start_at_current_volume(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 100, 200),
		)
		session = _FakeSession()

		ducker._ensureDucked(session, 10.0)
		ducker._advanceTransition(session, 10.05)
		self.assertAlmostEqual(session.value, 0.5, places=5)

		ducker._ensureRestored(session, 10.05)
		ducker._advanceTransition(session, 10.15)
		self.assertAlmostEqual(session.value, 0.5 ** 0.5, places=5)

		ducker._ensureDucked(session, 10.15)
		ducker._advanceTransition(session, 10.20)
		self.assertAlmostEqual(session.value, (0.25 * (0.5 ** 0.5)) ** 0.5, places=5)
		ducker._advanceTransition(session, 10.25)
		self.assertAlmostEqual(session.value, 0.25, places=5)

	def test_invalid_runtime_config_does_not_change_last_safe_config(self):
		safe = sessionDucker.AudioDuckingConfig(0.25, 150, 600)
		ducker = sessionDucker.FocusAwareSessionDucker(safe)
		invalid = sessionDucker.AudioDuckingConfig(2.0, -1, -1)
		self.assertFalse(ducker.configure(invalid))
		self.assertEqual(ducker.duckingConfig, safe)

	def test_runtime_config_retargets_an_in_flight_attack(self):
		ducker = sessionDucker.FocusAwareSessionDucker(
			sessionDucker.AudioDuckingConfig(0.25, 100, 200),
		)
		session = _FakeSession()
		ducker._ensureDucked(session, 10.0)
		ducker._advanceTransition(session, 10.05)
		self.assertAlmostEqual(session.value, 0.5, places=5)

		ducker._mode = sessionDucker.DUCK_ACTIVE_WINDOWS
		self.assertTrue(
			ducker.configure(sessionDucker.AudioDuckingConfig(0.75, 50, 300)),
		)
		ducker._ensureDucked(session, 10.05)
		ducker._advanceTransition(session, 10.075)
		self.assertAlmostEqual(session.value, (0.5 * 0.75) ** 0.5, places=5)
		ducker._advanceTransition(session, 10.10)
		self.assertAlmostEqual(session.value, 0.75, places=5)


if __name__ == "__main__":
	unittest.main()
