# -*- coding: UTF-8 -*-
# Copyright (C) 2026 Enhanced Duck contributors
# This file is covered by the GNU General Public License.

"""Windows Core Audio session ducking for foreground-window aware modes.

Ducking decisions are made per *application*, not per raw audio-session process.
Many apps play audio from a hidden helper/renderer/dependency process rather than
the process that owns their visible window, so each session's process is resolved
up its parent chain to the nearest ancestor that owns a visible top-level window,
which keeps the active app's own streaming audio from being ducked as if it were
a separate app. Sessions that cannot be tied to an application at all are ignored.

Multi-process applications -- web browsers above all -- stress that model in ways
that plain per-session ducking gets wrong, so they are handled explicitly:

* Chromium based browsers render audio in a sandboxed "audio service" utility
  process and Firefox in its content processes, none of which own a window. The
  parent chain covers the usual case; when it is broken (the creator has exited,
  or the child was re-parented) the session is matched to a window-owning process
  running the same executable instead. Shell and service hosts are never accepted
  as an application, so a browser whose windows are all closed is ignored rather
  than attributed to the desktop.
* Browser audio sessions churn constantly: one appears per tab that plays media,
  and they expire as tabs are closed or the audio service restarts. Windows
  persists a session's volume *per application*, so a session that disappears
  while ducked would leave the browser permanently quiet -- and its replacement
  would come back at the ducked volume, which the old code would then record as
  the "original". Original volumes are therefore remembered per application, and
  expired sessions are restored while they can still be reached.
* Browsers also make focus flicker (new windows, page loads, native menus), and
  ``GetForegroundWindow`` returns nothing during those transitions. The last
  known foreground application is kept across such gaps so ducking does not pump.
* A browser adds dozens of processes and sessions, which made the 300 ms poll
  expensive. The process table is now snapshotted at most every few seconds, the
  audio session manager is cached, and volumes are only written when they
  actually need to change.

How far an application is ducked, and how quickly it gets there and comes back,
are user settings (see ``settings``). A volume therefore does not jump to its new
value: once the pass above has decided which applications should be ducked, each
one's volume is moved towards the level it should be at over the configured
attack or release time, by a faster timer that runs only while something is
actually moving and only writes to the applications that are moving.
"""

from __future__ import annotations

import os
import time
from ctypes import (
	POINTER, Structure, WINFUNCTYPE, WinDLL, byref, c_bool, c_float, c_int, c_long, c_size_t, c_uint,
	c_void_p, c_wchar, c_wchar_p, create_unicode_buffer, sizeof,
)
from ctypes.wintypes import DWORD
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

import core
from logHandler import log

from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown
from comtypes.client import CreateObject

from .settings import DuckingSettings, getDuckingSettings


DUCK_INACTIVE_WINDOWS = 3
DUCK_ACTIVE_WINDOWS = 4

# How often the set of applications that should be ducked is recomputed.
POLL_INTERVAL_MS = 300

# While a volume is on its way to a new level, it is updated this often. Only the
# applications whose volume is actually moving are written to, so these extra
# ticks cost a couple of COM calls each rather than a full session enumeration.
# 50 ms is about 20 steps per second, which is smooth to the ear while keeping
# the shortest useful ramp (a 150 ms attack) to a handful of writes.
RAMP_INTERVAL_MS = 50

# Volumes are floats, so a level this close to the one being aimed for counts as
# having arrived; without it a ramp could creep for ever in the last few bits.
_LEVEL_EPSILON = 0.0005

# Taking a snapshot of every running process is by far the most expensive part of
# a poll, and with a browser open there are hundreds of them. Parent/child links
# never change, so the snapshot is reused for a while; it is refreshed early when
# an unknown process turns up, but never more often than the floor below.
_PROCESS_MAP_TTL = 5.0
_PROCESS_MAP_MIN_INTERVAL = 1.0

# The default endpoint's session manager is cached for this long. Rebuilding it
# periodically (rather than never) is what picks up a change of output device.
_SESSION_MANAGER_TTL = 5.0

# Volumes are floats and every write notifies each client of the session plus the
# volume mixer, so a value this close to the wanted one is left alone.
_VOLUME_EPSILON = 0.005

# NVDA's own process must never be ducked, otherwise its speech and sounds would
# be lowered in the "duck inactive applications" mode (NVDA is never foreground).
_OWN_PID = os.getpid()

