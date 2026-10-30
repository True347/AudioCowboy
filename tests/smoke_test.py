"""Offline smoke tests for AudioCowboy — no Flow Launcher or real audio required.

Exercises: profiles CRUD round-trip, device helpers, and the JSON-RPC wire layer
(main.py invoked as a subprocess, the way Flow invokes it).

    python tests/smoke_test.py
"""
import os
import sys
import json
import shutil
import tempfile
import importlib
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_profiles():
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)

    assert profiles.list_profiles() == []
    profiles.save_profile(
        "Gaming",
        output={"id": "{0.0.0.x}.{out}", "friendly": "Speakers (Realtek)"},
        input={"id": "{0.0.1.x}.{in}", "friendly": "Mic (HyperX)"},
    )
    p = profiles.get_profile("gaming")  # case-insensitive lookup
    assert p and p["output"]["endpointId"] == "{0.0.0.x}.{out}"
    assert p["input"]["friendlyName"] == "Mic (HyperX)"
    assert p["output"]["friendlyName"] == "Speakers (Realtek)"

    # overwrite keeps created, adds updated, and can clear a side
    profiles.save_profile("Gaming", output={"id": "X", "friendly": "New"}, input=None)
    p = profiles.get_profile("Gaming")
    assert p["output"]["endpointId"] == "X" and p["input"] is None
    assert "created" in p and "updated" in p
    assert len(profiles.list_profiles()) == 1

    assert profiles.delete_profile("GAMING") is True
    assert profiles.list_profiles() == []
    assert profiles.delete_profile("nope") is False

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  profiles CRUD")


def test_rename():
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)

    profiles.save_profile("Gaming", output={"id": "O", "friendly": "Spk"}, input=None)
    profiles.save_profile("Music", output={"id": "O2", "friendly": "Spk2"}, input=None)
    created = profiles.get_profile("Gaming")["created"]

    # rename to a fresh name: preserves created, adds updated, keeps device data
    assert profiles.rename_profile("Gaming", "Streaming") == "renamed"
    assert profiles.get_profile("Gaming") is None
    r = profiles.get_profile("Streaming")
    assert r and r["created"] == created and "updated" in r
    assert r["output"]["endpointId"] == "O"

    # collision with a DIFFERENT profile is rejected (no data loss on either side)
    assert profiles.rename_profile("Streaming", "Music") == "exists"
    assert profiles.get_profile("Streaming") is not None
    assert profiles.get_profile("Music")["output"]["endpointId"] == "O2"

    # case-only rename is allowed (not a self-collision)
    assert profiles.rename_profile("music", "MUSIC") == "renamed"
    assert profiles.get_profile("MUSIC")["name"] == "MUSIC"

    assert profiles.rename_profile("nope", "whatever") == "not_found"

    try:
        profiles.rename_profile("MUSIC", "   ")
        assert False, "empty new name should raise"
    except ValueError:
        pass

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  profile rename (collision-safe)")


def test_corruption_backup():
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)

    # seed a valid profile, then corrupt the file on disk
    profiles.save_profile("Keep", output={"id": "X", "friendly": "Spk"}, input=None)
    path = profiles.profiles_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ]")

    # load() must not raise, must back up the corrupt file, and must start fresh
    doc = profiles.load()
    assert doc["profiles"] == []
    assert os.path.exists(path + ".bak"), "corrupt file should be preserved as .bak"

    # a subsequent save works and does not resurrect corruption
    profiles.save_profile("Fresh", output={"id": "Y", "friendly": "New"}, input=None)
    assert [p["name"] for p in profiles.list_profiles()] == ["Fresh"]

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  corrupt-file backup + recovery")


