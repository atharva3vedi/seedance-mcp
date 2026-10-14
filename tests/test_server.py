import time

import pytest
from conftest import Dialog

from seedance_mcp.__main__ import main

pytestmark = pytest.mark.anyio

CLIP = dict(prompt="a cat surfing a wave at sunset", model="2.5", duration=5)


async def test_cheap_image_goes_straight_through(connect, fake_ark):
    dialog = Dialog()
    async with connect(dialog) as call:
        out = await call("generate_image", prompt="a lighthouse")
    assert out["ok"] and out["cost_usd"] == 0.09 and out["approved_by"] == "auto"
    assert dialog.messages == []
    assert fake_ark.images[0]["watermark"] is False


async def test_expensive_video_asks_in_a_dialog(connect, fake_ark):
    dialog = Dialog(approve=True)
    async with connect(dialog) as call:
        out = await call("generate_video", **CLIP)
    assert out["ok"] and out["approved_by"] == "dialog"
    assert "Spend about $1.17 on Seedance 2.5, 5s, 720p, with audio?" in dialog.messages[0]
    assert len(fake_ark.created) == 1


@pytest.mark.parametrize("answer", [False, None])
async def test_declined_or_dismissed_dialog_spends_nothing(connect, fake_ark, answer):
    async with connect(Dialog(approve=answer)) as call:
        out = await call("generate_video", **CLIP)
    assert out["status"] == "declined"
    assert fake_ark.created == []


async def test_without_dialogs_it_asks_in_chat(connect, fake_ark):
    async with connect(dialog=None) as call:
        first = await call("generate_video", **CLIP)
        assert first["status"] == "needs_your_ok"
        assert "$1.17" in first["ask_user"]
        assert fake_ark.created == []

        changed = await call(
            "generate_video", **{**CLIP, "duration": 10}, approval_id=first["approval_id"]
        )
        assert changed["status"] == "approval_invalid"

        second = await call("generate_video", **CLIP, approval_id=first["approval_id"])
        assert second["ok"] and second["approved_by"].startswith("single:")
    assert len(fake_ark.created) == 1


async def test_hard_cap_beats_any_approval(connect, fake_ark):
    async with connect(Dialog(approve=True), hard_cap_usd=1.0) as call:
        out = await call("generate_video", **CLIP)
    assert out["status"] == "blocked_by_hard_cap"
    assert fake_ark.created == []


async def test_batch_preapproval(connect, fake_ark):
    dialog = Dialog(approve=True)
    async with connect(dialog) as call:
        plan = [{"kind": "video", "model": "2.5", "duration": 5, "count": 2}]
        estimate = await call("estimate_cost", items=plan)
        assert estimate["total_usd"] == pytest.approx(2.33, abs=0.01)
        assert dialog.messages == []

        batch = await call("preapprove_spend", items=plan, note="two surf clips")
        assert batch["status"] == "approved" and len(dialog.messages) == 1
        for _ in range(2):
            out = await call("generate_video", **CLIP, approval_id=batch["approval_id"])
            assert out["ok"]
        third = await call("generate_video", **CLIP, approval_id=batch["approval_id"])
        assert third["status"] == "approval_invalid"
    assert len(dialog.messages) == 1 and len(fake_ark.created) == 2


async def test_batch_preapproval_in_chat(connect, fake_ark):
    async with connect(dialog=None) as call:
        plan = [{"kind": "video", "model": "mini", "resolution": "480p", "count": 3}]
        pending = await call("preapprove_spend", items=plan)
        assert pending["status"] == "needs_your_ok"
        early = await call(
            "generate_video",
            **{**CLIP, "model": "mini", "resolution": "480p"},
            approval_id=pending["approval_id"],
        )
        assert early["status"] == "approval_invalid"
        active = await call("preapprove_spend", approval_id=pending["approval_id"])
        assert active["status"] == "approved" and active["calls_left"] == 3


async def test_wait_settles_real_cost_and_saves(connect, tmp_path):
    out_dir = tmp_path / "renders"
    async with connect(Dialog()) as call:
        out = await call("generate_video", **CLIP, wait=True, save_to=str(out_dir))
        assert out["status"] == "succeeded"
        assert out["cost_usd"] == pytest.approx(1.165, abs=0.001)
        assert (out_dir / f"{out['task_id']}.mp4").read_bytes() == b"fake-media"
        status = await call("spend_status")
    assert status["spent_usd"] == pytest.approx(1.17, abs=0.01)
    assert status["by_model"][0]["model"] == "Seedance 2.5"


async def test_reference_video_from_an_earlier_task_is_priced_exactly(connect):
    async with connect(Dialog()) as call:
        first = await call("generate_video", **{**CLIP, "duration": 4}, wait=True)
        second = await call(
            "generate_video", **{**CLIP, "duration": 4}, reference_videos=[first["video_url"]]
        )
    assert second["estimate"]["tokens"] == 173_700
    assert "usd_max" not in second["estimate"]


async def test_spending_elsewhere_on_the_key_counts(connect, fake_ark):
    fake_ark.elsewhere.append(
        {
            "id": "cgt-from-a-script",
            "model": "dreamina-seedance-2-5-260628",
            "status": "succeeded",
            "duration": 10,
            "resolution": "720p",
            "created_at": int(time.time()),
            "usage": {"completion_tokens": 216_900},
        }
    )
    async with connect(Dialog(), budget_usd=2.0, ask_above_usd=5) as call:
        status = await call("spend_status")
        assert status["spent_usd"] == pytest.approx(2.32, abs=0.01)
        out = await call("generate_image", prompt="a lighthouse")
    assert out["approved_by"] == "dialog"  # already over budget, so even $0.09 asks


async def test_face_rejection_comes_with_a_fix(connect, fake_ark):
    fake_ark.reject_next = "InputImageSensitiveContentDetected.PrivacyInformation"
    async with connect(Dialog(), ask_above_usd=5) as call:
        out = await call("generate_video", **CLIP)
    assert out["status"] == "provider_error"
    assert "reference_videos" in out["hint"]


async def test_bad_resolution_is_caught_before_spending(connect, fake_ark):
    async with connect(Dialog()) as call:
        out = await call("generate_video", **{**CLIP, "model": "mini"}, resolution="1080p")
    assert out["status"] == "invalid_request" and fake_ark.created == []


async def test_check_setup_reports_dialog_support(connect):
    async with connect(Dialog()) as call:
        assert (await call("check_setup"))["approvals"] == "approval dialog"
    async with connect(dialog=None) as call:
        setup = await call("check_setup")
    assert setup["key_ok"] and "in chat" in setup["approvals"]


def test_cli_spend(make_settings, monkeypatch, capsys, tmp_path):
    home = tmp_path / "cli-home"
    home.mkdir()
    (home / "ledger.jsonl").write_text(
        '{"id":"img_1","kind":"image","model":"dola-seedream-5-0-pro-260628",'
        f'"status":"succeeded","created":{time.time()},"usd":0.09,"label":"Seedream 5.0 Pro"}}\n'
    )
    monkeypatch.setenv("SEEDANCE_HOME", str(home))
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    main(["--budget", "10", "spend"])
    out = capsys.readouterr().out
    assert "$0.09 of $10.00" in out and "Seedream 5.0 Pro" in out
