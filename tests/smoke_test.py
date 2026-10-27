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


if __name__ == "__main__":
    test_profiles()
    test_rename()
    test_corruption_backup()
    test_audio_helpers()
    test_pycaw_warning_suppressed()
    test_wire()
    test_delete_rename_wire()
    test_profile_ranking_wire()
    print("\nALL PASS")
