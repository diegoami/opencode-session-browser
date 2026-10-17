from conftest import spec
from opencode_session_browser import tracker


def test_track_by_id_prefix_and_key(make_registry, std_root):
    reg = make_registry([spec("windows", std_root, label="Windows")])
    t = tracker.track(reg, "ses_root0001")
    assert t["found"] and t["key"] == "windows~ses_root0001" and t["session"]["source"] == "Windows"
    assert [c["id"] for c in t["session"]["children"]] == ["ses_child001", "ses_child002"]
    kinds = [s["kind"] for s in t["steps"]]
    assert kinds[0] == "user" and "says" in kinds and "error" in kinds
    tools = [s for s in t["steps"] if s["kind"] == "tool"]
    assert any("needle_in_haystack" in s["input"] and s["status"] == "completed" for s in tools)
    assert any(s["tool"] == "task" and s["child_session_id"] == "ses_child001" for s in tools)
    assert any(s["status"] == "error" and "cancelled" in s["error"] for s in tools)
    assert tracker.track(reg, "windows~ses_root0001")["id"] == "ses_root0001"
    assert tracker.track(reg, "ses_grand0")["id"] == "ses_grand001"           # unique prefix
    assert not tracker.track(reg, "ses_child0")["found"]                      # ambiguous prefix
    assert not tracker.track(reg, "ses_nope")["found"]
    again = tracker.track(reg, "ses_root0001", fp=t["fingerprint"])
    assert again["unchanged"] and "steps" not in again
    assert tracker.track(reg, "ses_child002")["session"]["parent"]["id"] == "ses_root0001"
