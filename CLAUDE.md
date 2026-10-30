# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AudioCowboy is a **Flow Launcher plugin** (Windows) that switches the default audio
input/output device and saves/loads device profiles, all under one action keyword (`ac`)
with a rofi-style multi-layer menu. Pure Python, no build step. The only first-party code
is `main.py`, `audio.py`, `profiles.py`, and `tests/` + `tools/` — everything under `lib/`
is **vendored third-party deps** (pycaw, comtypes, psutil), not to be edited.

## Commands

```bash
# Offline test suite — runs cross-platform (no Flow, no real audio):
python tests/smoke_test.py        # profiles CRUD, device helpers, JSON-RPC wire layer

# Live checks — Windows only, against real devices:
python tests/inspect_live.py      # read-only: dumps the menus + device lists
python tests/inspect_actions.py   # save/load/delete in a temp APPDATA (switch is a no-op)
python tests/inspect_ids.py [name]  # read-only: endpoint id / stable id / state per endpoint

# Vendor the runtime deps the way CI/release does (needed to run the pycaw backend locally):
pip install -r requirements.txt -t ./lib
```

There is no single-test runner; `smoke_test.py` calls its `test_*` functions directly in
`__main__`. To run one, edit that block or invoke the function from a REPL.

`smoke_test.py` is the gate that matters for first-party logic — it shells out to `main.py`
as a subprocess exactly the way Flow does, so it catches wire-format regressions. Run it
after any change to `main.py` / `audio.py` / `profiles.py`.

## Runtime model (Flow Launcher V1 Python protocol)

Flow invokes `python main.py '<json-request>'` — the request is a single JSON arg in
`sys.argv[1]`, and the response is a **single JSON object on stdout**. This shapes
everything:

- `method` routes to a method on the `AudioCowboy` class (`query`, `context_menu`, or an
  action like `load_profile`). `query`/`context_menu` return a list, wrapped as
  `{"result": [...]}`. Action methods perform a side effect and may emit **one**
  `Flow.Launcher.*` follow-up request instead (we use `ShowMsg` for toasts).
- **Exactly one payload may be written to stdout** — the `_emitted` flag enforces this.
  Never `print()` to stdout; it corrupts the response.
- **Flow raises `InvalidDataException` on ANY stderr output** — a single non-fatal warning
  kills every query. This bit us in v1.0.1: pycaw warns (to stderr) when a device's
  properties raise a COMError (some JBL / virtual-audio endpoints do). So warnings are
  suppressed globally (`warnings.filterwarnings("ignore")` in `main.py`) plus a
  `catch_warnings` guard around the pycaw calls in `audio.py`. The plugin must never write
  to stderr; surface diagnostics via result items / `ShowMsg` instead.
- The whole `query`/`context_menu` handler is wrapped in try/except so the plugin never
  crashes silently — errors become a result item (queries) or an error toast (actions).

### The single-keyword / drill-down design (don't "fix" it)