def test_audio_helpers():
    import audio
    importlib.reload(audio)
    spk = {"id": "{0.0.0.00000000}.{guid}", "direction": "render", "is_default": True,
           "friendly": "Speakers (Realtek)", "state": "Active", "name": "Speakers",
           "device_name": "Realtek", "is_default_comm": False, "cmd_id": ""}
    mic = {"id": "a", "direction": "capture", "is_default": True, "friendly": "Mic",
           "state": "Active", "name": "Mic", "device_name": "", "is_default_comm": False, "cmd_id": ""}
    devs = [spk, mic]
    render, capture = audio.split_by_direction(devs)
    assert len(render) == 1 and len(capture) == 1
    assert audio.find_default(devs, "render")["friendly"] == "Speakers (Realtek)"
    assert audio.find_default(devs, "capture")["friendly"] == "Mic"
    assert audio.find_by_id(devs, "a")["direction"] == "capture"
    assert audio.find_by_id(devs, "missing") is None
    print("OK  audio helpers")


def test_pycaw_warning_suppressed():
    """A pycaw warning (as a quirky JBL/virtual endpoint triggers) must never escape
    get_devices() — Flow treats any plugin stderr as fatal (InvalidDataException)."""
    import warnings
    import audio
    importlib.reload(audio)

    def _warn_then_return(include_inactive=False):
        warnings.warn("COMError attempting to get property 67", UserWarning)
        return []

    audio._pycaw_ready = lambda: True
    audio._pycaw_devices = _warn_then_return

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        devices = audio.get_devices()
    assert devices == []
    assert not caught, "pycaw warning leaked past get_devices() (fatal to Flow)"
    print("OK  pycaw warning suppressed (stderr stays clean)")


def test_pycaw_devices_mapping():
    """_pycaw_devices against a fake pycaw: real endpoint states, the requested state mask
    on BOTH flows, and a stable id that degrades to None when it can't be read."""
    import types
    from enum import Enum
    import audio
    importlib.reload(audio)

    class EDataFlow(Enum):
        eRender = 0
        eCapture = 1

    class DEVICE_STATE(Enum):
        ACTIVE = 0x1
        MASK_ALL = 0xF

    class State(Enum):
        Active = 0x1
        NotPresent = 0x4

    class Dev(object):
        def __init__(self, i, name, state):
            self.id, self.FriendlyName, self.state = i, name, state

    calls = []
    by_flow = {0: [Dev("R1", "Speakers (FiiO K11)", State.NotPresent), Dev("R2", "Spk", State.Active)],
               1: [Dev("C1", "Mic", State.Active)]}

    class AudioUtilities(object):
        SetDefaultDevice = None
        GetSpeakers = staticmethod(lambda: Dev("R2", "Spk", State.Active))
        GetMicrophone = staticmethod(lambda: Dev("C1", "Mic", State.Active))

        @staticmethod
        def GetAllDevices(data_flow, device_state):
            calls.append((data_flow, device_state))
            return by_flow[data_flow]

    fake = {"pycaw": types.ModuleType("pycaw"), "pycaw.pycaw": types.ModuleType("pycaw.pycaw"),
            "pycaw.constants": types.ModuleType("pycaw.constants")}
    fake["pycaw.pycaw"].AudioUtilities = AudioUtilities
    fake["pycaw.constants"].EDataFlow = EDataFlow
    fake["pycaw.constants"].DEVICE_STATE = DEVICE_STATE
    saved = {k: sys.modules.get(k) for k in fake}
    sys.modules.update(fake)
    try:
        devs = audio._pycaw_devices(include_inactive=True)
        assert calls == [(0, 0xF), (1, 0xF)], calls  # the mask must not leak per device
        assert [(d["id"], d["state"], d["is_default"]) for d in devs] == [
            ("R1", "Not present", False), ("R2", "Active", True), ("C1", "Active", True)], devs
        assert all(d["stable_id"] is None for d in devs)  # no comtypes here -> None, no raise
        calls[:] = []
        audio._pycaw_devices(include_inactive=False)
        assert calls == [(0, 0x1), (1, 0x1)], calls
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    print("OK  pycaw device mapping (real states, per-flow state mask)")


