# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Windows Core Audio session ducking for foreground-window aware modes."""

from __future__ import annotations

import os
from ctypes import (
	POINTER, byref, c_bool, c_float, c_int, c_uint, c_void_p, c_wchar_p, create_unicode_buffer, windll,
)
from ctypes.wintypes import DWORD, HWND
from typing import Dict, Iterable, Optional, Set, Tuple

import core
from logHandler import log

from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown
from comtypes.client import CreateObject


DUCK_INACTIVE_WINDOWS = 3
DUCK_ACTIVE_WINDOWS = 4
# Scalar (0.0 - 1.0) applied to a ducked application's audio session volume.
DUCKED_VOLUME = 0.2
POLL_INTERVAL_MS = 300

# NVDA's own process must never be ducked, otherwise its speech and sounds would
# be lowered in the "duck inactive applications" mode (NVDA is never foreground).
_OWN_PID = os.getpid()

# Processes that own audio sessions but are not user applications, so ducking
# them is meaningless or harmful. audiodg.exe is the Windows audio engine that
# renders every stream; lowering its session volume distorts the whole output
# pipeline rather than a single app.
_EXCLUDED_PROCESS_NAMES = frozenset({"audiodg.exe"})

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

eRender = 0
eMultimedia = 1

windll.user32.GetForegroundWindow.restype = c_void_p


class IAudioSessionManager2(IUnknown):
	_iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
	_methods_ = [
		COMMETHOD([], HRESULT, "GetAudioSessionControl"),
		COMMETHOD([], HRESULT, "GetSimpleAudioVolume"),
		COMMETHOD([], HRESULT, "GetSessionEnumerator",
			(["out"], POINTER(POINTER(IUnknown)), "SessionEnum")),
	]


class IMMDevice(IUnknown):
	_iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
	_methods_ = [
		COMMETHOD([], HRESULT, "Activate",
			(["in"], POINTER(GUID), "iid"),
			(["in"], DWORD, "dwClsCtx"),
			(["in"], c_void_p, "pActivationParams"),
			(["out"], POINTER(POINTER(IAudioSessionManager2)), "ppInterface")),
	]


class IMMDeviceEnumerator(IUnknown):
	_iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
	_methods_ = [
		COMMETHOD([], HRESULT, "EnumAudioEndpoints"),
		COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
			(["in"], c_uint, "dataFlow"),
			(["in"], c_uint, "role"),
			(["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
	]


class IAudioSessionControl(IUnknown):
	_iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
	_methods_ = [
		COMMETHOD([], HRESULT, "GetState"),
		COMMETHOD([], HRESULT, "GetDisplayName"),
		COMMETHOD([], HRESULT, "SetDisplayName"),
		COMMETHOD([], HRESULT, "GetIconPath"),
		COMMETHOD([], HRESULT, "SetIconPath"),
		COMMETHOD([], HRESULT, "GetGroupingParam"),
		COMMETHOD([], HRESULT, "SetGroupingParam"),
		COMMETHOD([], HRESULT, "RegisterAudioSessionNotification"),
		COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification"),
	]


class IAudioSessionControl2(IAudioSessionControl):
	_iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
	_methods_ = [
		COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
		COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
		COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(DWORD), "pRetVal")),
		COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
		COMMETHOD([], HRESULT, "SetDuckingPreference"),
	]


class IAudioSessionEnumerator(IUnknown):
	_iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
	_methods_ = [
		COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
		COMMETHOD([], HRESULT, "GetSession",
			(["in"], c_int, "SessionCount"),
			(["out"], POINTER(POINTER(IAudioSessionControl)), "Session")),
	]


class ISimpleAudioVolume(IUnknown):
	_iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
	_methods_ = [
		COMMETHOD([], HRESULT, "SetMasterVolume",
			(["in"], c_float, "fLevel"),
			(["in"], c_void_p, "EventContext")),
		COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(c_float), "pfLevel")),
		COMMETHOD([], HRESULT, "SetMute",
			(["in"], c_bool, "bMute"),
			(["in"], c_void_p, "EventContext")),
		COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(c_bool), "pbMute")),
	]


MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


class AudioSessionVolume:
	def __init__(self, pid: int, key: Tuple[int, str], volume: ISimpleAudioVolume):
		self.pid = pid
		self.key = key
		self.volume = volume

	def getVolume(self) -> float:
		return float(self.volume.GetMasterVolume())

	def setVolume(self, value: float):
		self.volume.SetMasterVolume(c_float(value), None)


def _getForegroundPid() -> Optional[int]:
	hwnd = windll.user32.GetForegroundWindow()
	if not hwnd:
		return None
	pid = DWORD()
	windll.user32.GetWindowThreadProcessId(HWND(hwnd), byref(pid))
	return int(pid.value) or None


_processNameCache: Dict[int, str] = {}


