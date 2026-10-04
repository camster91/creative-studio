"""
Landing pricing copy matches the subscription plans and credit weights,
and the draft /terms page is served and linked.

Run:  pytest tests/test_pricing_copy.py -v
"""
from pathlib import Path

from creative_studio_app.billing import CREDIT_WEIGHT_BY_TIER

REPO = Path(__file__).parent.parent


def _landing():
    return (REPO / "templates" / "landing.html").read_text()


def test_landing_no_longer_claims_no_subscriptions():
    assert "No subscriptions" not in _landing()


def test_landing_lists_plans():
    src = _landing()
    for text in ("$19", "100 credits", "$49", "500 credits", "$99", "1,500 credits"):
        assert text in src


def test_landing_tier_credits_match_weight_table():
    pricing = _landing().split('id="pricing"')[1].split('id="faq"')[0]
    for tier, weight in CREDIT_WEIGHT_BY_TIER.items():
        label = f'<div class="tier-label">{tier.capitalize()}</div>'
        card = pricing.split(label)[1]
        unit = "credit" if weight == 1 else "credits"
        assert f'<div class="tier-price">{weight} {unit}</div>' in card.split("</ul>")[0]


def test_terms_page_is_draft_and_linked():
    import importlib.util
    import sys

    scripts = REPO / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("creative_studio_web_terms", scripts / "creative-studio-web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web_terms"] = mod
    spec.loader.exec_module(mod)
    r = mod.app.test_client().get("/terms")
    assert r.status_code == 200
    assert "Draft — not legally reviewed" in r.get_data(as_text=True)
    footer = _landing().split('class="landing-footer"')[1]
    assert 'href="/terms"' in footer
    pricing = _landing().split('id="pricing"')[1].split('id="faq"')[0]
    assert 'href="/terms"' in pricing
