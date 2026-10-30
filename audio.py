"""Audio backend for AudioCowboy.

Backend: ``pycaw`` (>= 20251023, which added ``AudioUtilities.SetDefaultDevice``),
vendored into ``lib/`` and shipped in the release — pure Python, no external executable.
There is no public Windows API to *set* the default device, so every tool wraps the
undocumented ``IPolicyConfig`` COM interface; pycaw does it in-process. It is
feature-detected and fully guarded, so a missing/old pycaw degrades to the "backend
missing" path rather than raising.

Device identity is the Windows MMDevice endpoint ID string (pycaw ``device.id``),
e.g. ``{0.0.0.00000000}.{guid}`` — stable across reboots and unique even for two
identical devices. It is NOT stable across driver changes: a DAC switched between
UAC1 and UAC2 is a different endpoint to Windows. So each device also carries its
``PKEY_AudioEndpoint_StableId`` (Windows 11 24H2+, when the endpoint has one), and
``resolve()`` falls back from known ids to stable ids to a unique friendly-name match.
"""
import re
import warnings


class AudioError(Exception):
    """A backend operation failed."""


class AudioBackendUnavailable(AudioError):
    """No usable backend — the pycaw backend failed to load."""


# PKEY_AudioEndpoint_StableId (mmdeviceapi.h; Windows 11 24H2+). Windows tries to keep
# it across OS and audio-driver updates, which change the ordinary endpoint id.
_STABLE_ID_FMTID = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}"
_STABLE_ID_PID = 12

# DEVICE_STATE_* bit -> label (main.py lowercases it for "currently not present").
_STATE_NAMES = {0x1: "Active", 0x2: "Disabled", 0x4: "Not present", 0x8: "Unplugged"}


# --------------------------------------------------------------------------- #
# pycaw backend — the only backend (guarded / feature-detected)
# --------------------------------------------------------------------------- #
def _pycaw_ready():
    try:
        from pycaw.pycaw import AudioUtilities  # noqa: F401
        return hasattr(AudioUtilities, "SetDefaultDevice")
    except Exception:
        return False


def _pycaw_device_id(d):
    """pycaw devices expose `.id`; the default getters may return an IMMDevice (GetId())."""
    if d is None:
        return None
    return getattr(d, "id", None) or (d.GetId() if hasattr(d, "GetId") else None)


def _pycaw_stable_id(dev):
    """The endpoint's PKEY_AudioEndpoint_StableId, or None.

    None covers Windows before 24H2, an endpoint without a stable id (VT_EMPTY), and any
    property-store failure — Microsoft says apps must handle all three.
    """
    try:
        from comtypes import GUID
        from comtypes.automation import VT_LPWSTR
        from pycaw.constants import STGM
        from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY

        key = PROPERTYKEY()
        key.fmtid = GUID(_STABLE_ID_FMTID)
        key.pid = _STABLE_ID_PID
        value = dev._dev.OpenPropertyStore(STGM.STGM_READ.value).GetValue(key)
        try:
            if value.vt == VT_LPWSTR:
                return value.union.pwszVal or None
            return None
        finally:
            value.clear()
    except Exception:
        return None


def _pycaw_devices(include_inactive=False):
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import EDataFlow, DEVICE_STATE

    state = DEVICE_STATE.MASK_ALL.value if include_inactive else DEVICE_STATE.ACTIVE.value
    default_ids = set()
    for getter in (AudioUtilities.GetSpeakers, AudioUtilities.GetMicrophone):
        try:
            default_ids.add(_pycaw_device_id(getter()))
        except Exception:
            pass
    default_ids.discard(None)

    out = []
    for flow, direction in ((EDataFlow.eRender.value, "render"),
                            (EDataFlow.eCapture.value, "capture")):
        try:
            devs = AudioUtilities.GetAllDevices(data_flow=flow, device_state=state)
        except Exception:
            devs = []  # GetAllDevices can COMError on some machines; degrade per-flow
        for dev in devs:
            dev_id = _pycaw_device_id(dev)
            if not dev_id:
                continue
            friendly = getattr(dev, "FriendlyName", None) or dev_id
            dev_state = getattr(dev.state, "value", dev.state)
            out.append({
                "id": dev_id,
                "name": friendly,
                "device_name": "",
                "friendly": friendly,
                "direction": direction,
                "is_default": dev_id in default_ids,
                "is_default_comm": False,  # pycaw can't read the communications default
                "state": _STATE_NAMES.get(dev_state, "Unknown"),
                "stable_id": _pycaw_stable_id(dev),
                "cmd_id": "",
            })
    return out


