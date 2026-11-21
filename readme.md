# Enhanced Duck

Enhanced Duck is an NVDA add-on that extends NVDA's audio ducking with two
focus-aware modes. In addition to NVDA's built-in modes, it can automatically
lower the volume of other running applications based on which window is focused.

## Modes

Press `NVDA+shift+d` to cycle through the available ducking modes:

* No ducking
* Duck when outputting speech and sounds
* Always duck
* Duck inactive applications
* Duck the active application

The first three are NVDA's standard mode selections. Enhanced Duck replaces the
fixed volume adjustment used by **Duck when outputting speech and sounds** and
**Always duck**, so the configured volume multiplier, attack time, and release
time apply to those modes too. The last two modes are added by this add-on:

* **Duck inactive applications** lowers the audio of every application that is
  not currently focused, so the app you are working in stays at full volume.
* **Duck the active application** lowers the audio of the application that is
  currently focused, leaving background audio (for example, music) at full
  volume.

As focus moves between applications, the add-on continually updates which audio
sessions are lowered, and it restores every application's original volume when
you leave the mode or disable ducking. NVDA's own speech and sounds are never
lowered.

The mode can also be chosen from NVDA Menu > Preferences > Settings > Audio, in
the **Audio ducking mode** combo box (the two new entries appear at the end of
the list). Changing configuration profiles is respected as well.

## Ducking settings

Open NVDA Menu > Preferences > Settings > **Enhanced Duck** to configure every
ducking mode except **No ducking**:

* **Ducking volume multiplier** is the fraction of an application's original
  volume that remains while it is ducked. For example, `0.25` keeps 25 percent
  of the original volume. Valid values range from `0.00` to `1.00`.
* **Attack time** is the number of milliseconds used to move from the original
  volume to the ducked volume.
* **Release time** is the number of milliseconds used to return to the original
  volume after ducking ends.
* **Ducking volume adjustment step** controls how much `NVDA+-` and `NVDA+=`
  change the multiplier. It may range from `0.0001` to `1.0000`.
* **Attack adjustment step** and **release adjustment step** control how many
  milliseconds the corresponding keyboard commands add or subtract. They may
  range from 1 to 60000 milliseconds.
* **Modes included when cycling with NVDA+Shift+D** is a checklist controlling
  which modes the shortcut visits. At least one mode must remain checked.

Attack and release values may range from 0 to 60000 milliseconds. Zero applies
the change immediately. Non-zero values produce a perceptually even transition,
and a transition can safely reverse when focus or ducking mode changes partway
through it. In **Duck when outputting speech and sounds**, the release begins
when NVDA releases its last output reference. The configured release replaces
NVDA's fixed one-second hold. Attack runs asynchronously alongside speech and
sounds. The speech-triggered mode uses NVDA's output notifications and requires
an NVDA installation with audio ducking support.

The defaults are a `0.20` multiplier, 150 milliseconds of attack and 600
milliseconds of release. Settings can vary by NVDA configuration profile.
Existing profile values are carried forward, including the percentage-based
depth from version 0.2.0. Invalid configuration-file values fall back to the
last valid settings or the defaults at startup. All five cycling modes are
checked by default. If the current mode is not checked, the
next shortcut press starts at the first checked mode.

## Keyboard adjustments

The current profile's ducking parameters can be changed without opening
Settings. Each command announces its new value and retargets active audio
without restarting the selected ducking mode:

* `NVDA+-` lowers the ducked volume multiplier, making ducking more aggressive.
* `NVDA+=` raises the ducked volume multiplier, making ducking less aggressive.
* `NVDA+Ctrl+-` and `NVDA+Ctrl+=` decrease and increase attack time.
* `NVDA+Alt+-` and `NVDA+Alt+=` decrease and increase release time.

The adjustment steps are configured in the Enhanced Duck settings category.
Their defaults are 0.05 for volume and 50 ms for both attack and release. The
commands can be reassigned under **Audio** in NVDA's **Input Gestures** dialog.

The same limits used by the Settings panel are enforced, so repeated key
presses cannot move volume outside 0.00–1.00 or transition times outside
0–60000 ms. Rapid key-repeat bursts are coalesced to prevent repeated Core Audio
discovery while still applying the newest value at the next configuration refresh.

## How the focus-aware modes work

The focus-aware modes are built on the Windows Core Audio session API. The add-on
adjusts the per-application volume that Windows exposes in its volume mixer, so
ducking works independently of NVDA's built-in WASAPI ducking and does not
require an installed copy or UI access.

Windows groups audio by process, but the process that plays a sound is often not
the one that owns the window you focus. Web browsers are the extreme case: a
Chromium based browser renders its audio in a separate sandboxed service process
and gives every tab its own renderer, and Firefox spreads audio across content
processes. The add-on therefore works out which *application* each audio session
belongs to, by following the session's process up to the window it belongs to,
and falling back to matching the executable when that link is unusable. All of a
browser's processes are treated as the one application that its window
represents, so focusing a browser window affects all of its audio, and audio from
one of its helper processes is never mistaken for a separate background app.

Keep the following in mind:

* The add-on reduces active sessions and restores tracked sessions that become
  inactive or expire, while Windows still exposes them.
* Audio that cannot be traced to any application window is left alone. Sounds
  played by Windows itself, by the audio engine, and by NVDA are never lowered.
* If two separate copies of the same program are running with windows of their
  own, audio from a helper process that cannot be traced to either is attributed
  to whichever of them is focused, and otherwise left alone.
* Only the current default playback device is managed. If you change output
  device while an application is ducked, that application may keep the lowered
  volume on the old device.

Enhanced Duck runs Core Audio discovery and volume updates on a dedicated worker,
so fade ticks do not occupy NVDA's interface thread. It uses inexpensive
foreground checks and performs heavier process and session discovery less often.
Inactive sessions and expired timers are released from tracking. Fades use
five-millisecond volume updates and a logarithmic, eased curve to soften the
start and end of each transition. Windows session-volume controls still change
in discrete steps, so very short fades or large volume changes may remain
audible; try a longer attack or release time if that happens.

Before reducing an application's session volume, Enhanced Duck saves its
original value in a small recovery file inside the NVDA configuration folder.
The entry is removed after restoration, and entries whose application has since
exited are discarded. If NVDA or the add-on stops unexpectedly, the next start
restores any matching sessions before ducking begins again. A normal exit or
restart also restores volumes without briefly re-enabling NVDA's system-wide
native ducking effect.

Windows remembers each application's volume-mixer level across runs, so this
recovery file is what keeps an unclean exit from leaving applications quiet. It
can only match sessions that are still open: if the machine is restarted, or an
application is closed and reopened, before NVDA runs again, that application
keeps its lowered mixer level and has to be turned back up by hand. This applies
to every ducking mode except **No ducking**, including **Duck when outputting
speech and sounds**.

## Building

Install the [NVDA add-on build requirements][1], then run:

```
scons
```

The generated `enhancedDuck-<version>.nvda-addon` file uses metadata from
`buildVars.py` and documentation from this `readme.md`.

[1]: https://github.com/nvdaaddons/AddonTemplate

## Store notes

The manifest metadata follows the current Add-on Store validation shape: a
unique alphanumeric add-on name, a semantic `major.minor.patch` version, and
valid NVDA API versions for `minimumNVDAVersion` and `lastTestedNVDAVersion`.

Before publishing, set a real HTTPS project or support URL in the `addon_url`
value in `buildVars.py`.