def run_main(req):
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), json.dumps(req)],
        capture_output=True,
    )
    return proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8", "replace")


def test_wire():
    # Own APPDATA so the subprocess doesn't inherit a deleted temp dir from a prior test.
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))

    out, err = run_main({"method": "query", "parameters": [""], "settings": {}})
    assert err.strip() == "", "stderr not empty:\n" + err
    obj = json.loads(out)
    assert "result" in obj and isinstance(obj["result"], list) and obj["result"], out

    # sub-menu routing returns valid JSON regardless of backend availability
    for q in ("o", "i", "s gaming", "p"):
        out_q, err_q = run_main({"method": "query", "parameters": [q], "settings": {}})
        assert err_q.strip() == "", "stderr for %r:\n%s" % (q, err_q)
        json.loads(out_q)

    # profile context menu -> Apply + Rename (drill) + Delete
    out_c, err_c = run_main({"method": "context_menu", "parameters": [["profile", "Gaming"]], "settings": {}})
    assert err_c.strip() == "", err_c
    objc = json.loads(out_c)
    assert len(objc["result"]) == 3
    assert objc["result"][0]["JsonRPCAction"]["method"] == "load_profile"
    assert objc["result"][1]["JsonRPCAction"]["method"] == "Flow.Launcher.ChangeQuery"
    assert objc["result"][2]["JsonRPCAction"]["method"] == "delete_profile"

    # device context menu -> all roles + comms only
    out_d, _ = run_main({"method": "context_menu",
                         "parameters": [["device", "render", "{id}", "Speakers"]], "settings": {}})
    objd = json.loads(out_d)
    assert objd["result"][0]["JsonRPCAction"]["method"] == "set_default_output"
    assert objd["result"][1]["JsonRPCAction"]["method"] == "set_default_comm"

    # drill-down result shape: ChangeQuery + keep-open flag
    top = json.loads(run_main({"method": "query", "parameters": [""], "settings": {}})[0])
    drill_items = [r for r in top["result"]
                   if r.get("JsonRPCAction", {}).get("method") == "Flow.Launcher.ChangeQuery"]
    # top menu has drill items only when a backend is present; if pycaw failed to load it's
    # a single "⚠ Audio backend unavailable" item — accept either, but validate any drills.
    for r in drill_items:
        act = r["JsonRPCAction"]
        assert act["parameters"][0].startswith("ac ")
        assert act["parameters"][1] is True
        assert act.get("DontHideAfterAction") is True

    # unknown method -> empty result, no crash
    out_u, err_u = run_main({"method": "bogus", "parameters": [], "settings": {}})
    assert err_u.strip() == ""
    assert json.loads(out_u) == {"result": []}
    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  JSON-RPC wire layer (%d drill items in top menu)" % len(drill_items))