There is only one `ActionKeyword` (`ac`), and sub-commands are parsed from the query text
(`ac o`, `ac i`, `ac s <name>`, `ac p`, `ac r`, `ac d`). Profile ops live under Profiles,
not the top menu, to keep the model object-first (list → Enter applies); `ac r` renames
(encodes `Old → New` in the box, split on the `_RENAME_SEP` arrow against known names so
spaces don't break parsing) and `ac d` deletes (re-lists after each delete via
`delete_profile_relist` so several can go in a row). Any query whose first word isn't a
sub-command falls through to profile-name matching (`ac gaming` applies "Gaming" from the
root; no match → top menu), ranked by `_name_rank` (exact > prefix > substring, always above
the Save/Rename/Delete entries so Enter applies the best match — issue #1). Sub-command words
win, so a profile named e.g. "Out Loud" is only reachable via `ac p`. This is deliberate: in the V1 Python model the
request does **not** include which keyword fired, so separate keywords (`aco`/`aci`/…)
can't be distinguished on an empty query. The nested-menu feel is faked by `drill()`,
which returns a `Flow.Launcher.ChangeQuery` action that rewrites the search box and keeps
the window open (`DontHideAfterAction: True`). Splitting into multiple keywords would
require migrating to the `Python_v2` protocol.

## Architecture

Three first-party modules with a clean split:

- **`main.py`** — JSON-RPC entry, router (`query` → `_*_menu` builders), context menus, and
  action handlers. `result()`/`drill()` build Flow result items; `_emit()`/`_flow()` are
  the one-payload stdout writers. Prepends the plugin dir and `lib/` to `sys.path` so
  imports work regardless of Flow's CWD.
- **`audio.py`** — the audio backend: the pure-Python **pycaw** backend (vendored into
  `lib/`) is the only backend — feature-detected via `hasattr(AudioUtilities,
  "SetDefaultDevice")`, requires pycaw ≥ 20251023. It produces normalized device dicts
  keyed on the MMDevice endpoint ID. Errors are `AudioError` / `AudioBackendUnavailable`.
  There is **no external binary** — the plugin never bundles or downloads one.
- **`profiles.py`** — `profiles.json` CRUD. Storage lives **outside the plugin folder**
  (`%APPDATA%\FlowLauncher\Settings\Plugins\AudioCowboy\`) because Flow wipes the install
  dir on every update. Writes are atomic (temp file + `os.replace`); corrupt/invalid JSON
  is backed up to `.bak` rather than clobbered, and `load()` never raises.

### Invariants that pervade the code

- **Device identity is the MMDevice endpoint ID** (e.g. `{0.0.0.00000000}.{guid}`) —
  stable across reboots and unique even for two identical headsets — but it changes with
  the driver, so one physical device can have several (a DAC switched UAC1 ↔ UAC2 is a
  new endpoint each way). A profile slot therefore holds `endpointId` + optional `altIds`
  and `stableIds` (`PKEY_AudioEndpoint_StableId`, Win11 24H2+), and `audio.resolve()`
  maps a slot to an **Active** endpoint: known ids in order (primary wins) → known stable
  id → the one active endpoint whose `name_key` (Windows' `2- `/`(2)` numbering stripped)
  matches. Several name matches → "ambiguous", never a guess.
- **Only exact identity is persisted** (`main._learn` → `profiles.remember_device`). A
  stable-id hit stores the new endpoint id; an id hit backfills the stable id. A **name**
  hit is applied but stored only when Windows has *deleted* every endpoint the slot knew
  (none listed in any state) — then the slot is re-pointed (dead ids dropped) so its
  stable id gets tracked. Windows does re-create endpoints under new ids with no USB
  change (seen on a FiiO K11: delete + create in one PnP instant, monthly-ish). While a
  known endpoint still exists (even unplugged), a name hit may be the other of two
  identical headsets, so it is never stored; the user merges endpoints explicitly via the
  profile context menu's link entries (`link_device`). `save_profile` carries learned ids
  over only when the new device's id or stable id is already known for that slot.
- **A backend "success" is not proof.** After any `set_default`, the code re-reads the
  device list to *verify* the default actually changed (`_is_now_default`, and the retry
  loop in `load_profile`), retrying a few times because Windows can apply the change just
  after the call returns. Verified state — not the return value — is the source of truth.
  Outcomes are reported honestly as applied / unverified / failed / absent.
- "Set default" means **all three roles** (Console + Multimedia + Communications) so
  Teams/Discord/Zoom follow; `role="2"` / comm-only is a separate context-menu action.
  The pycaw backend cannot read the *communications* default, so comm verification
  returns `None` (unverifiable) there — which is the shipped configuration.

## Packaging & release

`.github/workflows/Publish Release.yml` runs on push to `main` (ignoring docs/tests/md),
**on `windows-latest`** (required — pycaw pulls in psutil, which ships a platform-native
binary; vendoring on Linux would package the wrong build). It re-vendors `lib/` from
`requirements.txt`, then zips `plugin.json` + the three modules + `SettingsTemplate.yaml`
+ `README.md` + `LICENSE` + `Images` + `lib` into `Flow.Launcher.Plugin.AudioCowboy.zip` with
`plugin.json at the zip root`, and cuts a GitHub Release tagged `v<Version>`.

- **To ship an update**: bump `Version` in `plugin.json`, commit, push to `main`. The
  manifest auto-updater (~every 3h) picks up the higher version. First-ever listing needs
  one manual PR to `Flow.Launcher.PluginsManifest` — see `publish/PUBLISHING.md`.
- `lib/` is git-ignored; the release rebuilds it. The plugin is pure-Python (pycaw only),
  with no external binary bundled or downloaded.
- The repo is **`True347/AudioCowboy`** (not the conventional `Flow.Launcher.Plugin.*`
  name). All GitHub/jsdelivr URLs in `plugin.json`, `publish/*.json`, and `README.md`
  point at it; only the release **zip asset** keeps the conventional name
  `Flow.Launcher.Plugin.AudioCowboy.zip`.
- Commits use the GitHub noreply identity (`211762551+True347@users.noreply.github.com`),
  never a personal email — the repo is public-bound for the Flow plugin store.

## Constraints to respect

- Target the embedded Python that ships with Flow (CI pins **3.11**). The code style is
  intentionally conservative (`%`-formatting, `class X(object)`, broad guards) to run on
  that interpreter — match it.
- Keep `KEYWORD` in `main.py`, `ActionKeyword` in `plugin.json`, and `PLUGIN_NAME` in
  `profiles.py` in sync (they're cross-referenced by comments for this reason).
- Icon paths in result items are repo-relative (`Images/…`); `ShowMsg` toasts need an
  **absolute** path, hence the `_abs()` helper.