# Processes that own audio sessions but are not user applications, so ducking
# them is meaningless or harmful. audiodg.exe is the Windows audio engine that
# renders every stream; lowering its session volume distorts the whole output
# pipeline rather than a single app.
_EXCLUDED_PROCESS_NAMES = frozenset({"audiodg.exe"})

# Processes that may own top-level windows (the desktop and the taskbar belong to
# explorer.exe) but are never the application a sound belongs to. Without this,
# any windowless audio producer launched from the shell would be attributed to
# explorer.exe, lumping unrelated apps together. Console hosts are deliberately
# absent: for a command line player the terminal window really is the app.
_NON_APPLICATION_PARENTS = frozenset({
	"explorer.exe",
	"services.exe",
	"svchost.exe",
	"wininit.exe",
	"winlogon.exe",
	"userinit.exe",
	"csrss.exe",
	"smss.exe",
	"dllhost.exe",
	"runtimebroker.exe",
	"sihost.exe",
	"taskhostw.exe",
})

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = c_void_p(-1).value
_GA_ROOTOWNER = 3

# AudioSessionState. An expired session's audio client is gone, so its volume is
# inaudible; it is still enumerable for a while, which is the only chance to undo
# a ducked volume before Windows persists it for the next run of the app.
_SESSION_STATE_EXPIRED = 2

eRender = 0
eMultimedia = 1

# Private handles to the two DLLs. ``windll.user32`` is shared process wide, so
# pinning prototypes on it would change the behaviour of the same function
# objects inside NVDA itself; a separate ``WinDLL`` keeps that to ourselves.
# Handle-returning APIs default to a 32-bit ``c_int`` return/argument type, which
# truncates 64-bit handles on x64, hence the explicit pointer-width prototypes.
_user32 = WinDLL("user32")
_kernel32 = WinDLL("kernel32")

_user32.GetForegroundWindow.restype = c_void_p
_user32.GetForegroundWindow.argtypes = []
_user32.GetAncestor.restype = c_void_p
_user32.GetAncestor.argtypes = [c_void_p, c_uint]
_user32.GetWindowThreadProcessId.restype = DWORD
_user32.GetWindowThreadProcessId.argtypes = [c_void_p, POINTER(DWORD)]
_user32.IsWindowVisible.restype = c_bool
_user32.IsWindowVisible.argtypes = [c_void_p]
_kernel32.OpenProcess.restype = c_void_p
_kernel32.CreateToolhelp32Snapshot.restype = c_void_p
_kernel32.CloseHandle.argtypes = [c_void_p]
_kernel32.CloseHandle.restype = c_bool
_kernel32.QueryFullProcessImageNameW.argtypes = [c_void_p, DWORD, c_wchar_p, POINTER(DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = c_bool


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
		COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint), "pRetVal")),
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
	"""One audio session, plus the identity of the application it belongs to.

	``appKey`` is the session *identifier*, which Windows derives from the
	executable behind the stream: every process of a multi-process application
	(all of a browser's) shares it, and a session created later for the same
	application gets the same value. That is what makes it usable as the key for
	remembering an application's real volume across the constant creation and
	destruction of browser sessions. ``key`` is the session *instance*
	identifier, unique to this one session.
	"""

	def __init__(self, pid: int, key: str, appKey: str, state: int, volume: ISimpleAudioVolume):
		self.pid = pid
		self.key = key
		self.appKey = appKey
		self.state = state
		self.volume = volume

	@property
	def expired(self) -> bool:
		return self.state == _SESSION_STATE_EXPIRED

	def getVolume(self) -> float:
		return float(self.volume.GetMasterVolume())

	def setVolume(self, value: float):
		"""Write ``value`` unless the session is already there.

		Skipping no-op writes matters: a browser can hold dozens of sessions and
		each ``SetMasterVolume`` call notifies every client of the session and the
		volume mixer, four times a second, for as long as the mode is active.
		"""
		if abs(self.getVolume() - value) <= _VOLUME_EPSILON:
			return
		self.volume.SetMasterVolume(c_float(value), None)


def _getForegroundWindowPid() -> Optional[int]:
	"""Return the pid owning the foreground window's root, or ``None``."""
	hwnd = _user32.GetForegroundWindow()
	if not hwnd:
		return None
	# Dialogs and popup menus are owned by the window the user thinks of as the
	# application, so resolve to the root owner before asking for its process.
	root = _user32.GetAncestor(hwnd, _GA_ROOTOWNER) or hwnd
	pid = DWORD()
	_user32.GetWindowThreadProcessId(root, byref(pid))
	return int(pid.value) or None