def test_delete_rename_wire():
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)
    profiles.save_profile("Gaming", output={"id": "O", "friendly": "Spk"}, input=None)
    sep = " → "

    # delete list: Enter removes via delete_profile_relist and keeps the window open
    out, err = run_main({"method": "query", "parameters": ["d"], "settings": {}})
    assert err.strip() == "", err
    res = json.loads(out)["result"]
    ditem = [r for r in res if r.get("JsonRPCAction", {}).get("method") == "delete_profile_relist"]
    assert ditem and ditem[0]["JsonRPCAction"]["parameters"] == ["Gaming"]
    assert ditem[0]["JsonRPCAction"].get("DontHideAfterAction") is True

    # rename stage 1: picking a profile drills the box to "ac r Gaming → "
    out, err = run_main({"method": "query", "parameters": ["r"], "settings": {}})
    assert err.strip() == "", err
    res = json.loads(out)["result"]
    target = "%s r Gaming%s" % ("ac", sep)
    assert any(r.get("JsonRPCAction", {}).get("parameters", [None])[0] == target
               for r in res), [r.get("JsonRPCAction") for r in res]

    # rename stage 2: typing a new name yields a rename_profile confirm (Old, New)
    out, _ = run_main({"method": "query", "parameters": ["r Gaming%sStreaming" % sep], "settings": {}})
    conf = [r for r in json.loads(out)["result"]
            if r.get("JsonRPCAction", {}).get("method") == "rename_profile"]
    assert conf and conf[0]["JsonRPCAction"]["parameters"] == ["Gaming", "Streaming"]

    # rename onto an existing DIFFERENT profile is a warning, never an actionable rename
    profiles.save_profile("Music", output={"id": "O2", "friendly": "S2"}, input=None)
    out, _ = run_main({"method": "query", "parameters": ["r Gaming%sMusic" % sep], "settings": {}})
    assert not [r for r in json.loads(out)["result"]
                if r.get("JsonRPCAction", {}).get("method") == "rename_profile"]

    def _rename_action(rest):
        return [r for r in json.loads(
            run_main({"method": "query", "parameters": ["r " + rest], "settings": {}})[0])["result"]
            if r.get("JsonRPCAction", {}).get("method") == "rename_profile"]

    # just-drilled state "Gaming → " (query() strips the trailing space): must show the
    # "type the new name" prompt, NOT "No matching profiles", and offer no rename yet.
    out, _ = run_main({"method": "query", "parameters": ["r Gaming%s" % sep], "settings": {}})
    res = json.loads(out)["result"]
    assert any(r.get("SubTitle") == "Type the new name" for r in res), res
    assert not [r for r in res if r.get("JsonRPCAction", {}).get("method") == "rename_profile"]

    # a case-only rename is a legitimate rename (backend allows it), so the UI offers it
    case_act = _rename_action("Gaming" + sep + "GAMING")
    assert case_act and case_act[0]["JsonRPCAction"]["parameters"] == ["Gaming", "GAMING"], case_act

    # arrow-in-name: with "Foo" and "Foo → Bar", renaming the latter must target the LONGER
    # name (not mis-split to old="Foo"). Longest-known-name-first guarantees this.
    profiles.save_profile("Foo", output={"id": "F", "friendly": "F"}, input=None)
    profiles.save_profile("Foo → Bar", output={"id": "FB", "friendly": "FB"}, input=None)
    act = _rename_action("Foo%sBar%sBaz" % (sep, sep))  # "Foo → Bar → Baz"
    assert act and act[0]["JsonRPCAction"]["parameters"] == ["Foo → Bar", "Baz"], act

    # the action methods themselves emit exactly ONE follow-up payload, stderr clean
    out, err = run_main({"method": "delete_profile_relist", "parameters": ["Music"], "settings": {}})
    assert err.strip() == "", err
    assert json.loads(out) == {"method": "Flow.Launcher.ChangeQuery", "parameters": ["ac d ", True]}
    out, err = run_main({"method": "rename_profile", "parameters": ["Foo", "Renamed"], "settings": {}})
    assert err.strip() == "", err
    assert json.loads(out) == {"method": "Flow.Launcher.ChangeQuery", "parameters": ["ac p ", True]}
    # a colliding rename action emits a single ShowMsg (not a ChangeQuery, not a crash)
    out, err = run_main({"method": "rename_profile", "parameters": ["Renamed", "Gaming"], "settings": {}})
    assert err.strip() == "" and json.loads(out)["method"] == "Flow.Launcher.ShowMsg"

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  delete + rename wire (parsing + one-payload actions)")


