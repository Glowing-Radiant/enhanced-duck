# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Windows Core Audio session ducking for foreground-window aware modes.

Ducking decisions are made per *application*, not per raw audio-session process.
Many apps play audio from a hidden helper/renderer/dependency process rather than
the process that owns their visible window, so each session's process is resolved
up its parent chain to the nearest ancestor that owns a top-level window. Sessions
with no top-level window anywhere in their ancestry are ignored, which keeps the
active app's own streaming audio from being ducked as if it were a separate app.
"""

from __future__ import annotations

import os
from ctypes import (
	POINTER, Structure, WINFUNCTYPE, byref, c_bool, c_float, c_int, c_long, c_size_t, c_uint, c_void_p,
	c_wchar, c_wchar_p, create_unicode_buffer, sizeof, windll,
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
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = c_void_p(-1).value

eRender = 0
eMultimedia = 1

# Handle-returning APIs default to a 32-bit ``c_int`` return/argument type, which
# truncates 64-bit handles on x64. Pin them to pointer width so the values stay
# intact.
windll.user32.GetForegroundWindow.restype = c_void_p
windll.kernel32.OpenProcess.restype = c_void_p
windll.kernel32.CreateToolhelp32Snapshot.restype = c_void_p
windll.kernel32.CloseHandle.argtypes = [c_void_p]
windll.kernel32.CloseHandle.restype = c_bool


class _PROCESSENTRY32W(Structure):
	_fields_ = [
		("dwSize", DWORD),
		("cntUsage", DWORD),
		("th32ProcessID", DWORD),
		("th32DefaultHeapID", c_size_t),
		("th32ModuleID", DWORD),
		("cntThreads", DWORD),
		("th32ParentProcessID", DWORD),
		("pcPriClassBase", c_long),
		("dwFlags", DWORD),
		("szExeFile", c_wchar * 260),
	]


# BOOL CALLBACK EnumWindowsProc(HWND, LPARAM)
_WNDENUMPROC = WINFUNCTYPE(c_int, c_void_p, c_void_p)


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


def _getParentPidMap() -> Dict[int, int]:
	"""Return a ``pid -> parent pid`` map for every running process."""
	result: Dict[int, int] = {}
	snapshot = windll.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
	if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
		return result
	try:
		entry = _PROCESSENTRY32W()
		entry.dwSize = sizeof(_PROCESSENTRY32W)
		if not windll.kernel32.Process32FirstW(snapshot, byref(entry)):
			return result
		while True:
			result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
			if not windll.kernel32.Process32NextW(snapshot, byref(entry)):
				break
	finally:
		windll.kernel32.CloseHandle(snapshot)
	return result


def _getTopLevelWindowPids() -> Set[int]:
	"""Return the pids that own at least one visible top-level window.

	These are the processes a user thinks of as "applications". Streaming
	helpers, renderer children and other background workers have no top-level
	window of their own and are therefore absent from this set.
	"""
	pids: Set[int] = set()

	def callback(hwnd, _lparam):
		if windll.user32.IsWindowVisible(hwnd):
			pid = DWORD()
			windll.user32.GetWindowThreadProcessId(HWND(hwnd), byref(pid))
			if pid.value:
				pids.add(int(pid.value))
		return 1

	# Keep a reference to the trampoline alive for the (synchronous) enumeration.
	proc = _WNDENUMPROC(callback)
	windll.user32.EnumWindows(proc, 0)
	return pids


def _resolveApplicationPid(
	pid: Optional[int], topLevelPids: Set[int], parentMap: Dict[int, int]
) -> Optional[int]:
	"""Map a process to the application (top-level window) it belongs to.

	Walks up the parent-process chain until it reaches a process that owns a
	top-level window, so a hidden streaming/renderer child is attributed to the
	visible application that spawned it. Returns ``None`` for processes with no
	top-level window anywhere in their ancestry -- those are ignored entirely.
	"""
	seen: Set[int] = set()
	current = pid
	while current and current not in seen:
		if current in topLevelPids:
			return current
		seen.add(current)
		current = parentMap.get(current)
	return None


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
		# Resolve every player to the top-level application it belongs to, so an
		# app that streams through a hidden helper process is treated as a single
		# application together with its window.
		parentMap = _getParentPidMap()
		topLevelPids = _getTopLevelWindowPids()
		foregroundAppPid = _resolveApplicationPid(_getForegroundPid(), topLevelPids, parentMap)

		currentSessions: Dict[Tuple[int, str], AudioSessionVolume] = {}
		sessionAppPids: Dict[Tuple[int, str], Optional[int]] = {}
		appPids: Set[int] = set()
		for session in iterAudioSessions():
			currentSessions[session.key] = session
			appPid = _resolveApplicationPid(session.pid, topLevelPids, parentMap)
			sessionAppPids[session.key] = appPid
			if appPid is not None:
				appPids.add(appPid)

		targetAppPids = self._getTargetAppPids(appPids, foregroundAppPid)

		for key, session in currentSessions.items():
			appPid = sessionAppPids[key]
			# Keep one uncooperative session from aborting the whole pass.
			try:
				if appPid is not None and appPid in targetAppPids:
					self._duckSession(session)
				elif key in self._duckedSessions:
					self._restoreSession(session)
			except Exception:
				log.exception("Enhanced Duck: could not adjust an audio session")

		for staleKey in set(self._duckedSessions) - set(currentSessions):
			self._duckedSessions.discard(staleKey)
			self._originalVolumes.pop(staleKey, None)

	def _getTargetAppPids(self, appPids: Set[int], foregroundAppPid: Optional[int]) -> Set[int]:
		if foregroundAppPid is None:
			return set()
		# ``appPids`` already excludes sessions with no top-level window; NVDA is
		# filtered out of the session list, and this drops it defensively too.
		duckable = appPids - {_OWN_PID}
		if self._mode == DUCK_ACTIVE_WINDOWS:
			return {foregroundAppPid} & duckable
		if self._mode == DUCK_INACTIVE_WINDOWS:
			return duckable - {foregroundAppPid}
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