_processNameCache: Dict[int, str] = {}


def _getProcessName(pid: int) -> str:
	"""Return the lower-cased image name (e.g. ``chrome.exe``) for a pid, or ``""``."""
	cached = _processNameCache.get(pid)
	if cached is not None:
		return cached
	name = ""
	handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
	if handle:
		try:
			buffer = create_unicode_buffer(260)
			size = DWORD(len(buffer))
			if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
				name = os.path.basename(buffer.value).lower()
		finally:
			_kernel32.CloseHandle(handle)
	_processNameCache[pid] = name
	return name


def _getParentPidMap() -> Dict[int, int]:
	"""Return a ``pid -> parent pid`` map for every running process."""
	result: Dict[int, int] = {}
	snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
	if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
		return result
	try:
		entry = _PROCESSENTRY32W()
		entry.dwSize = sizeof(_PROCESSENTRY32W)
		if not _kernel32.Process32FirstW(snapshot, byref(entry)):
			return result
		while True:
			result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
			if not _kernel32.Process32NextW(snapshot, byref(entry)):
				break
	finally:
		_kernel32.CloseHandle(snapshot)
	return result


def _getTopLevelWindowPids() -> Tuple[Set[int], Set[int]]:
	"""Return ``(visible, any)`` sets of pids owning top-level windows.

	A *visible* top-level window is the strong signal that a process is an
	application the user can see and focus. Streaming helpers, renderer children
	and other background workers do not have one, which is what lets their audio
	be attributed to the application that spawned them.

	Owning only *hidden* top-level windows is a much weaker signal, since almost
	every process ends up with one (``Default IME`` and friends), so it is kept
	separately and used only as a last resort. It is what recognises an
	application minimised to the notification area -- it really is its own app,
	even though nothing of it is on screen.
	"""
	visiblePids: Set[int] = set()
	anyPids: Set[int] = set()

	def callback(hwnd, _lparam):
		pid = DWORD()
		_user32.GetWindowThreadProcessId(hwnd, byref(pid))
		if pid.value:
			anyPids.add(int(pid.value))
			if _user32.IsWindowVisible(hwnd):
				visiblePids.add(int(pid.value))
		return 1

	# Keep a reference to the trampoline alive for the (synchronous) enumeration.
	proc = _WNDENUMPROC(callback)
	_user32.EnumWindows(proc, 0)
	return visiblePids, anyPids