def test_profile_ranking_wire():
    """The best-matching profile must be the highlighted row (top Score) so Enter applies
    it — in `ac p <filter>` and straight from the root (`ac <name>`). Issue #1."""
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)
    for name in ("My Realtek", "Realtek", "Realtek Speakers", "Headset"):
        profiles.save_profile(name, output={"id": "O-" + name, "friendly": "Spk"}, input=None)

    def ranked(q):
        out, err = run_main({"method": "query", "parameters": [q], "settings": {}})
        assert err.strip() == "", "stderr for %r:\n%s" % (q, err)
        # Flow orders results by Score desc (stable); the first one is highlighted
        return sorted(json.loads(out)["result"], key=lambda r: -r["Score"])

    def applies(r):
        act = r.get("JsonRPCAction", {})
        return act["parameters"][0] if act.get("method") == "load_profile" else None

    # ac p: profiles outrank Save/Rename/Delete (object-first), saved order kept
    res = ranked("p")
    assert [applies(r) for r in res[:4]] == ["My Realtek", "Realtek", "Realtek Speakers",
                                             "Headset"], [r["Title"] for r in res]

    # filtered: exact > prefix > substring, non-matches dropped
    want = ["Realtek", "Realtek Speakers", "My Realtek"]
    res = ranked("p realtek")
    assert [applies(r) for r in res[:3]] == want, [r["Title"] for r in res]
    assert "Headset" not in [applies(r) for r in res]

    # root: a non-sub-command query matches profile names (case-insensitive), same ranking
    res = ranked("REALTEK")
    assert [applies(r) for r in res[:3]] == want, [r["Title"] for r in res]
    assert res[0].get("ContextData") == ["profile", "Realtek"]  # Shift+Enter menu works
    assert applies(ranked("head")[0]) == "Headset"
    assert applies(ranked("realtek sp")[0]) == "Realtek Speakers"

    # root: no match -> the unchanged top menu
    top = json.loads(run_main({"method": "query", "parameters": [""], "settings": {}})[0])
    nomatch = json.loads(run_main({"method": "query", "parameters": ["zzz"], "settings": {}})[0])
    assert nomatch == top, nomatch

    # sub-command words still route to their menus, never to root profile matching
    profiles.save_profile("Out Loud", output={"id": "OL", "friendly": "Spk"}, input=None)
    assert not [r for r in ranked("out loud") if applies(r)]
    assert applies(ranked("p out loud")[0]) == "Out Loud"  # still reachable via ac p

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  profile ranking (ac p filter + root-level apply)")

def test_resolve():
    """Slot -> live endpoint: known ids (in order) > stable id > unique name match."""
    import audio
    importlib.reload(audio)
    nk = audio.name_key
    assert nk("Speakers (2- FiiO K11)") == nk("Speakers (FiiO K11)") == "speakers (fiio k11)"
    assert nk("2- Speakers") == "speakers" and nk("Speakers (FiiO K11) (2)") == nk("speakers  (FiiO K11)")
    assert nk("Speakers (Realtek(R) Audio)") == "speakers (realtek(r) audio)"  # "(R)" kept
    assert nk("") == "" and nk(None) == ""

    def dev(i, friendly, state="Active", stable=None, direction="render"):
        return {"id": i, "direction": direction, "friendly": friendly, "state": state,
                "stable_id": stable, "is_default": False}

    uac2 = dev("U2", "Speakers (FiiO K11)", state="Not present", stable="S2")
    uac1 = dev("U1", "Speakers (2- FiiO K11)", stable="S1")
    rtk = dev("R", "Speakers (Realtek)")
    mic = dev("M", "Speakers (FiiO K11)", direction="capture")  # wrong direction: ignored
    devs = [uac2, uac1, rtk, mic]

    # a known id only counts while its endpoint is Active
    assert audio.resolve(devs, "render", ["R"]) == (rtk, "id")
    assert audio.resolve(devs, "render", ["U2", "U1"]) == (uac1, "id")
    assert audio.resolve(devs, "render", ["R", "U1"])[0] is rtk  # primary id wins
    # stable id beats the name, and needs no name match at all
    assert audio.resolve(devs, "render", ["gone"], ["S1"], "whatever") == (uac1, "stable")
    # name fallback: the UAC2 endpoint is Not present, the UAC1 one is the unique match
    assert audio.resolve(devs, "render", ["U2"], ["S2"], "Speakers (FiiO K11)") == (uac1, "name")
    # two active endpoints with the same name: never guess
    twin = dev("T", "Speakers (FiiO K11)")
    assert audio.resolve(devs + [twin], "render", ["U2"], (), "Speakers (FiiO K11)") == (None, "ambiguous")
    assert audio.resolve(devs, "render", ["U2"], (), "Headphones (Other)") == (None, "absent")
    assert audio.resolve(devs, "render", [], (), "") == (None, "absent")
    print("OK  resolve (id > stable id > unique name)")


