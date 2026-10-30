"""Read-only dump of every audio endpoint's identity — Windows only, changes nothing.

Shows what AudioCowboy matches profiles on (endpoint id, stable id, normalized name)
plus any USB hardware path Windows stores on the endpoint. Run it once per DAC mode
(e.g. UAC1, then UAC2, then UAC1 again) and compare:

  - does each mode get the same endpoint id every time you switch back?
  - does the friendly name (name_key) stay the same across modes?
  - does the stable id stay the same across modes?

    python tests\\inspect_ids.py            # every endpoint, every state
    python tests\\inspect_ids.py fiio       # only names containing "fiio"
"""
import os
import sys
import platform
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audio  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _hardware_paths():
    """endpoint id -> [(property key, value)] for string properties naming a USB VID."""
    out = {}
    try:
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import EDataFlow, DEVICE_STATE
        devs = AudioUtilities.GetAllDevices(data_flow=EDataFlow.eAll.value,
                                            device_state=DEVICE_STATE.MASK_ALL.value)
    except Exception:
        return out
    for dev in devs:
        hits = [(k, v) for k, v in sorted(dev.properties.items())
                if isinstance(v, str) and "VID_" in v.upper()]
        if hits:
            out[dev.id] = hits
    return out


def main():
    filt = " ".join(sys.argv[1:]).strip().lower()
    print("Windows %s (build %s)" % (platform.release(), platform.version()))
    try:
        devices = audio.get_devices(include_inactive=True)
    except audio.AudioError as exc:
        print("Audio backend unavailable: %s" % exc)
        return 1
    hw = _hardware_paths()
    shown = 0
    for d in devices:
        if filt and filt not in d["friendly"].lower():
            continue
        shown += 1
        print("\n[%s] %-11s %s%s" % (d["direction"], d["state"], d["friendly"],
                                     "   <- default" if d["is_default"] else ""))
        print("    id         %s" % d["id"])
        print("    stable_id  %s" % (d.get("stable_id") or "(none)"))
        print("    name_key   %s" % audio.name_key(d["friendly"]))
        for key, value in hw.get(d["id"], []):
            print("    hw         %s   [%s]" % (value, key))
    print("\n%d endpoint(s) shown." % shown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