def _getProcessName(pid: int) -> str:
	"""Return the lower-cased image name (e.g. ``chrome.exe``) for a pid, or ``""``."""
	cached = _processNameCache.get(pid)
	if cached is not None:
		return cached
	name = ""
	handle = windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
	if handle:
		try:
			buffer = create_unicode_buffer(260)
			size = DWORD(len(buffer))
			if windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
				name = os.path.basename(buffer.value).lower()
		finally:
			windll.kernel32.CloseHandle(handle)
	_processNameCache[pid] = name
	return name


def _getAudioSessionEnumerator() -> IAudioSessionEnumerator:
	deviceEnumerator = CreateObject(MMDeviceEnumerator, interface=IMMDeviceEnumerator)
	device = deviceEnumerator.GetDefaultAudioEndpoint(eRender, eMultimedia)
	manager = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
	return manager.GetSessionEnumerator().QueryInterface(IAudioSessionEnumerator)


def iterAudioSessions() -> Iterable[AudioSessionVolume]:
	enumerator = _getAudioSessionEnumerator()
	count = enumerator.GetCount()
	for index in range(count):
		session = enumerator.GetSession(index)
		control2 = session.QueryInterface(IAudioSessionControl2)
		pid = int(control2.GetProcessId())
		# Skip the system sounds session (pid 0), NVDA itself, and non-application
		# audio producers such as the Windows audio engine.
		if not pid or pid == _OWN_PID or _getProcessName(pid) in _EXCLUDED_PROCESS_NAMES:
			continue
		try:
			sessionKey = control2.GetSessionInstanceIdentifier()
		except Exception:
			sessionKey = str(index)
		volume = session.QueryInterface(ISimpleAudioVolume)
		yield AudioSessionVolume(pid, (pid, sessionKey), volume)


class FocusAwareSessionDucker:
	def __init__(self):
		self._mode: Optional[int] = None
		self._callLater = None
		self._originalVolumes: Dict[Tuple[int, str], float] = {}
		self._duckedSessions: Set[Tuple[int, str]] = set()

	def start(self, mode: int):
		self._mode = mode
		self._schedule(immediate=True)

	def stop(self):
		self._mode = None
		self._cancelTimer()
		self._restoreAll()

	def _schedule(self, immediate: bool = False):
		self._cancelTimer()
		delay = 0 if immediate else POLL_INTERVAL_MS
		self._callLater = core.callLater(delay, self._poll)

	def _cancelTimer(self):
		if self._callLater is not None:
			try:
				self._callLater.Stop()
			except Exception:
				pass
			self._callLater = None

	def _poll(self):
		self._callLater = None
		if self._mode not in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS):
			return
		try:
			self._updateDuckedSessions()
		except Exception:
			log.exception("Error while updating Enhanced Duck audio sessions")
		finally:
			if self._mode in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS):
				self._schedule()

	def _updateDuckedSessions(self):
		foregroundPid = _getForegroundPid()
		currentSessions = {session.key: session for session in iterAudioSessions()}
		sessionPids = {session.pid for session in currentSessions.values()}
		targetPids = self._getTargetPids(sessionPids, foregroundPid)

		for key, session in currentSessions.items():
			# Keep one uncooperative session from aborting the whole pass.
			try:
				if session.pid in targetPids:
					self._duckSession(session)
				elif key in self._duckedSessions:
					self._restoreSession(session)
			except Exception:
				log.exception("Enhanced Duck: could not adjust an audio session")

		for staleKey in set(self._duckedSessions) - set(currentSessions):
			self._duckedSessions.discard(staleKey)
			self._originalVolumes.pop(staleKey, None)

	def _getTargetPids(self, sessionPids: Set[int], foregroundPid: Optional[int]) -> Set[int]:
		if foregroundPid is None:
			return set()
		# NVDA and non-application processes are already filtered out of the
		# session list; this is a defensive second guard.
		duckable = sessionPids - {_OWN_PID}
		if self._mode == DUCK_ACTIVE_WINDOWS:
			return {foregroundPid} & duckable
		if self._mode == DUCK_INACTIVE_WINDOWS:
			return duckable - {foregroundPid}
		return set()

	def _duckSession(self, session: AudioSessionVolume):
		if session.key not in self._originalVolumes:
			self._originalVolumes[session.key] = session.getVolume()
		session.setVolume(min(session.getVolume(), DUCKED_VOLUME))
		self._duckedSessions.add(session.key)

	def _restoreSession(self, session: AudioSessionVolume):
		originalVolume = self._originalVolumes.pop(session.key, None)
		if originalVolume is not None:
			session.setVolume(originalVolume)
		self._duckedSessions.discard(session.key)

	def _restoreAll(self):
		try:
			currentSessions = {session.key: session for session in iterAudioSessions()}
		except Exception:
			log.exception("Error while restoring Enhanced Duck audio sessions")
			self._originalVolumes.clear()
			self._duckedSessions.clear()
			return
		for key in list(self._duckedSessions):
			session = currentSessions.get(key)
			if session is not None:
				self._restoreSession(session)
			else:
				self._duckedSessions.discard(key)
				self._originalVolumes.pop(key, None)