def test_profile_ids():
    """altIds / stableIds: stored, learned, linked, and kept across a same-device overwrite."""
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)

    profiles.save_profile("DAC", output={"id": "U2", "friendly": "Spk (FiiO)", "stable_id": "S2"},
                          input={"id": "M", "friendly": "Mic"})
    rec = profiles.get_profile("DAC")["output"]
    assert rec == {"endpointId": "U2", "friendlyName": "Spk (FiiO)", "stableIds": ["S2"]}, rec
    assert "stableIds" not in profiles.get_profile("DAC")["input"]  # no stable id: key omitted
    assert profiles.record_ids(None) == [] and profiles.record_ids(rec) == ["U2"]

    # remember the other UAC mode's endpoint: primary id kept, label kept
    other = {"id": "U1", "friendly": "Spk (2- FiiO)", "stable_id": "S1"}
    assert profiles.remember_device("dac", "output", other) is True
    rec = profiles.get_profile("DAC")["output"]
    assert rec["endpointId"] == "U2" and rec["altIds"] == ["U1"]
    assert rec["stableIds"] == ["S2", "S1"] and rec["friendlyName"] == "Spk (FiiO)"
    assert profiles.record_ids(rec) == ["U2", "U1"]
    assert profiles.remember_device("DAC", "output", other) is False  # nothing new: no write
    assert profiles.remember_device("nope", "output", other) is False
    try:
        profiles.remember_device("DAC", "speakers", other)
        assert False, "unknown slot should raise"
    except ValueError:
        pass

    # overwrite while on a KNOWN endpoint: that id becomes primary, the others stay
    profiles.save_profile("DAC", output={"id": "U1", "friendly": "Spk (2- FiiO)", "stable_id": "S1"},
                          input=None)
    rec = profiles.get_profile("DAC")["output"]
    assert rec["endpointId"] == "U1" and rec["altIds"] == ["U2"], rec
    assert rec["stableIds"] == ["S1", "S2"] and rec["friendlyName"] == "Spk (2- FiiO)"

    # overwrite with a DIFFERENT device (unknown id and stable id): learned ids are dropped
    profiles.save_profile("DAC", output={"id": "R", "friendly": "Spk (FiiO)"}, input=None)
    assert profiles.get_profile("DAC")["output"] == {"endpointId": "R", "friendlyName": "Spk (FiiO)"}

    # drop: forget deleted ids; a dropped primary hands its place to the device
    profiles.save_profile("Rot", output={"id": "P", "friendly": "Spk"}, input=None)
    profiles.remember_device("Rot", "output", {"id": "A", "friendly": "Spk"})
    assert profiles.remember_device("Rot", "output", {"id": "N", "friendly": "Spk", "stable_id": "SN"},
                                    drop=["P"]) is True
    rec = profiles.get_profile("Rot")["output"]
    assert rec["endpointId"] == "N" and rec["altIds"] == ["A"] and rec["stableIds"] == ["SN"], rec
    # dropping a non-primary id keeps the primary first
    assert profiles.remember_device("Rot", "output", {"id": "M", "friendly": "Spk"}, drop=["A"]) is True
    assert profiles.record_ids(profiles.get_profile("Rot")["output"]) == ["N", "M"]

    # an empty slot just takes the linked device
    assert profiles.remember_device("DAC", "input", {"id": "M2", "friendly": "Mic 2"}) is True
    assert profiles.get_profile("DAC")["input"] == {"endpointId": "M2", "friendlyName": "Mic 2"}

    shutil.rmtree(tmp, ignore_errors=True)
    print("OK  profile altIds / stableIds (remember, link, overwrite)")


