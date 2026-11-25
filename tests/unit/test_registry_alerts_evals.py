import pytest

from lumen.alerts import detectors as D
from lumen.common.errors import InvalidInput
from lumen.evals.calibration import calibrate, cohens_kappa
from lumen.evals.evaluators import evaluate_cheap, pii_types, split_context
from lumen.registry.render import lint_cache_friendliness, render, validate_template

SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}


def test_render_validates_variables_and_is_strict():
    msgs = [{"role": "system", "content": "Be precise."}, {"role": "user", "content": "Q: {{ question }}"}]
    assert render(msgs, SCHEMA, {"question": "hi"})[1]["content"] == "Q: hi"
    with pytest.raises(InvalidInput):
        render(msgs, SCHEMA, {})
    with pytest.raises(InvalidInput):  # undefined variable, no schema: StrictUndefined
        render([{"role": "user", "content": "{{ missing }}"}], {}, {})


def test_sandbox_blocks_attribute_escape():
    payload = "{{ ''.__class__.__mro__[1].__subclasses__() }}"
    with pytest.raises(InvalidInput):
        render([{"role": "user", "content": payload}], {}, {})


def test_template_syntax_errors_rejected():
    with pytest.raises(InvalidInput):
        validate_template([{"role": "user", "content": "{{ unclosed"}])


def test_cache_friendliness_lint():
    bad = [{"role": "system", "content": "Today is {{ date }}. " + "Static rules. " * 40}]
    good = [{"role": "system", "content": "Static rules. " * 40}, {"role": "user", "content": "{{ q }}"}]
    assert lint_cache_friendliness(bad)
    assert not lint_cache_friendliness(good)


def test_burn_rate_multiwindow():
    fast = {"5m": (20, 100), "30m": (60, 600), "1h": (180, 1200), "6h": (300, 7200)}
    assert D.multiwindow_burn(fast, 0.99).firing
    calm = {"5m": (0, 100), "30m": (1, 600), "1h": (2, 1200), "6h": (10, 7200)}
    assert not D.multiwindow_burn(calm, 0.99).firing


def test_cost_anomaly_uses_robust_weekly_baseline():
    assert D.cost_anomaly(50.0, [10, 11, 9, 10], []).firing
    assert not D.cost_anomaly(11.5, [10, 11, 9, 10], []).firing
    assert not D.cost_anomaly(0.5, [0.01, 0.02, 0.01], []).firing  # below min_usd


def test_quality_cache_and_drift_detectors():
    assert D.quality_drop(0.80, 0.90, 100).firing
    assert not D.quality_drop(0.80, 0.90, 5).firing
    assert D.cache_hit_drop(0.1, 0.6).firing
    assert not D.cache_hit_drop(0.55, 0.6).firing
    a, b = [[1.0, 0.0]] * 30, [[0.0, 1.0]] * 30
    assert D.query_drift(a, b).firing
    assert not D.query_drift(a, a).firing


def test_cheap_evaluators():
    assert pii_types("mail me at jane.doe@example.com") == ["email"]
    assert "credit_card" not in pii_types("order 1234567812345678")  # fails Luhn
    assert "credit_card" in pii_types("card 4111 1111 1111 1111")
    s = {x.name: x.value for x in evaluate_cheap("I can't help with that.", "stop")}
    assert s["refusal"] == 1.0 and s["pii_leak"] == 0.0


def test_split_context_extracts_atlas_sources():
    ctx, _ = split_context("<sources><source id=\"1\">fact</source></sources><question>q</question>")
    assert "fact" in ctx


def test_judge_calibration_kappa():
    assert cohens_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    c = calibrate([0.9, 0.2, 0.8, 0.1, 0.7, 0.6], [1, 0, 1, 0, 0, 1])
    assert c.n == 6 and -1 <= c.kappa <= 1 and c.tp + c.tn + c.fp + c.fn == 6
