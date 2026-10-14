import pytest

from seedance_mcp.pricing import PriceBook, PricingError, money

SEEDANCE_25 = "dreamina-seedance-2-5-260628"
MINI = "dreamina-seedance-2-0-mini-260615"
SEEDREAM_PRO = "dola-seedream-5-0-pro-260628"

prices = PriceBook()


@pytest.mark.parametrize(
    ("resolution", "duration", "billed"),
    [
        # completion_tokens from real Seedance 2.5 tasks (9:16, 24 fps)
        ("720p", 4, 87_300),
        ("720p", 5, 108_900),
        ("720p", 10, 216_900),
        ("480p", 15, 144_512),
    ],
)
def test_output_tokens_match_real_bills(resolution, duration, billed):
    est = prices.estimate_video(SEEDANCE_25, resolution=resolution, duration=duration)
    assert est.tokens == billed


def test_reference_video_tokens_match_real_bill():
    # A real 4 s clip with a known 4 s reference video billed 173,700 tokens.
    est = prices.estimate_video(SEEDANCE_25, resolution="720p", duration=4, input_videos=[4.0])
    assert est.tokens == 173_700
    assert est.usd == pytest.approx(173_700 * 6.40 / 1e6)
    assert est.usd_max == pytest.approx(est.usd)


def test_unknown_reference_length_gives_a_worst_case():
    est = prices.estimate_video(SEEDANCE_25, resolution="720p", duration=5, input_videos=[None])
    assert est.usd_max > est.usd
    assert "unknown length" in est.notes[0]


def test_dollars_for_common_clips():
    assert prices.estimate_video(SEEDANCE_25, resolution="720p", duration=5).usd == pytest.approx(
        1.165, abs=0.001
    )
    assert prices.estimate_video(MINI, resolution="480p", duration=5).usd == pytest.approx(
        0.17, abs=0.01
    )


def test_aliases():
    assert prices.resolve_video("2.5") == SEEDANCE_25
    assert prices.resolve_video("Mini") == MINI
    assert prices.resolve_image("pro") == SEEDREAM_PRO
    assert prices.resolve_video("some-new-model") == "some-new-model"


def test_mini_cannot_do_1080p_or_smart_length():
    with pytest.raises(PricingError, match="480p, 720p"):
        prices.estimate_video(MINI, resolution="1080p", duration=5)
    with pytest.raises(PricingError, match="smart duration"):
        prices.estimate_video(MINI, resolution="720p", duration=-1)


def test_unknown_model_is_unpriced():
    est = prices.estimate_video("brand-new-model", resolution="720p", duration=5)
    assert est.usd is None and not est.priced


def test_image_prices():
    assert prices.image_cost(SEEDREAM_PRO, "2K", 1) == pytest.approx(0.09)
    assert prices.image_cost(SEEDREAM_PRO, "1K", 1) == pytest.approx(0.045)
    assert prices.image_cost(SEEDREAM_PRO, "1K", 2, input_images=3) == pytest.approx(0.096)
    assert prices.image_cost("dola-seedream-5-0-flash-260915", "2K", 1) == pytest.approx(0.018)


def test_infer_video_input():
    assert not PriceBook.infer_video_input("720p", 5, 108_900)
    assert PriceBook.infer_video_input("720p", 5, 195_300)


def test_price_overrides(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        '{"video": {"my-model": {"name": "Mine", "max_input_video_seconds": 10,'
        ' "rates": {"sd": [1.0, 0.5]}}}}'
    )
    book = PriceBook.load(path)
    assert book.estimate_video("my-model", resolution="720p", duration=5).usd == pytest.approx(
        0.1089
    )


def test_money():
    assert money(1.1556) == "$1.16"
    assert money(0.09) == "$0.09"
    assert money(0.045) == "$0.045"
    assert money(None) == "an unknown amount"
