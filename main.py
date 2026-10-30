#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AudioCowboy — Flow Launcher plugin.

Switch the default audio output/input device and manage device profiles, all from
one action keyword (``ac``) with a rofi-style multi-layer menu built on
Flow.Launcher.ChangeQuery drill-down.

    ac              top menu: Output / Input / Profiles / Save
    ac o [filter]   list output (render) devices  -> Enter sets default output
    ac i [filter]   list input  (capture) devices -> Enter sets default input
    ac s [name]     save current devices as a named profile
    ac p [filter]   list saved profiles -> Enter applies; right-click = Apply/Link/Rename/Delete
    ac <name>       anything that isn't a sub-command matches profile names -> Enter applies

Flow Launcher V1 Python model: the request arrives as JSON in ``sys.argv[1]`` and the
response is JSON on stdout. ``query``/``context_menu`` return a list (wrapped in
``{"result": [...]}``); action methods perform a side effect and may emit ONE
``Flow.Launcher.*`` follow-up request (we use ShowMsg for confirmations).
"""
import os
import sys
import json
import time
import warnings

# Flow Launcher raises InvalidDataException if a plugin writes ANYTHING to stderr, so a
# single non-fatal warning kills every query. Third-party backends emit such warnings —
# pycaw warns (to stderr) when a quirky endpoint's properties raise a COMError, as some
# JBL / virtual-audio drivers cause. Silence Python warnings so they never reach stderr;
# user-facing diagnostics use result items / ShowMsg, never stderr.
warnings.filterwarnings("ignore")

# Make sibling modules + any vendored deps importable regardless of the CWD Flow uses.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (PLUGIN_DIR, os.path.join(PLUGIN_DIR, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audio
import profiles

try:  # emit real UTF-8 so non-ASCII device names render correctly
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KEYWORD = "ac"  # must match plugin.json "ActionKeyword"

# Rename drill encodes "Old → New" in the query box; the arrow is the delimiter we split on.
_RENAME_SEP = " → "

# Appended to "Unavailable" toasts: where to fix a slot whose device got a new endpoint.
_LINK_HINT = " — Shift+Enter the profile to link a device"

ICON = "Images/app.png"
ICON_OUT = "Images/output.png"
ICON_IN = "Images/input.png"
ICON_PROFILE = "Images/profile.png"
ICON_SAVE = "Images/save.png"
ICON_DELETE = "Images/delete.png"
ICON_WARN = "Images/warning.png"
ICON_ERROR = "Images/error.png"
ICON_BACK = "Images/back.png"


def _abs(rel):
    """Absolute icon path (ShowMsg wants an absolute path)."""
    return os.path.join(PLUGIN_DIR, rel.replace("/", os.sep))


def result(title, subtitle="", ico=ICON, method=None, params=None,
           context=None, dont_hide=False, score=0, auto=None):
    item = {"Title": title, "SubTitle": subtitle, "IcoPath": ico, "Score": score}
    if auto is not None:
        item["AutoCompleteText"] = auto  # Autocomplete key (Ctrl+Tab) fills the box with this
    if context is not None:
        item["ContextData"] = context
    if method is not None:
        action = {"method": method, "parameters": params or []}
        if dont_hide:
            action["DontHideAfterAction"] = True
        item["JsonRPCAction"] = action
    return item


def drill(title, subtitle, sub_query, ico=ICON, score=0):
    """A result that rewrites the search box to drill into a sub-menu and keeps Flow open.

    ``sub_query`` is the text after the keyword, e.g. ``"o "`` -> box becomes ``"ac o "``.
    Also sets AutoCompleteText so the autocomplete key (Ctrl+Tab) drills in too.
    """
    full = ("%s %s" % (KEYWORD, sub_query)) if sub_query else (KEYWORD + " ")
    return result(title, subtitle, ico=ico, score=score,
                  method="Flow.Launcher.ChangeQuery",
                  params=[full, True], dont_hide=True, auto=full)


def _back_item():
    return drill("← Back", "Return to the main menu", "", ico=ICON_BACK, score=-100)


def _name_rank(name, filt_l):
    """Score a profile name against a lowercase filter; None if it doesn't match.

    Exact > prefix > substring, and every match outranks the Save/Rename/Delete entries
    (score <= 0), so the best match is the highlighted row and Enter applies it.
    """
    n = name.lower()
    if not filt_l:
        return 50
    if filt_l not in n:
        return None
    if n == filt_l:
        return 80
    if n.startswith(filt_l):
        return 70
    return 60


# Profile slot key -> device direction.
_SLOTS = (("output", "render"), ("input", "capture"))


def _resolve(rec, devices, direction):
    """(device, how) for a saved profile slot — see audio.resolve."""
    return audio.resolve(devices, direction, profiles.record_ids(rec),
                         rec.get("stableIds") or (), rec.get("friendlyName") or "")


class AudioCowboy(object):
    def __init__(self):
        try:
            req = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"method": "query", "parameters": [""]}
        except (ValueError, IndexError):
            req = {"method": "query", "parameters": [""]}

        self._emitted = False  # guard: at most one JSON object on stdout
        self.settings = req.get("settings") or {}
        self.include_inactive = bool(self.settings.get("show_disconnected", False))

        method = req.get("method", "query")
        params = req.get("parameters") or []
        handler = getattr(self, method, None)
        if handler is None:
            self._emit([])
            return
        try:
            out = handler(*params)
        except Exception as exc:  # never let the plugin crash silently
            if method in ("query", "context_menu"):
                self._emit([result("AudioCowboy error", repr(exc), ICON_ERROR)])
            else:
                self.show_msg("AudioCowboy error", repr(exc), _abs(ICON_ERROR))
            return
        if method in ("query", "context_menu"):
            self._emit(out or [])

    # ------------------------------------------------------------------ #
    # wire helpers
    # ------------------------------------------------------------------ #
    def _emit(self, results):
        if self._emitted:
            return
        self._emitted = True
        sys.stdout.write(json.dumps({"result": results}, ensure_ascii=False))
        sys.stdout.flush()  # Flow reads one payload off the pipe; don't rely on exit flush

    def _flow(self, method, parameters):
        if self._emitted:
            return
        self._emitted = True
        sys.stdout.write(json.dumps({"method": method, "parameters": parameters}, ensure_ascii=False))
        sys.stdout.flush()

    def show_msg(self, title, sub_title="", ico_path=""):
        self._flow("Flow.Launcher.ShowMsg", [title, sub_title, ico_path or _abs(ICON)])

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #
    def query(self, query=""):
        text = (query or "").strip()
        if not text:
            return self._top_menu()
        parts = text.split(None, 1)
        head = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if head in ("o", "out", "output"):
            return self._device_menu("render", rest)
        if head in ("i", "in", "input"):
            return self._device_menu("capture", rest)
        if head in ("s", "save"):
            return self._save_menu(rest)
        if head in ("p", "profile", "profiles", "l", "load"):
            return self._profile_menu(rest)
        if head in ("d", "del", "delete"):
            return self._delete_menu(rest)
        if head in ("r", "ren", "rename"):
            return self._rename_menu(rest)
        # Anything else is a profile name: "ac gaming" applies it straight from the root.
        # Sub-command words above win, so a profile named e.g. "Out Loud" needs `ac p`.
        matches = self._profile_matches(profiles.list_profiles(), text.lower())
        if matches:
            return [_back_item()] + matches
        return self._top_menu()

    def context_menu(self, data=None):
        if not isinstance(data, list) or not data:
            return []
        kind = data[0]
        if kind == "profile" and len(data) >= 2:
            name = data[1]
            return [
                result("Apply profile “%s”" % name,
                       "Set both devices (all roles)", ICON_PROFILE,
                       method="load_profile", params=[name]),
            ] + self._link_items(name) + [
                drill("Rename profile “%s”…" % name, "Give it a new name",
                      "r " + name + _RENAME_SEP, ICON_PROFILE),
                result("Delete profile “%s”" % name,
                       "Remove this profile permanently", ICON_DELETE,
                       method="delete_profile", params=[name]),
            ]
        if kind == "device" and len(data) >= 4:
            _, direction, item_id, friendly = data[0], data[1], data[2], data[3]
            method = "set_default_output" if direction == "render" else "set_default_input"
            dev_icon = ICON_OUT if direction == "render" else ICON_IN
            return [
                result("Set for all roles", friendly, dev_icon,
                       method=method, params=[item_id, friendly]),
                result("Set as Communication device only", friendly, dev_icon,
                       method="set_default_comm", params=[item_id, friendly, direction]),
            ]
        return []

    # ------------------------------------------------------------------ #
    # menus
    # ------------------------------------------------------------------ #
    def _top_menu(self):
        out_name = in_name = None
        try:
            devices = audio.get_devices(self.include_inactive)
            od = audio.find_default(devices, "render")
            idv = audio.find_default(devices, "capture")
            out_name = od["friendly"] if od else None
            in_name = idv["friendly"] if idv else None
        except audio.AudioBackendUnavailable:
            return [self._backend_missing()]
        except audio.AudioError:
            pass  # show the menu anyway; device queries will report errors when used

        # Fixed order via descending Score (Flow sorts by Score desc): Output > Input > Profile > Save.
        return [
            drill("\U0001F50A  Output device", "Current: %s" % (out_name or "unknown"), "o ", ICON_OUT, score=40),
            drill("\U0001F3A4  Input device", "Current: %s" % (in_name or "unknown"), "i ", ICON_IN, score=30),
            drill("\U0001F4C1  Profiles", "Load a saved device profile", "p ", ICON_PROFILE, score=20),
            drill("\U0001F4BE  Save current as profile…",
                  "Snapshot the current input + output devices", "s ", ICON_SAVE, score=10),
        ]

    def _device_menu(self, direction, filt):
        kind = "output" if direction == "render" else "input"
        try:
            devices = audio.get_devices(self.include_inactive)
        except audio.AudioBackendUnavailable:
            return [self._backend_missing()]
        except audio.AudioError as exc:
            return [result("Could not list %s devices" % kind, str(exc), ICON_ERROR)]

        render, capture = audio.split_by_direction(devices)
        wanted = render if direction == "render" else capture
        filt_l = (filt or "").lower()
        method = "set_default_output" if direction == "render" else "set_default_input"

        items = [_back_item()]
        matched = 0
        for d in wanted:
            if filt_l and filt_l not in d["friendly"].lower():
                continue
            matched += 1
            is_def = d["is_default"]
            inactive = d["state"] and d["state"].lower() != "active"
            title = ("✓ " if is_def else "") + d["friendly"]
            if inactive:
                sub = "%s — currently %s" % (
                    "Default" if is_def else "Set as default %s" % kind, d["state"].lower())
            elif is_def:
                sub = "Already the default %s device" % kind
            else:
                sub = "Set as default %s device (all roles)" % kind
            items.append(result(
                title, sub,
                ico=ICON_OUT if direction == "render" else ICON_IN,
                method=method, params=[d["id"], d["friendly"]],
                context=["device", direction, d["id"], d["friendly"]],
                score=100 if is_def else 0,
            ))
        if matched == 0:
            items.append(result(
                "No matching %s devices" % kind,
                "Try a different filter" if filt_l else "No active %s devices found" % kind,
                ICON_WARN))
        return items

    def _save_menu(self, name):
        name = (name or "").strip()
        out_name = in_name = None
        out_dev = in_dev = None
        try:
            devices = audio.get_devices(self.include_inactive)
            out_dev = audio.find_default(devices, "render")
            in_dev = audio.find_default(devices, "capture")
            out_name = out_dev["friendly"] if out_dev else None
            in_name = in_dev["friendly"] if in_dev else None
        except audio.AudioBackendUnavailable:
            return [self._backend_missing()]
        except audio.AudioError:
            pass

        snapshot = "Out: %s   |   In: %s" % (out_name or "?", in_name or "?")
        if out_name is None and in_name is None:
            snapshot += "   ⚠ could not read current devices"
        if not name:
            return [
                _back_item(),
                result("Type a name to save the current devices…", snapshot, ICON_SAVE),
            ]
        existing = profiles.get_profile(name)
        verb = "Overwrite" if existing else "Save"
        sub = snapshot + ("   (overwrites existing)" if existing else "")
        return [
            _back_item(),
            result("%s profile “%s”" % (verb, name), sub, ICON_SAVE,
                   method="save_profile", params=[name]),
        ]

    def _profile_menu(self, filt):
        profs = profiles.list_profiles()
        filt_l = (filt or "").strip().lower()
        items = [_back_item(),
                 drill("\U0001F4BE  Save current as new profile…",
                       "Snapshot the current input + output devices", "s ", ICON_SAVE)]
        if not profs:
            items.append(result("No saved profiles yet",
                                "Use “ac s <name>” to save the current devices",
                                ICON_PROFILE))
            return items

        items.append(drill("✏️  Rename a profile…", "Give a profile a new name",
                           "r ", ICON_PROFILE, score=-10))
        items.append(drill("\U0001F5D1  Delete a profile…", "Remove a saved profile",
                           "d ", ICON_DELETE, score=-20))

        matches = self._profile_matches(profs, filt_l)
        items.extend(matches)
        if not matches:
            items.append(result("No matching profiles", "Try a different filter", ICON_WARN))
        return items

    def _profile_matches(self, profs, filt_l):
        """Apply-on-Enter items for the profiles whose name matches ``filt_l``, ranked."""
        ranked = []
        for p in profs:
            score = _name_rank(p.get("name", ""), filt_l)
            if score is not None:
                ranked.append((p, score))
        if not ranked:
            return []

        live = []
        try:
            live = audio.get_devices()
        except audio.AudioError:
            pass  # availability badge is optional; surface unexpected (non-audio) bugs

        items = []
        for p, score in ranked:
            pname = p.get("name", "")
            out = p.get("output") or {}
            inp = p.get("input") or {}
            sub = "Out: %s   |   In: %s" % (out.get("friendlyName") or "?",
                                            inp.get("friendlyName") or "?")
            if live:
                # "Unavailable" = nothing active resolves to it (a known-but-unplugged
                # endpoint doesn't count), so a DAC in its other mode isn't flagged when
                # the name fallback will find it.
                missing = [key for key, direction in _SLOTS
                           if (p.get(key) or {}).get("endpointId")
                           and _resolve(p[key], live, direction)[0] is None]
                if missing:
                    sub += "   ⚠ %s unavailable" % " & ".join(missing)
            items.append(result("\U0001F4C1  " + pname, sub, ICON_PROFILE,
                                method="load_profile", params=[pname],
                                context=["profile", pname], score=score))
        return items

    def _link_items(self, name):
        """Context-menu "Use … as this profile's output/input" entries.

        Offered only for a slot that no known id or stable id resolves — typically a DAC
        switched to its other UAC mode, which Windows treats as a new endpoint. Linking
        adds that endpoint to the slot for good. If the name fallback found a device, only
        that one is offered; otherwise every active device of the direction.
        """
        p = profiles.get_profile(name)
        if not p:
            return []
        try:
            live = audio.get_devices()
        except audio.AudioError:
            return []  # optional entries; Apply/Rename/Delete still work
        items = []
        for key, direction in _SLOTS:
            rec = p.get(key) or {}
            if not rec.get("endpointId"):
                continue
            picked, how = _resolve(rec, live, direction)
            if how in ("id", "stable"):
                continue
            kind = "output" if direction == "render" else "input"
            cands = [picked] if picked else [d for d in live if d["direction"] == direction]
            for d in cands:
                items.append(result(
                    "\U0001F517  Use “%s” as this profile's %s" % (d["friendly"], kind),
                    "Remember it alongside “%s” (e.g. the DAC's other USB mode) and apply"
                    % (rec.get("friendlyName") or "?"),
                    ICON_OUT if direction == "render" else ICON_IN,
                    method="link_device", params=[name, key, d["id"]]))
        return items

    def _delete_menu(self, filt):
        profs = profiles.list_profiles()
        filt_l = (filt or "").strip().lower()
        items = [drill("← Back", "Return to profiles", "p ", ICON_BACK, score=-100)]
        if not profs:
            items.append(result("No profiles to delete",
                                "Use “ac s <name>” to save one first", ICON_PROFILE))
            return items
        matched = 0
        for p in profs:
            pname = p.get("name", "")
            if filt_l and filt_l not in pname.lower():
                continue
            matched += 1
            out = p.get("output") or {}
            inp = p.get("input") or {}
            sub = "Out: %s   |   In: %s   ·   Enter to delete permanently" % (
                out.get("friendlyName") or "?", inp.get("friendlyName") or "?")
            # Stay open and re-list after deleting (delete_profile_relist), so several can
            # be removed in a row; the refreshed list is the confirmation.
            items.append(result("\U0001F5D1  " + pname, sub, ICON_DELETE,
                                method="delete_profile_relist", params=[pname],
                                dont_hide=True))
        if matched == 0:
            items.append(result("No matching profiles", "Try a different filter", ICON_WARN))
        return items

    def _rename_menu(self, rest):
        profs = profiles.list_profiles()
        names = [p.get("name", "") for p in profs]
        items = [drill("← Back", "Return to profiles", "p ", ICON_BACK, score=-100)]
        if not profs:
            items.append(result("No profiles to rename",
                                "Use “ac s <name>” to save one first", ICON_PROFILE))
            return items

        # Detect "Old → New": match rest against a KNOWN name + separator so spaces (or
        # even an arrow) in either name can't break the split — longest known name wins.
        # query() strips the trailing space of _RENAME_SEP, so accept both the just-drilled
        # state ("<name> →") and the typing state ("<name> → <new>").
        # Accepted limit: if a profile literally named "X → Y" exists, renaming "X" *to* a
        # name starting "Y …" produces a box identical to drilling into "X → Y", so it
        # targets "X → Y". Non-destructive — the confirm shows the real Old → New before
        # Enter, and the collision guard blocks any clobber — so it's documented, not fixed.
        sep_core = _RENAME_SEP.rstrip()  # " →"
        old = new = None
        for cand in sorted(names, key=len, reverse=True):
            if rest.startswith(cand + _RENAME_SEP):
                old, new = cand, rest[len(cand + _RENAME_SEP):]
                break
            if rest == cand + sep_core:
                old, new = cand, ""
                break

        if old is not None:
            new = new.strip()
            if not new:
                items.append(result("Rename “%s” → …" % old, "Type the new name", ICON_PROFILE))
            elif new == old:  # exact match only; a case change is a legitimate rename
                items.append(result("Rename “%s”" % old,
                                    "That's the same name — type a different one", ICON_WARN))
            elif any(n.strip().lower() == new.lower() and n.lower() != old.lower()
                     for n in names):
                items.append(result("⚠ “%s” already exists" % new,
                                    "Pick a name not used by another profile", ICON_WARN))
            else:
                items.append(result("Rename “%s” → “%s”" % (old, new), "Enter to rename",
                                    ICON_PROFILE, method="rename_profile",
                                    params=[old, new], dont_hide=True))
            return items

        # Stage 1: pick which profile to rename; `rest` acts as a name filter.
        filt_l = rest.strip().lower()
        matched = 0
        for pname in names:
            if filt_l and filt_l not in pname.lower():
                continue
            matched += 1
            items.append(drill("✏️  " + pname, "Rename this profile",
                               "r " + pname + _RENAME_SEP, ICON_PROFILE))
        if matched == 0:
            items.append(result("No matching profiles", "Try a different filter", ICON_WARN))
        return items

    def _backend_missing(self):
        return result(
            "⚠ Audio backend unavailable",
            "Could not load the bundled pycaw backend. Try reinstalling the plugin. "
            "Press Enter to open the plugin folder.",
            ICON_WARN,
            method="Flow.Launcher.OpenDirectory", params=[PLUGIN_DIR])

    def _backend_missing_toast(self):
        # Reached only when *neither* backend loaded. The release ships pycaw, so the
        # real cause is a broken/absent lib/ (pycaw failed to load).
        self.show_msg("Audio backend unavailable",
                      "Could not load the bundled pycaw backend. Try reinstalling the plugin.",
                      _abs(ICON_ERROR))

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def set_default_output(self, item_id, friendly=""):
        self._apply_device(item_id, "render", friendly, "Output", role="all")

    def set_default_input(self, item_id, friendly=""):
        self._apply_device(item_id, "capture", friendly, "Input", role="all")

    def set_default_comm(self, item_id, friendly="", direction="render"):
        label = "Output" if direction == "render" else "Input"
        self._apply_device(item_id, direction, friendly,
                           "%s (communication)" % label, role="2", comm=True)

    def _apply_device(self, item_id, direction, friendly, label, role="all", comm=False):
        try:
            ok = audio.set_default(item_id, role)
        except audio.AudioBackendUnavailable:
            self._backend_missing_toast()
            return
        except Exception as exc:
            self.show_msg("Failed to set %s device" % label.lower(), str(exc), _abs(ICON_ERROR))
            return
        # Confirm via toast, then the window closes (like applying a profile). A backend
        # reporting success is not proof: re-read the defaults and let that decide; the
        # backend's own return value is only the fallback when the read-back fails.
        verified = self._is_now_default(item_id, direction, comm=comm)
        ok_icon = ICON_OUT if direction == "render" else ICON_IN
        if verified is True:
            self.show_msg("%s device set" % label, friendly or item_id, _abs(ok_icon))
        elif verified is False:
            self.show_msg("%s device may not have changed" % label,
                          friendly or item_id, _abs(ICON_WARN))
        elif ok:  # couldn't read back — don't claim a definitive success
            self.show_msg("%s device set (unverified)" % label,
                          friendly or item_id, _abs(ICON_WARN))
        else:
            self.show_msg("Failed to set %s device" % label.lower(),
                          friendly or item_id, _abs(ICON_ERROR))

    def _is_now_default(self, item_id, direction, comm=False, retries=3):
        """True/False if the device is/ isn't the default now; None if unverifiable.

        Windows can apply the change a touch after the backend call returns, so re-read
        a few times before concluding it didn't take. The success case returns on the
        first read.
        """
        if comm:
            return None  # pycaw can't report the communications default; say so, don't guess
        for attempt in range(retries):
            try:
                devices = audio.get_devices(include_inactive=True)
            except Exception:
                return None
            dev = audio.find_by_id(devices, item_id)
            if dev is None:
                return False
            if dev["is_default_comm"] if comm else dev["is_default"]:
                return True
            if attempt < retries - 1:
                time.sleep(0.15)
        return False

    def save_profile(self, name):
        try:
            # Always read the full set so an inactive current default isn't dropped.
            devices = audio.get_devices(include_inactive=True)
        except audio.AudioBackendUnavailable:
            self._backend_missing_toast()
            return
        except Exception as exc:
            self.show_msg("Failed to read current devices", str(exc), _abs(ICON_ERROR))
            return
        out_dev = audio.find_default(devices, "render")
        in_dev = audio.find_default(devices, "capture")
        try:
            profiles.save_profile(name, output=out_dev, input=in_dev)
        except Exception as exc:
            self.show_msg("Failed to save profile", str(exc), _abs(ICON_ERROR))
            return
        self.show_msg(
            "Profile saved: %s" % name,
            "Out: %s   |   In: %s" % (out_dev["friendly"] if out_dev else "?",
                                      in_dev["friendly"] if in_dev else "?"),
            _abs(ICON_SAVE))

    def load_profile(self, name):
        p = profiles.get_profile(name)
        if not p:
            self.show_msg("Profile not found", name, _abs(ICON_ERROR))
            return
        slots = [(key, direction, p[key]) for key, direction in _SLOTS
                 if (p.get(key) or {}).get("endpointId")]
        if not slots:
            self.show_msg("Profile “%s” is empty" % name, "Nothing to apply", _abs(ICON_WARN))
            return

        # Resolve each slot to a live endpoint first: a known id, then a known stable id,
        # then a unique name match — the same DAC in its other UAC mode is a different
        # endpoint to Windows. If the list can't be read, try the saved id and let
        # verification judge.
        try:
            live = audio.get_devices()
        except audio.AudioBackendUnavailable:
            self._backend_missing_toast()
            return
        except Exception:
            live = None
        targets = []  # (slot key, endpointId, label, how, resolved device or None)
        absent = []
        for key, direction, rec in slots:
            saved = rec.get("friendlyName") or rec["endpointId"]
            if live is None:
                targets.append((key, rec["endpointId"], saved, "id", None))
                continue
            dev, how = _resolve(rec, live, direction)
            if dev is None:
                absent.append(saved + (" (several devices match)" if how == "ambiguous" else ""))
            else:
                label = dev["friendly"] + (" (by name)" if how == "name" else "")
                targets.append((key, dev["id"], label, how, dev))

        # Set each device. Only a raised exception is a hard failure; the backend's own
        # success value is ignored — verification (below) is the source of truth.
        set_error = set()
        for _key, endpoint_id, _label, _how, _dev in targets:
            try:
                audio.set_default(endpoint_id, "all")
            except audio.AudioBackendUnavailable:
                self._backend_missing_toast()
                return
            except Exception:
                set_error.add(endpoint_id)

        # Verify by re-reading the defaults; retry while any *present* target hasn't
        # become default yet (Windows can apply it just after the call returns). Drive the
        # retry off the verified state, NOT the backend's return value — a set that
        # reported failure but actually lands a moment later must still be caught.
        devices = None
        for attempt in range(3 if targets else 0):
            try:
                devices = audio.get_devices(include_inactive=True)
            except Exception:
                devices = None
                break
            pending = False
            for _key, eid, _lbl, _how, _dev in targets:
                dev = audio.find_by_id(devices, eid)
                if dev is not None and not dev["is_default"]:
                    pending = True
                    break
            if not pending or attempt == 2:
                break
            time.sleep(0.15)

        # Classify each target honestly:
        #   applied    - confirmed default now
        #   unverified - couldn't re-read; backend reported success
        #   failed     - device present but did not become default (or the set failed)
        #   absent     - no live endpoint for the slot (resolved above, or vanished since)
        applied, unverified, failed = [], [], []
        for key, endpoint_id, label, how, resolved in targets:
            if devices is None:
                # Can't verify: only a raised exception is a hard failure; an unreliable
                # False return is genuinely unknown, so report it as unverified.
                (failed if endpoint_id in set_error else unverified).append(label)
                continue
            dev = audio.find_by_id(devices, endpoint_id)
            if dev is None:
                absent.append(label)
            elif dev["is_default"]:
                applied.append(label)
                if resolved is not None:
                    self._learn(name, key, how, resolved, p[key], devices)
            else:
                failed.append(label)

        problems = failed + absent
        if (applied or unverified) and not problems:
            shown = applied + ["%s (unverified)" % u for u in unverified]
            self.show_msg("Applied profile: %s" % name, "   ".join(shown),
                          _abs(ICON_WARN if unverified else ICON_PROFILE))
        elif applied or unverified:
            shown = applied + ["%s (unverified)" % u for u in unverified]
            detail = "Set: %s" % ", ".join(shown)
            if failed:
                detail += "   Failed to set: %s" % ", ".join(failed)
            if absent:
                detail += "   Unavailable: %s%s" % (", ".join(absent), _LINK_HINT)
            self.show_msg("Applied “%s” — partial" % name, detail, _abs(ICON_WARN))
        else:
            parts = []
            if failed:
                parts.append("Failed to set: %s" % ", ".join(failed))
            if absent:
                parts.append("Unavailable: %s%s" % (", ".join(absent), _LINK_HINT))
            self.show_msg("Could not apply “%s”" % name, "   ".join(parts), _abs(ICON_ERROR))

    def _learn(self, name, key, how, dev, rec, devices):
        """After a verified apply, store what makes the next lookup direct (best-effort).

        An id / stable-id hit stores whichever of the two is new. A name hit is stored
        only when Windows has deleted every endpoint the slot knew — none is listed in
        ``devices`` (all states), not even as unplugged. Windows re-creates an endpoint
        under a new id now and then (seen on a FiiO K11 with no USB change at all), and
        re-pointing the slot lets its stable id be tracked from then on. While a known
        endpoint still exists, a name hit may be the other of two identical devices, so
        only an explicit link (the profile's context menu) may merge it in.
        """
        try:
            if how != "name":
                profiles.remember_device(name, key, dev)
                return
            known = profiles.record_ids(rec)
            if not any(audio.find_by_id(devices, i) for i in known):
                profiles.remember_device(name, key, dev, drop=known)
        except Exception:
            pass  # the switch itself succeeded

    def link_device(self, name, key, item_id):
        """Context-menu action: remember ``item_id`` for profile ``name``'s ``key`` slot
        (e.g. the endpoint the DAC has in its other UAC mode), then apply the profile."""
        try:
            dev = audio.find_by_id(audio.get_devices(include_inactive=True), item_id)
        except audio.AudioBackendUnavailable:
            self._backend_missing_toast()
            return
        except Exception as exc:
            self.show_msg("Failed to read current devices", str(exc), _abs(ICON_ERROR))
            return
        if dev is None:
            self.show_msg("Device not found", item_id, _abs(ICON_WARN))
            return
        try:
            profiles.remember_device(name, key, dev)
        except Exception as exc:
            self.show_msg("Failed to update profile", str(exc), _abs(ICON_ERROR))
            return
        self.load_profile(name)

    def delete_profile(self, name):
        try:
            removed = profiles.delete_profile(name)
        except Exception as exc:
            self.show_msg("Failed to delete profile", str(exc), _abs(ICON_ERROR))
            return
        if removed:
            self.show_msg("Profile deleted: %s" % name, "", _abs(ICON_DELETE))
        else:
            self.show_msg("Profile not found", name, _abs(ICON_WARN))

    def delete_profile_relist(self, name):
        # Delete, then re-open the delete list (window stays open via DontHideAfterAction)
        # so the user can remove several in a row; the refreshed list is the confirmation.
        # A no-op delete (already gone) still just re-lists — same visible outcome, so the
        # bool is intentionally ignored. Only a raised exception is surfaced as a toast.
        try:
            profiles.delete_profile(name)
        except Exception as exc:
            self.show_msg("Failed to delete profile", str(exc), _abs(ICON_ERROR))
            return
        self._flow("Flow.Launcher.ChangeQuery", ["%s d " % KEYWORD, True])

    def rename_profile(self, old, new):
        try:
            status = profiles.rename_profile(old, new)
        except Exception as exc:
            self.show_msg("Failed to rename profile", str(exc), _abs(ICON_ERROR))
            return
        if status == "renamed":
            # Back to the profiles list, now showing the new name (that's the confirmation).
            self._flow("Flow.Launcher.ChangeQuery", ["%s p " % KEYWORD, True])
        elif status == "exists":
            self.show_msg("Name already in use", new, _abs(ICON_WARN))
        else:
            self.show_msg("Profile not found", old, _abs(ICON_WARN))


if __name__ == "__main__":
    AudioCowboy()