class _ApplicationResolver:
	"""Maps audio-session processes to the application they belong to.

	The window list is refreshed on every pass (cheap); the process table is not
	(expensive, and parent links never change once a process exists).
	"""

	def __init__(self):
		self._parentMap: Dict[int, int] = {}
		self._parentMapTime = 0.0
		self._visiblePids: Set[int] = set()
		self._anyWindowPids: Set[int] = set()
		self._visiblePidsByName: Optional[Dict[str, Set[int]]] = None

	@property
	def visibleWindowPids(self) -> Set[int]:
		return self._visiblePids

	def beginPass(self):
		self._visiblePids, self._anyWindowPids = _getTopLevelWindowPids()
		self._visiblePidsByName = None

	def noteApplicationPid(self, pid: int):
		"""Record a pid known to own an application window.

		The foreground window is a top-level application window by definition, so
		it counts even if the window enumeration raced against it appearing.
		"""
		self._visiblePids.add(pid)
		self._anyWindowPids.add(pid)
		if self._visiblePidsByName is not None:
			self._visiblePidsByName.setdefault(_getProcessName(pid), set()).add(pid)

	def resolve(self, pid: Optional[int], foregroundAppPid: Optional[int] = None) -> Optional[int]:
		"""Map a process to the application it belongs to, best signal first.

		The parent-process chain is walked upwards, stopping at the shell and at
		service hosts, which own windows but are never the application behind a
		sound. Three answers are looked for, in descending order of confidence:

		1. A process in the chain that owns a visible top-level window. This is
		   the normal case and is what attributes a hidden renderer or audio
		   helper to the application window that spawned it.
		2. A process running the same executable that owns a visible window --
		   how a browser's audio process is reunited with its browser when the
		   parent link is unusable.
		3. A process in the chain that owns only hidden top-level windows, which
		   is what an application minimised to the notification area looks like.

		Returns ``None`` when nothing in the chain can be called an application.
		"""
		if not pid:
			return None
		if pid in self._visiblePids:
			return pid
		self._ensureProcessMap(pid)
		hiddenWindowOwner: Optional[int] = None
		seen: Set[int] = set()
		current: Optional[int] = pid
		while current and current not in seen:
			if current != pid and _getProcessName(current) in _NON_APPLICATION_PARENTS:
				break
			if current in self._visiblePids:
				return current
			if hiddenWindowOwner is None and current in self._anyWindowPids:
				hiddenWindowOwner = current
			seen.add(current)
			current = self._parentMap.get(current)
		byName, sameBinaryAppExists = self._resolveByImageName(pid, foregroundAppPid)
		if byName is not None:
			return byName
		if sameBinaryAppExists:
			# The process belongs to a windowed application we cannot pin down.
			# Treating it as an application in its own right would be worse than
			# ignoring it: a browser's audio process would then count as a
			# separate app and be ducked while the browser's own window is
			# focused, which is exactly the behaviour to avoid.
			return None
		return hiddenWindowOwner

	def _resolveByImageName(
		self, pid: int, foregroundAppPid: Optional[int]
	) -> Tuple[Optional[int], bool]:
		"""Attribute a process to a window-owning process running the same binary.

		Browsers spread audio across several processes that all run the same
		executable, and the parent link is not always usable: the audio service
		can be re-spawned, and a relaunched browser leaves children whose creator
		has exited. Matching the image name still groups them correctly. When
		several instances of that executable have windows the choice is
		ambiguous, so the focused one wins if it is a candidate -- that keeps the
		audio of the browser the user is actually in attributed to it.

		Returns the application pid (or ``None``) together with whether any
		windowed application runs the same binary at all, so the caller can tell
		"this belongs to nothing" apart from "this belongs to something I cannot
		name".
		"""
		name = _getProcessName(pid)
		if not name:
			return None, False
		candidates = self._getVisiblePidsByName().get(name)
		if not candidates:
			return None, False
		if foregroundAppPid is not None and foregroundAppPid in candidates:
			return foregroundAppPid, True
		if len(candidates) == 1:
			return next(iter(candidates)), True
		return None, True

	def _getVisiblePidsByName(self) -> Dict[str, Set[int]]:
		if self._visiblePidsByName is None:
			byName: Dict[str, Set[int]] = {}
			for windowPid in self._visiblePids:
				name = _getProcessName(windowPid)
				if name:
					byName.setdefault(name, set()).add(windowPid)
			self._visiblePidsByName = byName
		return self._visiblePidsByName

	def _ensureProcessMap(self, pid: int):
		age = time.time() - self._parentMapTime
		if age >= _PROCESS_MAP_TTL:
			self._refreshProcessMap()
		elif pid not in self._parentMap and age >= _PROCESS_MAP_MIN_INTERVAL:
			# A process we have never seen: worth a snapshot, but not on every
			# poll, or a session belonging to an already dead process would put
			# us straight back to snapshotting several times a second.
			self._refreshProcessMap()

	def _refreshProcessMap(self):
		self._parentMap = _getParentPidMap()
		self._parentMapTime = time.time()
		if self._parentMap:
			# Names are cached by pid, and Windows reuses pids freely once a
			# process exits -- something browsers cause constantly. Dropping the
			# ones that are gone keeps the cache both correct and bounded.
			for stalePid in set(_processNameCache) - set(self._parentMap):
				del _processNameCache[stalePid]


_sessionManager: Optional[IAudioSessionManager2] = None
_sessionManagerTime = 0.0


def _getAudioSessionEnumerator() -> IAudioSessionEnumerator:
	"""Return a fresh enumerator over the default endpoint's sessions.

	The enumerator itself is a snapshot and has to be recreated each time, but
	the device and its session manager are reused for a few seconds so that the
	poll does not pay for a ``CoCreateInstance`` and an ``Activate`` every time.
	Letting them expire is also what picks up a change of default output device.
	"""
	global _sessionManager, _sessionManagerTime
	if _sessionManager is not None and time.time() - _sessionManagerTime >= _SESSION_MANAGER_TTL:
		_sessionManager = None
	if _sessionManager is None:
		deviceEnumerator = CreateObject(MMDeviceEnumerator, interface=IMMDeviceEnumerator)
		device = deviceEnumerator.GetDefaultAudioEndpoint(eRender, eMultimedia)
		_sessionManager = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
		_sessionManagerTime = time.time()
	try:
		return _sessionManager.GetSessionEnumerator().QueryInterface(IAudioSessionEnumerator)
	except Exception:
		# The endpoint went away (device unplugged, driver restarted); drop the
		# cached manager so the next pass builds one for the current device.
		_sessionManager = None
		raise


