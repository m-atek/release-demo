"""The dashboard reads what the load generator and the canary write."""

import json

import pricing

DASHBOARD = json.loads((pricing.HERE / "dashboard.json").read_text())


def panel(title):
    return next(p for p in DASHBOARD["panels"] if p.get("title") == title)


def test_the_panels_the_video_shows():
    for title in ("Request share", "Error rate", "p95 latency"):
        expr = panel(title)["targets"][0]["expr"]
        assert 'source="release-demo-loadgen"' in expr and "version" in expr


def test_request_share_draws_each_named_version_down_to_zero():
    assert 'source="release-demo-loadgen-versions"' in panel("Request share")["targets"][0]["expr"]


def test_the_bands_use_the_canary_tags():
    tags = [a["target"]["tags"] for a in DASHBOARD["annotations"]["list"]]
    assert ["pricing-api", "canary-window"] in tags
    assert ["pricing-api", "canary-alarm"] in tags
    source = (pricing.HERE / "canary.py").read_text()
    assert '["canary-window"]' in source and '["canary-alarm"]' in source


def test_it_carries_the_demo_tag():
    # The videos open with their own disclaimer, so no panel says it on screen.
    assert "release-demo" in DASHBOARD.get("tags", [])
