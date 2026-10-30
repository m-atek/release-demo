"""The before card of video 13 (acceptance check 9), and the history check's sources."""

import re

import history_sources
from common import ROOT

PAGE = (ROOT / "demo-data" / "bridge-call.html").read_text()


def test_the_invite_lists_six_invitees_by_role():
    roles = re.findall(r'<div class="role">([^<]+)</div>', PAGE)
    assert roles == ["On-call engineer", "Release manager", "Platform lead", "Service owner", "Database administrator",
                     "Change manager"]
    assert "6 invitees" in PAGE and "P1 bridge: orders page answering 502" in PAGE and "Starts: now" in PAGE
    assert "Find what changed" in PAGE


def test_the_column_fits_a_4_by_5_crop_and_the_footer_stays_below_the_first_screen():
    assert "max-width: 780px" in PAGE
    main_height = int(re.search(r"main \{[^}]*min-height: (\d+)px", PAGE).group(1))
    assert main_height > 1080
    assert PAGE.index("</main>") < PAGE.index("<footer>Fictional invite, made for a demo.</footer>")
    assert not re.search(r"\b\d{1,2}:\d{2}\b|a\.m\.|p\.m\.", PAGE)  # no time of day


def test_the_production_check_lines_come_from_the_build_log():
    console = "\n".join([
        "[Pipeline] sh", "ok   /health reports 1.2.4 after 2s", "Staging check passed",
        "[2026-10-27T15:01:02.123Z] ok   /health reports 1.2.4 after 3s",
        "[2026-10-27T15:01:02.523Z] FAIL / answered 502", "[2026-10-27T15:01:02.623Z] ok   /api/orders answered 200",
        "[2026-10-27T15:01:02.700Z] Production check failed", "ok   /health reports 1.2.3 after 1s"])
    assert history_sources.check_lines(console) == ["ok   /health reports 1.2.4 after 3s", "FAIL / answered 502",
                                                     "ok   /api/orders answered 200", "Production check failed"]