def _invalidateSessionManager():
	global _sessionManager
	_sessionManager = None


def iterAudioSessions() -> Iterator[AudioSessionVolume]:
	enumerator = _getAudioSessionEnumerator()
	count = enumerator.GetCount()
	for index in range(count):
		try:
			session = enumerator.GetSession(index)
			control2 = session.QueryInterface(IAudioSessionControl2)
			pid = int(control2.GetProcessId())
			# Skip the system sounds session (pid 0), NVDA itself, and non-application
			# audio producers such as the Windows audio engine.
			if not pid or pid == _OWN_PID or _getProcessName(pid) in _EXCLUDED_PROCESS_NAMES:
				continue
			state = int(control2.GetState())
			try:
				key = control2.GetSessionInstanceIdentifier()
			except Exception:
				key = "index:%d" % index
			try:
				appKey = control2.GetSessionIdentifier()
			except Exception:
				appKey = key
			volume = session.QueryInterface(ISimpleAudioVolume)
		except Exception:
			# Sessions are created and destroyed while we walk the snapshot --
			# a browser tab opening or closing is enough. One that dies mid-pass
			# must not abort the whole enumeration.
			log.debugWarning("Enhanced Duck: skipping an unreadable audio session", exc_info=True)
			continue
		yield AudioSessionVolume(pid, key, appKey, state, volume)


def _rampedLevel(
	level: float, target: float, original: float, depth: float, elapsed: float, rampTimeMs: int
) -> float:
	"""Return ``level`` moved towards ``target`` by one tick's worth of ramp.

	The rate is the whole ducking range -- from the application's own volume down
	to the ducking depth -- divided by the configured ramp time, so a full duck or
	release takes exactly that long. Deriving it from the range rather than from
	the distance still to go keeps the slope constant: a ramp that is interrupted
	half way (focus bouncing between two applications) turns round at the same
	rate instead of crawling, and it cannot overshoot, since the target is also
	the limit.

	``elapsed`` is in seconds, and is measured rather than assumed, so a tick that
	the main thread served late moves the volume by what that delay was worth
	instead of stretching the ramp.
	"""
	if rampTimeMs <= 0 or elapsed <= 0:
		return target
	span = original * (1.0 - depth)
	if span <= 0:
		# Nothing to ramp across: a depth of 100% does not duck at all, and an
		# application already at zero has nothing to lower.
		return target
	step = span * (elapsed * 1000.0 / rampTimeMs)
	if step <= 0:
		return level
	if target < level:
		return max(target, level - step)
	return min(target, level + step)


