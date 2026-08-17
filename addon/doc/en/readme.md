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

The first three are NVDA's standard ducking modes. The last two are added by
this add-on:

* **Duck inactive applications** lowers the audio of every application that is
  not currently focused, so the app you are working in stays at full volume.
* **Duck the active application** lowers the audio of the application that is
  currently focused, leaving background audio (for example, music) at full
  volume.

As focus moves between applications, the add-on continually updates which audio
sessions are lowered, and it restores every application's original volume when
you leave the mode or disable ducking. NVDA's own speech and sounds are never
lowered.

An application's original volume is remembered per application rather than per
audio session, so a program that closes and reopens its audio while ducked --
which browsers do constantly, one session per tab -- is still restored to the
volume you had set for it, instead of being left quiet in the Windows volume
mixer.

The mode can also be chosen from NVDA Menu > Preferences > Settings > Audio, in
the **Audio ducking mode** combo box (the two new entries appear at the end of
the list). Changing configuration profiles is respected as well.

## How the focus-aware modes work

The two extra modes are built on the Windows Core Audio session API. The add-on
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

* An application is only affected while it has an audio session, that is, from
  the moment it opens the audio device rather than only while sound is audible.
* Audio that cannot be traced to any application window is left alone. Sounds
  played by Windows itself, by the audio engine, and by NVDA are never lowered.
* If two separate copies of the same program are running with windows of their
  own, audio from a helper process that cannot be traced to either is attributed
  to whichever of them is focused, and otherwise left alone.
* Only the current default playback device is managed. If you change output
  device while an application is ducked, that application may keep the lowered
  volume on the old device.

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