def _pycaw_set_default(item_id, role="all"):
    from pycaw.pycaw import AudioUtilities
    try:
        from pycaw.constants import ERole
        roles = [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications] if role == "all" \
            else [ERole.eCommunications] if role == "2" else None
        try:
            AudioUtilities.SetDefaultDevice(item_id, roles=roles)
        except TypeError:
            AudioUtilities.SetDefaultDevice(item_id)  # older pycaw signature (all roles)
        return True
    except Exception as exc:
        # A pycaw exception is a definite failure — raise (don't return False) so callers
        # classify it as a hard failure rather than an unverified success.
        raise AudioError("pycaw could not set the default device: %s" % exc)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_devices(include_inactive=False):
    """Return all render+capture endpoint devices (one backend call)."""
    if _pycaw_ready():
        # pycaw warns (to stderr) when an endpoint's properties raise a COMError — seen
        # with some JBL / virtual-audio devices. Flow treats ANY stderr as fatal, so
        # contain those warnings here (main.py also suppresses globally as a backstop).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _pycaw_devices(include_inactive)
    raise AudioBackendUnavailable("pycaw backend unavailable")


def set_default(item_id, role="all"):
    """Set the default device (role 'all' = Console+Multimedia+Communications)."""
    if _pycaw_ready():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _pycaw_set_default(item_id, role)
    raise AudioBackendUnavailable("pycaw backend unavailable")


def split_by_direction(devices):
    render = [d for d in devices if d["direction"] == "render"]
    capture = [d for d in devices if d["direction"] == "capture"]
    return render, capture


def find_default(devices, direction):
    for d in devices:
        if d["direction"] == direction and d["is_default"]:
            return d
    return None


def find_by_id(devices, item_id):
    for d in devices:
        if d["id"] == item_id:
            return d
    return None


def name_key(friendly):
    """A friendly name normalized for fallback matching.

    Windows numbers a re-created endpoint, e.g. "Speakers (2- FiiO K11)" or
    "Speakers (FiiO K11) (2)", so drop a "N- " at the start or right after "(", and a
    trailing "(N)"; then fold case and whitespace.
    """
    s = re.sub(r"(^|\()\s*\d+\s*-\s*", r"\1", friendly or "")
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    return " ".join(s.split()).casefold()


def resolve(devices, direction, ids, stable_ids=(), friendly=""):
    """Find the *active* endpoint a saved profile slot refers to. Returns (device, how).

    ``how`` is "id" (one of ``ids``, tried in order so the primary id wins), "stable"
    (an endpoint whose stable id is in ``stable_ids``), or "name" (the one active
    endpoint whose ``name_key`` matches ``friendly``). With device None it is
    "ambiguous" (several active endpoints share that name — never guess between them)
    or "absent".
    """
    active = [d for d in devices if d["direction"] == direction and d["state"] == "Active"]
    by_id = {d["id"]: d for d in active}
    for i in ids:
        if i in by_id:
            return by_id[i], "id"
    stable = {sid for sid in stable_ids if sid}
    for d in active:
        if d.get("stable_id") in stable:
            return d, "stable"
    key = name_key(friendly)
    if key:
        hits = [d for d in active if name_key(d["friendly"]) == key]
        if len(hits) == 1:
            return hits[0], "name"
        if hits:
            return None, "ambiguous"
    return None, "absent"
