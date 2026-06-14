"""
Regression tests for /privacy — the static privacy policy page
that ships with the waitlist to satisfy "we collect PII" obligations.

Run:  pytest tests/test_refactor_ws1 privacy.py -v
"""
from pathlib import Path
import re

import pytest

REPO = Path(__file__).parent.parent


class TestPrivacyPage:
    """The privacy page is one of the things the waitlist launch
    skipped. It needs to: (1) exist, (2) be linked from the landing
    page, (3) disclose what data we collect, (4) make it easy to
    ask for deletion. This test is a checklist of those obligations."""

    def test_privacy_route_responds_200(self):
        """The route exists and is public (no auth)."""
        import importlib.util, os, sys
        web_path = REPO / "scripts" / "creative-studio-web.py"
        sys.path.insert(0, str(web_path.parent))
        os.environ.setdefault("GEMINI_API_KEY", "test-key")
        spec = importlib.util.spec_from_file_location("csw", web_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        client = mod.app.test_client()
        r = client.get("/privacy")
        assert r.status_code == 200, f"/privacy returned {r.status_code}"

    def test_privacy_page_discloses_what_we_collect(self):
        """The page must say what data we collect. Specifically:
        email (waitlist), prompts/images (Gemini API), API key location
        (browser localStorage), logs (HTTP access only)."""
        from pathlib import Path as P
        # Use the source so we don't have to load the full app
        template = (REPO / "templates" / "privacy.html").read_text().lower()
        for required in [
            "email",          # waitlist
            "prompts",        # gemini inputs
            "localstorage",   # api key storage location
            "log",            # access logs
        ]:
            assert required in template, f"privacy page missing disclosure of: {required!r}"

    def test_privacy_page_discloses_no_third_party_ai_training(self):
        """Critical line: we don't train on user data. The page must
        say this explicitly to distinguish us from competitors that do."""
        template = (REPO / "templates" / "privacy.html").read_text().lower()
        assert "do not train" in template or "no third-party" in template

    def test_privacy_page_has_deletion_contact(self):
        """GDPR/CCPA right-to-deletion: a contact email for users to
        request their data be deleted."""
        template = (REPO / "templates" / "privacy.html").read_text().lower()
        assert "delete" in template
        assert "hello@ashbi.ca" in template

    def test_landing_page_links_to_privacy(self):
        """The landing page must link to the privacy policy. The
        waitlist form is collecting PII; users need to know what
        they're consenting to before they hit Submit."""
        src = (REPO / "templates" / "landing.html").read_text()
        assert "/privacy" in src, "landing page should link to /privacy"
        # The link should be in the footer
        footer = src.split("landing-footer")[1]
        assert "/privacy" in footer

    def test_no_404_when_privacy_link_clicked(self):
        """The link in the footer must work. (The template+route
        should be wired up; this catches the case where someone adds
        the template but forgets the route.)"""
        import importlib.util, os, sys
        web_path = REPO / "scripts" / "creative-studio-web.py"
        sys.path.insert(0, str(web_path.parent))
        os.environ.setdefault("GEMINI_API_KEY", "test-key")
        spec = importlib.util.spec_from_file_location("csw", web_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        client = mod.app.test_client()
        r = client.get("/privacy")
        assert r.status_code == 200
        assert "Privacy" in r.get_data(as_text=True)
