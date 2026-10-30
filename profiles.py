"""Profile persistence for AudioCowboy.

A profile is a named snapshot of {default output device, default input device}.
Each slot is stored as::

    {"endpointId": ..., "friendlyName": ..., "altIds": [...], "stableIds": [...]}

``endpointId`` is the MMDevice id the profile was saved with. ``altIds`` are more
endpoint ids for the same physical device (a DAC gets a different one in its other
UAC mode) and ``stableIds`` its PKEY_AudioEndpoint_StableId values; both are optional
and omitted when empty. Applying matches ids first, then stable ids, and only then a
unique friendly-name match (see ``audio.resolve``).

Profiles live OUTSIDE the plugin install folder (which Flow wipes on every update),
under the roaming settings root:

    %APPDATA%\\FlowLauncher\\Settings\\Plugins\\AudioCowboy\\profiles.json

Writes are atomic (temp file + os.replace) so a crash mid-write can't corrupt the file.
"""
import os
import json
import tempfile
from datetime import datetime, timezone

PLUGIN_NAME = "AudioCowboy"  # must match plugin.json "Name"
SCHEMA_VERSION = 1

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _settings_dir():
    """Update-safe directory for this plugin's data."""
    appdata = os.getenv("APPDATA")
    if appdata and os.path.isdir(os.path.join(appdata, "FlowLauncher")):
        base = os.path.join(appdata, "FlowLauncher", "Settings", "Plugins", PLUGIN_NAME)
    else:
        # Portable install: plugin dir is .../<root>/Plugins/<Name>-<ver>; settings
        # live at .../<root>/Settings/Plugins/<Name>.
        root = os.path.dirname(os.path.dirname(_PLUGIN_DIR))
        base = os.path.join(root, "Settings", "Plugins", PLUGIN_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def profiles_path():
    return os.path.join(_settings_dir(), "profiles.json")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_doc():
    return {"schemaVersion": SCHEMA_VERSION, "profiles": []}


def load():
    """Return the profiles document; never raises (returns an empty doc on error)."""
    path = profiles_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty_doc()
    except ValueError:
        # Corrupt JSON: preserve it as a .bak so a subsequent save can't silently
        # destroy the user's profiles, then start fresh.
        _backup_corrupt(path)
        return _empty_doc()
    except OSError:
        # Unreadable (locked / permissions): degrade without clobbering the file.
        return _empty_doc()
    if not isinstance(data, dict):
        _backup_corrupt(path)
        return _empty_doc()
    if not isinstance(data.get("profiles"), list):
        # Schema-invalid: back it up too, so the next save can't silently clobber it.
        _backup_corrupt(path)
        return _empty_doc()
    data.setdefault("schemaVersion", SCHEMA_VERSION)
    return data


def _backup_corrupt(path):
    try:
        if not os.path.exists(path):
            return
        bak = path + ".bak"
        if os.path.exists(bak):
            # Keep the first .bak; timestamp later ones so we don't lose forensic copies.
            bak = "%s.%s.bak" % (path, datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        os.replace(path, bak)
    except OSError:
        pass


def _save(data):
    path = profiles_path()
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".profiles-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def list_profiles():
    return load().get("profiles", [])


def _find(profs, name):
    name_l = (name or "").strip().lower()
    for p in profs:
        if (p.get("name") or "").strip().lower() == name_l:
            return p
    return None


def get_profile(name):
    return _find(load().get("profiles", []), name)


def record_ids(rec):
    """Every endpoint id known for a profile slot: the saved one first, then ``altIds``."""
    out = []
    if rec:
        for dev_id in [rec.get("endpointId")] + list(rec.get("altIds") or []):
            if dev_id and dev_id not in out:
                out.append(dev_id)
    return out


def save_profile(name, output=None, input=None):
    """Create or overwrite a profile. ``output``/``input`` are {id, friendlyName} dicts."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Profile name cannot be empty")
    data = load()
    entry = {
        "name": name,
        "created": _now_iso(),
        "output": _device_record(output),
        "input": _device_record(input),
    }
    profs = data["profiles"]
    for i, p in enumerate(profs):
        if (p.get("name") or "").strip().lower() == name.lower():
            entry["created"] = p.get("created", entry["created"])
            entry["updated"] = _now_iso()
            for key in ("output", "input"):
                entry[key] = _carry_ids(p.get(key), entry[key])
            profs[i] = entry
            break
    else:
        profs.append(entry)
    _save(data)
    return entry


def remember_device(name, key, device, drop=()):
    """Record ``device`` as another endpoint of profile ``name``'s ``key`` slot.

    ``key`` is "output" or "input"; ``device`` is an audio device dict. Its id joins
    ``altIds`` (the saved id stays primary) and its stable id joins ``stableIds``; an
    empty slot simply takes the device. ``drop`` lists endpoint ids to forget (ones
    Windows has deleted); if the primary is among them, the device's id becomes primary.
    Returns True if the profile changed and was saved.
    """
    if key not in ("output", "input"):
        raise ValueError("Unknown profile slot: %r" % key)
    new = _device_record(device)
    data = load()
    target = _find(data["profiles"], name)
    if target is None or new is None:
        return False
    old = target.get(key)
    if not old or not old.get("endpointId"):
        target[key] = new
    else:
        known = record_ids(old)
        ids = [i for i in known if i not in drop]
        stable = list(old.get("stableIds") or [])
        more_ids = [i for i in record_ids(new) if i not in ids]
        more_stable = [sid for sid in new.get("stableIds", []) if sid not in stable]
        if not more_ids and not more_stable and ids == known:
            return False
        ids = ids + more_ids if ids[:1] == known[:1] else more_ids + ids
        target[key] = _with_ids(old, ids, stable + more_stable)
    target["updated"] = _now_iso()
    _save(data)
    return True


def delete_profile(name):
    name_l = (name or "").strip().lower()
    data = load()
    before = len(data["profiles"])
    data["profiles"] = [p for p in data["profiles"]
                        if (p.get("name") or "").strip().lower() != name_l]
    removed = len(data["profiles"]) < before
    if removed:
        _save(data)
    return removed


def rename_profile(old, new):
    """Rename profile ``old`` to ``new``. Returns 'renamed', 'not_found', or 'exists'.

    Rejects (returns 'exists') if ``new`` collides with a *different* profile — unlike
    save_profile's overwrite, a rename must never silently destroy another profile.
    A case-only rename (e.g. "gaming" -> "Gaming") is allowed. Preserves ``created``.
    """
    new = (new or "").strip()
    if not new:
        raise ValueError("New profile name cannot be empty")
    old_l = (old or "").strip().lower()
    new_l = new.lower()
    data = load()
    target = None
    for p in data["profiles"]:
        if (p.get("name") or "").strip().lower() == old_l:
            target = p
            break
    if target is None:
        return "not_found"
    for p in data["profiles"]:
        if p is not target and (p.get("name") or "").strip().lower() == new_l:
            return "exists"
    target["name"] = new
    target["updated"] = _now_iso()
    _save(data)
    return "renamed"


def _device_record(device):
    if not device:
        return None
    dev_id = device.get("id") or device.get("endpointId")
    if not dev_id:
        return None
    rec = {
        "endpointId": dev_id,
        "friendlyName": device.get("friendly") or device.get("friendlyName") or "",
    }
    if device.get("stable_id"):
        rec["stableIds"] = [device["stable_id"]]
    return rec


def _with_ids(rec, ids, stable_ids):
    """Copy of ``rec`` with ``ids[0]`` as endpointId, the rest as altIds, and stableIds."""
    out = dict(rec)
    out["endpointId"] = ids[0]
    out.pop("altIds", None)
    out.pop("stableIds", None)
    if ids[1:]:
        out["altIds"] = ids[1:]
    if stable_ids:
        out["stableIds"] = stable_ids
    return out


def _carry_ids(old, new):
    """On overwrite, keep the ids learned for a slot if the new device is the same one.

    "Same" means the new endpoint id or stable id is already known for the slot — never
    a name match. The new id becomes primary; the old ones stay as altIds.
    """
    if not old or not new:
        return new
    old_ids = record_ids(old)
    old_stable = list(old.get("stableIds") or [])
    new_stable = new.get("stableIds", [])
    if new["endpointId"] not in old_ids and not set(new_stable) & set(old_stable):
        return new
    ids = [new["endpointId"]] + [i for i in old_ids if i != new["endpointId"]]
    stable = new_stable + [sid for sid in old_stable if sid not in new_stable]
    return _with_ids(new, ids, stable)
