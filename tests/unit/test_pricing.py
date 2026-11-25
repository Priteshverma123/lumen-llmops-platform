from datetime import UTC, datetime

from lumen.pricing.catalog import PriceCatalog

TS = datetime(2026, 11, 15, tzinfo=UTC)
CAT = PriceCatalog.from_dicts([{"version": "v", "effective_from": "2026-01-01", "models": [
    {"provider": "anthropic", "match": "claude-.*", "input": 9, "output": 9},
    {"provider": "anthropic", "match": "claude-haiku-5-5.*", "input": 0.1, "output": 0.5,
     "tiers": [{"above_input_tokens": 100000, "input": 0.5, "output": 2.5}]},
    {"provider": "openai", "match": "o-reason.*", "input": 1, "output": 4, "reasoning": 8},
]}])


def test_most_specific_pattern_wins():
    r = CAT.cost(provider="anthropic", model="claude-haiku-5-5", ts=TS, input_tokens=1_000_000, output_tokens=0)
    assert abs(r.usd - 0.5) < 1e-12  # 1M input is above the 100k tier → 0.5/M


def test_below_tier_uses_base_price():
    r = CAT.cost(provider="anthropic", model="claude-haiku-5-5", ts=TS, input_tokens=50_000, output_tokens=0)
    assert abs(r.usd - 0.005) < 1e-12


def test_unknown_model_is_zero_not_guessed():
    r = CAT.cost(provider="other", model="x", ts=TS, input_tokens=1000, output_tokens=1000)
    assert (r.usd, r.source) == (0.0, "unknown")


def test_reasoning_tokens_priced_separately_when_configured():
    r = CAT.cost(provider="openai", model="o-reason-1", ts=TS, input_tokens=0, output_tokens=1_000_000,
                 reasoning=500_000)
    assert abs(r.usd - (0.5 * 4 + 0.5 * 8)) < 1e-9