class FocusAwareSessionDucker:
	"""Holds every audio session at the volume the current mode asks for.

	A pass over the audio sessions (every ``POLL_INTERVAL_MS``) decides which
	*applications* should be ducked. Reaching the level each one should be at is a
	separate job, run on a ``RAMP_INTERVAL_MS`` timer while any volume is still
	moving, so that ducking and unducking fade over the configured attack and
	release times rather than stepping.
	"""

	def __init__(self):
		self._mode: Optional[int] = None
		self._callLater = None
		self._resolver = _ApplicationResolver()
		self._settings: DuckingSettings = getDuckingSettings()
		# Keyed by application (see ``AudioSessionVolume.appKey``) rather than by
		# session, so an application whose sessions are recreated -- the normal
		# state of affairs for a browser -- is restored to the volume it really
		# had instead of to whatever it was left at while ducked.
		self._originalVolumes: Dict[str, float] = {}
		# The volume currently written for each application. An entry lives from
		# the moment an application starts being ducked until it has arrived back
		# at its original volume, which is also what makes a release ramp survive
		# the application dropping out of the ducked set.
		self._levels: Dict[str, float] = {}
		# Filled in by each pass: the applications that should be ducked, their
		# sessions, and which of them have a session that is not expired.
		self._targetAppKeys: Set[str] = set()
		self._sessionsByApp: Dict[str, List[AudioSessionVolume]] = {}
		self._audibleApps: Set[str] = set()
		self._rampInProgress = False
		self._lastPassTime = 0.0
		self._lastTickTime = 0.0
		self._lastForegroundAppPid: Optional[int] = None

	def start(self, mode: int):
		self._mode = mode
		self._settings = getDuckingSettings()
		self._lastPassTime = 0.0
		self._lastTickTime = time.monotonic()
		self._schedule(immediate=True)

	def stop(self):
		self._mode = None
		self._cancelTimer()
		self._restoreAll()
		self._lastForegroundAppPid = None
		_invalidateSessionManager()

	def _schedule(self, delayMs: int = POLL_INTERVAL_MS, immediate: bool = False):
		self._cancelTimer()
		self._callLater = core.callLater(0 if immediate else delayMs, self._poll)

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
			now = time.monotonic()
			# Deciding what to duck is the expensive half, so it keeps its own
			# interval even when ramp ticks are running in between. Half a tick of
			# slack keeps a timer that fires a shade early from pushing the pass
			# out to the tick after it.
			if (now - self._lastPassTime) * 1000.0 >= POLL_INTERVAL_MS - RAMP_INTERVAL_MS / 2.0:
				self._lastPassTime = now
				self._settings = getDuckingSettings()
				self._refreshTargets()
			self._applyLevels(now)
		except Exception:
			log.exception("Error while updating Enhanced Duck audio sessions")
		finally:
			if self._mode in (DUCK_INACTIVE_WINDOWS, DUCK_ACTIVE_WINDOWS):
				self._schedule(RAMP_INTERVAL_MS if self._rampInProgress else POLL_INTERVAL_MS)

	def _getForegroundAppPid(self) -> Optional[int]:
		"""Return the focused application's pid, tolerating focus-less moments.

		``GetForegroundWindow`` returns nothing while focus moves between
		windows, and browsers cause that often (opening a window, a page load
		stealing focus, native menus). Treating those moments as "no application
		is focused" would unduck and immediately re-duck everything, which is
		audible as pumping, so the last known application is kept until either a
		real one turns up or it stops owning any window.
		"""
		pid = _getForegroundWindowPid()
		if pid:
			self._resolver.noteApplicationPid(pid)
			self._lastForegroundAppPid = pid
			return pid
		last = self._lastForegroundAppPid
		if last is not None and last not in self._resolver.visibleWindowPids:
			# The focused application has closed; there is nothing to fall back on.
			self._lastForegroundAppPid = None
			return None
		return last

	def _refreshTargets(self):
		"""Work out which applications should be ducked, and where their sessions are."""
		# Resolve every player to the top-level application it belongs to, so an
		# app that streams through a hidden helper process is treated as a single
		# application together with its window.
		resolver = self._resolver
		resolver.beginPass()
		foregroundAppPid = self._getForegroundAppPid()

		sessions: List[AudioSessionVolume] = list(iterAudioSessions())
		sessionAppPids = [resolver.resolve(session.pid, foregroundAppPid) for session in sessions]
		appPids = {appPid for appPid in sessionAppPids if appPid is not None}
		targetAppPids = self._getTargetAppPids(appPids, foregroundAppPid)

		sessionsByApp: Dict[str, List[AudioSessionVolume]] = {}
		audibleApps: Set[str] = set()
		targetAppKeys: Set[str] = set()
		for session, appPid in zip(sessions, sessionAppPids):
			sessionsByApp.setdefault(session.appKey, []).append(session)
			# Expired sessions produce no sound, so they never decide what gets
			# ducked. An application all of whose sessions have expired therefore
			# drops out of the target set and is restored, which is the point:
			# that restore is the last volume Windows will persist for it, and
			# leaving it at the ducked value is what would make a browser whose
			# audio service or tab went away come back permanently quiet.
			if session.expired:
				continue
			audibleApps.add(session.appKey)
			if appPid is not None and appPid in targetAppPids:
				targetAppKeys.add(session.appKey)

		self._sessionsByApp = sessionsByApp
		self._audibleApps = audibleApps
		self._targetAppKeys = targetAppKeys

		# An application that has no sessions left at all is gone: there is
		# nothing to write a restore to, and remembering its volume would only
		# pin a value that no longer means anything.
		for goneKey in set(self._levels) - set(sessionsByApp):
			self._levels.pop(goneKey, None)
			self._originalVolumes.pop(goneKey, None)

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

	def _applyLevels(self, now: float):
		"""Move every tracked application one tick closer to the level it should be at."""
		elapsed = max(0.0, now - self._lastTickTime)
		self._lastTickTime = now
		ramping = False
		for appKey in list(self._levels):
			# Keep one uncooperative session from aborting the whole tick.
			try:
				if self._applyAppLevel(appKey, elapsed):
					ramping = True
			except Exception:
				log.exception("Enhanced Duck: could not adjust an audio session")
		# Applications that have only just become targets are picked up after the
		# pass above, so their first move happens on the next tick, one ramp
		# interval from now. Starting them here instead would hand them the whole
		# gap since the last tick -- up to a full poll interval -- and a short
		# attack time would be spent in a single step, which is the jump the
		# ramp exists to avoid.
		for appKey in self._targetAppKeys - set(self._levels):
			try:
				if self._trackApplication(appKey):
					ramping = True
			except Exception:
				log.exception("Enhanced Duck: could not read an audio session's volume")
		self._rampInProgress = ramping

	def _trackApplication(self, appKey: str) -> bool:
		"""Start tracking an application at its own volume. Returns whether it has to ramp."""
		sessions = [session for session in self._sessionsByApp.get(appKey, ()) if not session.expired]
		if not sessions:
			return False
		original = self._originalVolumes.get(appKey)
		if original is None:
			original = sessions[0].getVolume()
			self._originalVolumes[appKey] = original
		self._levels[appKey] = original
		return abs(original - self._targetLevel(appKey, original)) > _LEVEL_EPSILON

	def _targetLevel(self, appKey: str, original: float) -> float:
		"""Return the volume ``appKey`` should end up at.

		The ducked level is relative to the application's own volume, so ducking
		always quietens by the same proportion instead of pulling an application
		the user has already turned down back up to a fixed level.
		"""
		if appKey in self._targetAppKeys:
			return original * self._settings.depth
		return original

	def _applyAppLevel(self, appKey: str, elapsed: float) -> bool:
		"""Write one tick of ``appKey``'s ramp. Returns whether it is still ramping."""
		original = self._originalVolumes.get(appKey)
		if original is None:
			self._levels.pop(appKey, None)
			return False
		ducked = appKey in self._targetAppKeys
		target = self._targetLevel(appKey, original)
		level = self._levels[appKey]
		if not ducked and appKey not in self._audibleApps:
			# Nothing of this application is playing any more, so there is no fade
			# for anyone to hear -- and its sessions can vanish at any moment,
			# which would leave Windows persisting whatever level the ramp had
			# reached. Go straight back to the original volume.
			level = target
		elif abs(level - target) > _LEVEL_EPSILON:
			rampTimeMs = (
				self._settings.attackTimeMs if target < level else self._settings.releaseTimeMs
			)
			level = _rampedLevel(level, target, original, self._settings.depth, elapsed, rampTimeMs)
		else:
			level = target
		self._levels[appKey] = level
		for session in self._sessionsByApp.get(appKey, ()):
			# Writing a ducked level to an expired session would only
			# desynchronise the application's volume mixer slider from what is
			# audible. A restore is written to them on purpose: the level they are
			# left at is the one Windows persists for the application's next run.
			if session.expired and ducked:
				continue
			session.setVolume(level)
		if abs(level - target) <= _LEVEL_EPSILON:
			if not ducked:
				# Back at its own volume and no longer a target: forget it, so
				# that the next time it is ducked its volume is read afresh.
				self._levels.pop(appKey, None)
				self._originalVolumes.pop(appKey, None)
			return False
		return True

	def _restoreAll(self):
		"""Put every tracked application back at its own volume, at once.

		Leaving the mode is not something to fade out of: this also runs when the
		add-on or NVDA itself is shutting down, and a ramp needs a timer that will
		not be there. Anything that cannot be written now would be left ducked.
		"""
		try:
			sessions: Iterable[AudioSessionVolume] = list(iterAudioSessions())
		except Exception:
			log.exception("Error while restoring Enhanced Duck audio sessions")
			sessions = ()
		for session in sessions:
			original = self._originalVolumes.get(session.appKey)
			if original is None:
				continue
			try:
				session.setVolume(original)
			except Exception:
				log.exception("Enhanced Duck: could not restore an audio session")
		self._levels.clear()
		self._originalVolumes.clear()
		self._targetAppKeys.clear()
		self._sessionsByApp = {}
		self._audibleApps = set()
		self._rampInProgress = False