class _FakeBackend(object):
    """Stands in for audio.get_devices / set_default inside main.py (in-process)."""

    def __init__(self, devices):
        self.devices = devices
        self.set_calls = []

    def get_devices(self, include_inactive=False):
        return [dict(d) for d in self.devices if include_inactive or d["state"] == "Active"]

    def set_default(self, item_id, role="all"):
        self.set_calls.append(item_id)
        direction = [d for d in self.devices if d["id"] == item_id][0]["direction"]
        for d in self.devices:
            if d["direction"] == direction:
                d["is_default"] = d["id"] == item_id
        return True


def _run_inproc(main_mod, method, params):
    """Run one Flow request through main.AudioCowboy in-process; return the parsed payload."""
    import io
    argv, stdout = sys.argv, sys.stdout
    sys.argv = ["main.py", json.dumps({"method": method, "parameters": params, "settings": {}})]
    sys.stdout = buf = io.StringIO()
    try:
        main_mod.AudioCowboy()
    finally:
        sys.argv, sys.stdout = argv, stdout
    return json.loads(buf.getvalue())


def test_mode_switch_apply():
    """A DAC in its other UAC mode is a new endpoint: the profile still applies (by name),
    the context menu offers a link, and a link (or a stable-id hit) is remembered."""
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    os.makedirs(os.path.join(tmp, "FlowLauncher"))
    import profiles
    importlib.reload(profiles)
    import main
    saved = (main.audio.get_devices, main.audio.set_default)

    def dev(i, friendly, state="Active", stable=None, direction="render", default=False):
        return {"id": i, "direction": direction, "friendly": friendly, "state": state,
                "stable_id": stable, "is_default": default, "is_default_comm": False}

    def toast(obj):
        assert obj["method"] == "Flow.Launcher.ShowMsg", obj
        return obj["parameters"][0], obj["parameters"][1]

    def links(name):
        res = _run_inproc(main, "context_menu", [["profile", name]])["result"]
        return [r["JsonRPCAction"]["parameters"] for r in res
                if r.get("JsonRPCAction", {}).get("method") == "link_device"]

    try:
        # saved in UAC2 (U2); now the DAC is in UAC1 (U1, Windows numbered it "2- ")
        profiles.save_profile("DAC", output={"id": "U2", "friendly": "Speakers (FiiO K11)",
                                             "stable_id": "S2"}, input=None)
        fake = _FakeBackend([
            dev("U2", "Speakers (FiiO K11)", state="Not present", stable="S2"),
            dev("U1", "Speakers (2- FiiO K11)", stable="S1"),
            dev("R", "Speakers (Realtek)", default=True),
        ])
        main.audio.get_devices, main.audio.set_default = fake.get_devices, fake.set_default

        title, sub = toast(_run_inproc(main, "load_profile", ["DAC"]))
        assert fake.set_calls == ["U1"] and title == "Applied profile: DAC", (title, sub)
        assert "(by name)" in sub, sub
        # U2 still exists (Not present), so the name hit may be a twin: nothing is stored
        assert profiles.get_profile("DAC")["output"] == {
            "endpointId": "U2", "friendlyName": "Speakers (FiiO K11)", "stableIds": ["S2"]}

        # badge: the name fallback finds it, so the profile isn't flagged unavailable
        res = _run_inproc(main, "query", ["p"])["result"]
        assert [r for r in res if r["Title"].endswith("DAC") and "⚠" not in r["SubTitle"]], res

        # link offers just the name pick (no input slot -> no input links)
        assert links("DAC") == [["DAC", "output", "U1"]], links("DAC")

        # linking remembers U1 for good, then applies by id (no "(by name)")
        fake.set_calls[:] = []
        title, sub = toast(_run_inproc(main, "link_device", ["DAC", "output", "U1"]))
        rec = profiles.get_profile("DAC")["output"]
        assert rec["endpointId"] == "U2" and rec["altIds"] == ["U1"] and rec["stableIds"] == ["S2", "S1"]
        assert fake.set_calls == ["U1"] and title == "Applied profile: DAC" and "(by name)" not in sub
        assert links("DAC") == []  # resolves by id now

        # back in UAC2: the primary id is live again and wins
        fake.devices[0]["state"], fake.devices[1]["state"] = "Active", "Not present"
        fake.set_calls[:] = []
        toast(_run_inproc(main, "load_profile", ["DAC"]))
        assert fake.set_calls == ["U2"]

        # Windows deleted the saved endpoint outright (not even "Not present") and made a
        # new one: the name hit re-points the slot, dropping the dead id, and keeps the
        # stable id so the next lookup is direct
        profiles.save_profile("Rotated", output={"id": "DEAD", "friendly": "耳機 (2- FiiO K12)"},
                              input=None)
        fake.devices.append(dev("FRESH", "耳機 (FiiO K12)", stable="SF"))
        fake.set_calls[:] = []
        title, sub = toast(_run_inproc(main, "load_profile", ["Rotated"]))
        assert fake.set_calls == ["FRESH"] and "(by name)" in sub, (title, sub)
        rec = profiles.get_profile("Rotated")["output"]
        assert rec["endpointId"] == "FRESH" and "altIds" not in rec and rec["stableIds"] == ["SF"], rec
        assert rec["friendlyName"] == "耳機 (2- FiiO K12)"  # the saved label stays
        _, sub = toast(_run_inproc(main, "load_profile", ["Rotated"]))
        assert "(by name)" not in sub, sub

        # stable-id hit under a new endpoint id + new name: applied AND the id is learned
        profiles.save_profile("Stable", output={"id": "OLD", "friendly": "Spk",
                                                "stable_id": "S9"}, input=None)
        fake.devices.append(dev("NEW", "Headphones (Renamed)", stable="S9"))
        fake.set_calls[:] = []
        title, _ = toast(_run_inproc(main, "load_profile", ["Stable"]))
        assert fake.set_calls == ["NEW"] and title == "Applied profile: Stable"
        assert profiles.get_profile("Stable")["output"]["altIds"] == ["NEW"]

        # two active endpoints share the saved name: refuse to guess, switch nothing
        profiles.save_profile("Twin", output={"id": "GONE", "friendly": "Speakers (USB Audio)"},
                              input=None)
        fake.devices += [dev("A1", "Speakers (USB Audio)"), dev("A2", "Speakers (2- USB Audio)")]
        fake.set_calls[:] = []
        title, sub = toast(_run_inproc(main, "load_profile", ["Twin"]))
        assert fake.set_calls == [] and title.startswith("Could not apply"), (title, sub)
        assert "several devices match" in sub and "Shift+Enter" in sub, sub

        # nothing matches at all: flagged in the list, and every active output is linkable
        profiles.save_profile("Lost", output={"id": "X", "friendly": "Headphones (Gone)"}, input=None)
        res = _run_inproc(main, "query", ["p lost"])["result"]
        assert [r for r in res if "⚠ output unavailable" in r["SubTitle"]], res
        assert len(links("Lost")) == len([d for d in fake.devices
                                          if d["state"] == "Active" and d["direction"] == "render"])
    finally:
        main.audio.get_devices, main.audio.set_default = saved
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK  UAC mode switch (name fallback, link, stable-id learning, ambiguity)")


if __name__ == "__main__":
    test_profiles()
    test_rename()
    test_corruption_backup()
    test_audio_helpers()
    test_pycaw_warning_suppressed()
    test_pycaw_devices_mapping()
    test_wire()
    test_delete_rename_wire()
    test_profile_ranking_wire()
    test_resolve()
    test_profile_ids()
    test_mode_switch_apply()
    print("\nALL PASS")
