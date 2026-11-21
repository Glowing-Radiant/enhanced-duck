import importlib.util
import pathlib
import queue
import sys
import threading
import unittest

from test_sessionDucker import sessionDucker


def _loadWorker():
	path = (
		pathlib.Path(__file__).parents[1]
		/ "addon"
		/ "globalPlugins"
		/ "enhancedDuck"
		/ "audioWorker.py"
	)
	spec = importlib.util.spec_from_file_location(
		f"{sessionDucker.__package__}.audioWorker", path,
	)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


audioWorker = _loadWorker()


class _FakeEngine:
	def __init__(self, events, config, recoveryPath, callLater):
		self.events = events
		self.callLater = callLater
		self.events.put(("construct", threading.get_ident(), config, recoveryPath))

	def recoverStaleVolumes(self):
		self.events.put(("recover", threading.get_ident()))

	def configure(self, config):
		self.events.put(("configure", threading.get_ident(), config))

	def start(self, mode):
		self.events.put(("start", threading.get_ident(), mode))
		self.callLater(5, lambda: self.events.put(("timer", threading.get_ident())))

	def stop(self, immediate=False):
		self.events.put(("stop", threading.get_ident(), immediate))


class AudioWorkerTests(unittest.TestCase):
	def setUp(self):
		self.events = queue.Queue()
		self.workers = []
		self.mainThread = threading.get_ident()

	def tearDown(self):
		for worker in self.workers:
			self.assertTrue(worker.close(), "audio worker did not exit")

	def _newWorker(self, **kwargs):
		def factory(config, recoveryPath, callLater):
			return _FakeEngine(self.events, config, recoveryPath, callLater)

		worker = audioWorker.AudioWorkerSessionDucker(
			sessionDucker.AudioDuckingConfig(0.2, 50, 50),
			"recovery.json",
			engineFactory=factory,
			initializeCom=lambda: self.events.put(("comInit", threading.get_ident())),
			uninitializeCom=lambda: self.events.put(("comUninit", threading.get_ident())),
			**kwargs,
		)
		self.workers.append(worker)
		return worker

	def _next(self, name):
		event = self.events.get(timeout=2)
		self.assertEqual(event[0], name, event)
		self.assertNotEqual(event[1], self.mainThread)
		return event

	def test_core_audio_lifecycle_and_timer_stay_on_worker_thread(self):
		worker = self._newWorker()
		self.assertTrue(worker.recoverStaleVolumes())
		workerId = self._next("comInit")[1]
		self.assertEqual(self._next("construct")[1], workerId)
		self.assertEqual(self._next("recover")[1], workerId)
		worker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
		self.assertEqual(self._next("start")[1], workerId)
		self.assertEqual(self._next("timer")[1], workerId)
		newConfig = sessionDucker.AudioDuckingConfig(0.4, 100, 200)
		self.assertTrue(worker.configure(newConfig))
		self.assertEqual(self._next("configure"), ("configure", workerId, newConfig))
		worker.stop()
		self.assertEqual(self._next("stop"), ("stop", workerId, False))
		self.assertTrue(worker.close())
		self.assertEqual(self._next("stop"), ("stop", workerId, True))
		self.assertEqual(self._next("comUninit")[1], workerId)

	def test_invalid_mode_and_configuration_are_rejected_without_killing_worker(self):
		worker = self._newWorker()
		self.assertTrue(worker.recoverStaleVolumes())
		self._next("comInit")
		self._next("construct")
		self._next("recover")
		with self.assertRaises(ValueError):
			worker.start(999)
		self.assertFalse(worker.configure(sessionDucker.AudioDuckingConfig(2.0, -1, -1)))
		worker.start(sessionDucker.DUCK_ACTIVE_WINDOWS)
		self._next("start")

	def test_cancelled_long_timers_do_not_accumulate_without_bound(self):
		class CancellingEngine(_FakeEngine):
			def start(engine, mode):
				for _ in range(300):
					engine.callLater(10000, lambda: None).Stop()
				engine.events.put(("scheduled", threading.get_ident()))

		worker = audioWorker.AudioWorkerSessionDucker(
			sessionDucker.FALLBACK_DUCKING_CONFIG,
			engineFactory=lambda config, path, callLater: CancellingEngine(
				self.events, config, path, callLater,
			),
			initializeCom=lambda: None,
			uninitializeCom=lambda: None,
		)
		self.workers.append(worker)
		worker.recoverStaleVolumes()
		self._next("construct")
		self._next("recover")
		worker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
		self._next("scheduled")
		self.assertLess(len(worker._timers), 128)

	def test_configuration_sent_during_startup_is_not_lost(self):
		recoveryEntered = threading.Event()
		finishRecovery = threading.Event()

		class SlowRecoveryEngine(_FakeEngine):
			def recoverStaleVolumes(engine):
				recoveryEntered.set()
				finishRecovery.wait(2)
				super().recoverStaleVolumes()

		worker = audioWorker.AudioWorkerSessionDucker(
			sessionDucker.FALLBACK_DUCKING_CONFIG,
			engineFactory=lambda config, path, callLater: SlowRecoveryEngine(
				self.events, config, path, callLater,
			),
			initializeCom=lambda: None,
			uninitializeCom=lambda: None,
		)
		self.workers.append(worker)
		self.assertTrue(recoveryEntered.wait(2))
		newConfig = sessionDucker.AudioDuckingConfig(0.4, 100, 200)
		self.assertTrue(worker.configure(newConfig))
		worker.start(sessionDucker.DUCK_ALL_APPLICATIONS)
		finishRecovery.set()
		self.assertTrue(worker.recoverStaleVolumes())
		self._next("construct")
		self._next("recover")
		self.assertEqual(self._next("configure")[2], newConfig)
		self.assertEqual(self._next("start")[2], sessionDucker.DUCK_ALL_APPLICATIONS)

	def test_startup_failure_is_reported_and_thread_exits(self):
		def failCom():
			raise RuntimeError("COM unavailable")

		worker = audioWorker.AudioWorkerSessionDucker(
			sessionDucker.FALLBACK_DUCKING_CONFIG,
			initializeCom=failCom,
			uninitializeCom=lambda: None,
		)
		self.workers.append(worker)
		with self.assertRaisesRegex(RuntimeError, "initialization failed"):
			worker.recoverStaleVolumes()
		self.assertTrue(worker.close())


if __name__ == "__main__":
	unittest.main()
