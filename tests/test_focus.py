"""Fictional identities: same nicknames/accounts across rooms must not merge."""

from unittest.mock import patch

import pytest

from marketpulse.bulk import parse_group_members, parse_group_nicknames, read_group_members
from marketpulse.focus import selections, own_evidence, validate_content, update_focus


def fixture():
    sources = {
        "T001": dict(
            id="T001", group_id="room-a", sender_id="account-a", speaker="成员1", time="09:31:00", text="样本股暂时观察"
        ),
        "T002": dict(
            id="T002", group_id="room-a", sender_id="account-b", speaker="成员2", time="09:32:00", text="同名用户的看法"
        ),
        "T003": dict(
            id="T003",
            group_id="room-b",
            sender_id="account-a",
            speaker="成员3",
            time="09:33:00",
            text="同账号另一群的看法",
        ),
    }
    records = [
        dict(
            group_id=s["group_id"],
            sender_id=s["sender_id"],
            sent_at="2025-01-02T" + s["time"] + "+08:00",
            message_kind="text",
        )
        for s in sources.values()
    ]
    result = dict(
        groups=[dict(id="room-a", name="虚构甲群"), dict(id="room-b", name="虚构乙群")],
        sources=sources,
        media=[],
        visuals=[],
        members=[
            dict(alias="成员1", group_id="room-a", sender_id="account-a", name="同名成员"),
            dict(alias="成员2", group_id="room-a", sender_id="account-b", name="同名成员"),
            dict(alias="成员3", group_id="room-b", sender_id="account-a", name="另一群昵称"),
        ],
    )
    selected = dict(group_id="room-a", sender_id="account-a", display_name="旧名")
    cfg = dict(provider="rules", input="fixture.jsonl", focus_members=[selected])
    return result, records, selected, cfg


def point(sources):
    return dict(title="测试观点", text="仅作测试", sources=sources)


def content(sources):
    return dict(overview=point(sources), positions=[], changes=[], watch=[], limitations=[])


def test_focus_is_explicit_and_scoped_to_selected_rooms():
    _, _, selected, cfg = fixture()
    assert selections(cfg, {"room-b"}) == []
    assert selections({"focus_members": [selected, selected]}, {"room-a"}) == [selected]
    with pytest.raises(ValueError, match="不能用昵称"):
        selections({"focus_members": [{"group_id": "room-a", "name": "同名成员"}]}, {"room-a"})


def test_same_nickname_and_same_account_in_other_room_do_not_leak_into_focus(tmp_path):
    result, records, selected, cfg = fixture()
    sources, _, _ = own_evidence(result, selected)
    assert set(sources) == {"T001"}
    update_focus(result, cfg, records, tmp_path)
    focus = result["focus_members"][0]
    assert focus["message_count"] == 1 and focus["name"] == "同名成员"
    assert focus["content"]["positions"][0]["sources"] == ["T001"]
    assert "同名用户" not in str(focus["content"]) and "另一群" not in str(focus["content"])


def test_model_refs_cannot_borrow_another_person_or_group():
    result, _, selected, _ = fixture()
    sources, media, visuals = own_evidence(result, selected)
    for sid in ("T002", "T003", "T999"):
        with pytest.raises(ValueError, match="他人发言"):
            validate_content(content([sid]), sources, media, visuals)


def test_change_requires_two_distinct_times_not_two_different_evidence_types():
    result, _, selected, _ = fixture()
    sources, _, _ = own_evidence(result, selected)
    data = content(["T001"])
    data["changes"] = [point(["T001", "I01"])]
    visuals = [dict(image_id="I01")]
    with pytest.raises(ValueError, match="两个时点"):
        validate_content(data, sources, {"I01": {"sent_at": "2025-01-02T09:31:00+08:00"}}, visuals)
    assert validate_content(data, sources, {"I01": {"sent_at": "2025-01-02T10:31:00+08:00"}}, visuals)


def test_no_synced_messages_does_not_call_model_or_invent_stance(tmp_path):
    result, records, _, cfg = fixture()
    cfg.update(provider="codex", focus_members=[dict(group_id="room-a", sender_id="silent", display_name="安静成员")])
    with patch("marketpulse.focus.call_model", side_effect=AssertionError("must not call")):
        update_focus(result, cfg, records, tmp_path)
    member = result["focus_members"][0]
    assert member["status"] == "no_messages" and member["content"] is None
    assert member["message_count"] == 0 and member["name"] == "安静成员"


def test_name_change_reuses_model_analysis_and_updates_heading(tmp_path):
    result, records, _, cfg = fixture()
    cfg["provider"] = "codex"
    with patch("marketpulse.focus.call_model", return_value=content(["T001"])) as model:
        update_focus(result, cfg, records, tmp_path)
        first = result["focus_members"][0]["content"]
        result["members"][0]["name"] = "更新昵称"
        update_focus(result, cfg, records, tmp_path)
        assert model.call_count == 1
    assert result["focus_members"][0]["name"] == "更新昵称"
    assert result["focus_members"][0]["content"] == first


def test_focus_failure_preserves_group_content_without_silent_rules_fallback(tmp_path):
    result, records, _, cfg = fixture()
    cfg["provider"] = "codex"
    result["content"] = {"headline": "原群总结"}
    with patch("marketpulse.focus.call_model", side_effect=RuntimeError("model failed")):
        update_focus(result, cfg, records, tmp_path)
    assert result["content"] == {"headline": "原群总结"}
    assert result["focus_members"][0]["status"] == "unavailable"
    assert result["focus_members"][0]["content"] is None


def test_image_authors_and_recirculated_reports_respect_identity():
    result, _, selected, _ = fixture()
    result["media"] = [
        dict(id="I01", group_id="room-a", sender_id="account-a"),
        dict(id="I02", group_id="room-a", sender_id="account-b"),
    ]
    result["visuals"] = [dict(image_id="I01", excluded_from_analysis=True), dict(image_id="I02")]
    _, media, visuals = own_evidence(result, selected)
    assert set(media) == {"I01"} and visuals == []


def test_room_members_without_custom_nicknames_are_retained_and_contacts_filtered():
    def field(n, value):
        value = value.encode() if isinstance(value, str) else value
        return bytes([n * 8 + 2, len(value)]) + value

    raw = field(1, field(1, "a")) + field(1, field(1, "b") + field(2, "群昵称"))
    assert parse_group_members(raw) == {"a": "", "b": "群昵称"}
    assert parse_group_nicknames(raw) == {"b": "群昵称"}
    from contextlib import nullcontext
    from pathlib import Path

    with (
        patch("marketpulse.bulk.read_config", return_value=(Path("fixture"), {})),
        patch("marketpulse.bulk.snapshot", return_value=nullcontext(Path("fixture.db"))),
        patch("marketpulse.bulk.key_for", return_value="fixture"),
        patch("marketpulse.bulk._room_members", return_value={"a": "", "b": "群昵称"}),
        patch(
            "marketpulse.bulk._contact_rows",
            return_value=[
                dict(username="a", nick_name="联系人昵称"),
                dict(username="b", nick_name="旧昵称"),
                dict(username="outsider", nick_name="非群成员"),
            ],
        ),
    ):
        members = read_group_members("room-a")
    assert [(m["sender_id"], m["name"]) for m in members] == [("a", "联系人昵称"), ("b", "群昵称")]
