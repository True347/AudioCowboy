# AudioCowboy 🤠🔊

A [Flow Launcher](https://www.flowlauncher.com/) plugin to **quickly switch the default audio input/output device** and **save/load device profiles** — a rofi-style multi-layer menu under a single keyword.

```
ac            →  🔊 Output   🎤 Input   📁 Profiles   💾 Save
ac o [filter] →  pick the default output (playback) device
ac i [filter] →  pick the default input  (recording) device
ac s <name>   →  save the current input+output as a named profile
ac p [filter] →  list profiles → Enter applies; right-click → Apply / Rename / Delete
ac r [filter] →  rename a profile (pick one, then type the new name)
ac d [filter] →  delete profiles (Enter removes; the list stays open)
ac <name>     →  apply a profile straight from the top level (matches profile names)
```

Switching sets **all three audio roles** (Console + Multimedia + Communications), so apps like Teams / Discord / Zoom follow the change too.

---

## How it works

Flow Launcher's plugin model has no native nested menu, so AudioCowboy fakes the rofi
multi-layer experience by **rewriting the search box** (`Flow.Launcher.ChangeQuery`)
to drill down a level while keeping the window open. One keyword (`ac`), several
sub-commands.

There is no public Windows API to *set* the default device — every tool wraps the
undocumented `IPolicyConfig` COM interface. AudioCowboy uses **`pycaw`** (pure Python),
so the plugin needs **no external executable** and no admin rights.

Profiles store each device by its **MMDevice endpoint ID** (stable across reboots and
unique even for two identical headsets) — so a profile re-selects the right device even
when friendly names collide. That ID is *not* stable across driver changes, though: a
USB DAC switched between **UAC1 and UAC2** is a different endpoint to Windows each time.
So applying a profile looks a device up in three steps:

1. **A known endpoint ID** that is currently active (a profile can hold several — one per DAC mode).
2. **A known stable ID** — Windows 11 24H2+ `PKEY_AudioEndpoint_StableId`, which survives
   driver and OS updates.
3. **The friendly name**, ignoring Windows' `2- ` / `(2)` numbering — used only when exactly
   one active device matches; with several, nothing is guessed.

Windows also re-creates a device's endpoint under a brand-new ID now and then, with no
USB change at all, deleting the old one. A name match is written into the profile only in
that case — when every endpoint the profile knew is gone — so its stable ID is tracked
from then on. Otherwise (the old endpoint still exists, e.g. unplugged) a name match might
be the other of two identical headsets, so it's applied but not stored. To make another
endpoint permanent, **link** it: Shift+Enter on the profile → *Use “…” as this profile's
output/input*.

---

## Install

**From the Flow Launcher plugin store** (once listed): open Flow, type
`pm install AudioCowboy`, or find it in Settings → Plugin Store.

**From a release zip** (or to test before the store listing):
```
pm install https://github.com/True347/AudioCowboy/releases/latest/download/Flow.Launcher.Plugin.AudioCowboy.zip
```
The release zip bundles the pure-Python backend (`lib/`), so it works out of the box —
no setup step. Restart Flow (or reload plugins) and type `ac`.

**Manual / from source** (developers): copy the folder into
`%APPDATA%\FlowLauncher\Plugins\AudioCowboy\`, then vendor the backend with
`pip install -r requirements.txt -t lib`. Restart Flow.

---

## Usage

| You type | What happens |
|---|---|
| `ac` | Top menu. Each item drills into the next layer; the current default devices are shown. |
| `ac o` | List active **output** devices. The current default is marked `✓`. Enter sets a new default. |
| `ac o hdmi` | Same, filtered to devices whose name contains "hdmi". |
| `ac i` | List active **input** devices; Enter sets the default. |
| `ac s Gaming` | Save the current output+input devices as a profile named "Gaming" (overwrites if it exists). |
| `ac p` | List saved profiles. Enter applies a profile; **Shift+Enter** opens its context menu (Apply / Link / Rename / Delete). A `⚠` marks a profile whose device can't be found right now; its context menu then offers the active devices to **link** (see *How it works*). The list also offers **Rename** and **Delete** entries. |
| `ac p game` | Same, filtered by profile name. The best match (exact, then starts-with, then contains) is highlighted, so Enter applies it. |
| `ac gaming` | Apply a profile **straight from the top level**: anything that isn't a sub-command is matched against profile names, ranked like `ac p`. If no name matches, the top menu is shown. A profile whose name starts with a sub-command word (e.g. `Out Loud`) is still reachable with `ac p`. |
| `ac r` | Pick a profile, then type a new name (`Gaming → Streaming`) and press Enter. Renaming onto a name already in use is refused. |
| `ac d` | List profiles; Enter deletes the highlighted one. The list stays open so you can remove several in a row. |

On a device, **Shift+Enter** offers *Set for all roles* and *Set as Communication device only*.

---

## Profiles & data

Profiles are stored as JSON **outside** the plugin folder (so they survive plugin updates):

```
%APPDATA%\FlowLauncher\Settings\Plugins\AudioCowboy\profiles.json
```

```jsonc
{
  "schemaVersion": 1,
  "profiles": [
    {
      "name": "Gaming",
      "created": "2026-06-17T10:32:00Z",
      "output": {
        "endpointId": "{0.0.0.00000000}.{guid-uac2}",   // the ID it was saved with
        "friendlyName": "Speakers (FiiO K11)",
        "altIds": ["{0.0.0.00000000}.{guid-uac1}"],     // linked: same DAC in its other mode
        "stableIds": ["…"]                              // Windows 11 24H2+ only
      },
      "input":  { "endpointId": "{0.0.1.00000000}.{guid}", "friendlyName": "Mic (HyperX QuadCast)" }
    }
  ]
}
```

`altIds` and `stableIds` are optional (older profiles just gain them as they're used).
Re-saving a profile keeps them when the current device is one it already knows.

Writes are atomic (temp file + `os.replace`), so a crash mid-write can't corrupt the file.

---

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `show_disconnected` | off | Also list unplugged / disabled endpoints. |

---

## Troubleshooting

- **"⚠ Audio backend unavailable"** — the bundled pycaw backend (in `lib/`) failed to load;
  reinstall the plugin. Running from source? Vendor it with `pip install -r requirements.txt -t lib`.
- **A device won't switch** — make sure it's *Active* (plugged in / enabled) in Windows Sound settings.
- **A profile can't find a device that is plugged in** (e.g. after switching a DAC between
  UAC1/UAC2, or a driver update) — Shift+Enter on the profile and pick *Use “…” as this
  profile's output/input*; it's remembered from then on. `python tests\inspect_ids.py <name>`
  shows each endpoint's ID, stable ID and state, to see what changed.
- **Nothing happens / errors** — check Flow's logs at `%APPDATA%\FlowLauncher\Logs\`.

---

## Backend

**pycaw** ([`pycaw`](https://github.com/AndreMiras/pycaw) `>= 20251023`, which added
`SetDefaultDevice`, plus `comtypes`) — pure Python, vendored into `lib/`. It's the only
backend, needs no external executable, and is what ships in the release.

To publish to the Flow plugin store, see [publish/PUBLISHING.md](publish/PUBLISHING.md).

---

## Development

```
plugin.json          # manifest (keyword: ac, Language: python)
main.py              # JSON-RPC entry + router + actions
audio.py             # pycaw backend
profiles.py          # profiles.json CRUD (atomic, update-safe path)
SettingsTemplate.yaml
Images/              # icon set
tools/gen_icons.py   # regenerate icons (Pillow)
tests/               # smoke_test.py (offline) + inspect_live.py / inspect_actions.py / inspect_ids.py
docs/RESEARCH.md     # design + feasibility research report
```

Run the offline tests (no Flow needed):

```powershell
python tests\smoke_test.py
```

Inspect live output against your real devices:

```powershell
python tests\inspect_live.py        # read-only: menus + device lists
python tests\inspect_actions.py     # save/load/delete in a temp dir (no-op switch)
python tests\inspect_ids.py [name]  # read-only: endpoint IDs, stable IDs, states
```

### Why one keyword instead of `aci`/`aco`/`acs`/`acp`?

In Flow Launcher's V1 Python model (`Language: "python"`), the request the plugin
receives **does not include which action keyword fired** — only the search text after
it. So separate keywords couldn't be told apart on an empty query. A single keyword
with sub-commands (`ac o`, `ac i`, …) is the robust choice and faithfully reproduces
the original rofi single-entry, multi-layer menu. (Distinguishing keywords would
require the newer `Python_v2` protocol.)

---

## Credits

- [pycaw](https://github.com/AndreMiras/pycaw) — the pure-Python Core Audio backend that powers the plugin.
- [Flow Launcher](https://github.com/Flow-Launcher/Flow.Launcher) — the launcher and plugin API.
- Inspired by [SoundSwitch](https://github.com/Belphemur/SoundSwitch) and rofi audio-menu workflows.
