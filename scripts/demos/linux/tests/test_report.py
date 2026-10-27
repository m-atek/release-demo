"""Each server's result in words, and the lines Slack shows when a rollout stops."""

import fleet
import report
from conftest import JAVA8, result


def stopped_at_dc01():
    return report.rows(fleet.servers(), [
        result("inv-cloud-01", "ok", "2.8.0", detail="ok   /health reports 2.8.0 after 0s"),
        result("inv-cloud-02", "ok", "2.8.0", detail="ok   /health reports 2.8.0 after 1s"),
        result("inv-dc-01", "rolled-back", "2.7.3", detail=JAVA8),
    ], "2.8.0")


def test_servers_come_in_rollout_order():
    assert [s["name"] for s in fleet.servers()] == ["inv-cloud-01", "inv-cloud-02", "inv-dc-01", "inv-dc-02"]
    assert [s["agent"] for s in fleet.servers()] == ["tools-host", "tools-host", "dc-agent", "dc-agent"]


def test_a_server_the_play_never_reached_is_not_started():
    table = stopped_at_dc01()
    assert [r["result"] for r in table] == ["ok", "ok", "rolled-back", "not-started"]
    assert report.stopped_at(table)["server"] == "inv-dc-01"


def test_the_slack_line_is_the_one_the_video_quotes():
    assert report.slack_line(stopped_at_dc01()) == "inv-dc-01 failed its check. Rolled back to 2.7.3. Rollout stopped."


def test_the_final_line_names_the_untouched_server():
    line = report.finish_line(stopped_at_dc01())
    assert line == "Rollout stopped at inv-dc-01 after {total}. inv-dc-02 untouched."
    assert line.format(total="6m 12s").startswith("Rollout stopped at inv-dc-01 after 6m 12s.")


def test_the_change_lists_all_four_servers_with_their_results():
    text = report.summary(stopped_at_dc01(), "2.8.0")
    assert "1. inv-cloud-01 (AWS eu-west-1): on 2.8.0, check ok in 24s. /health reports 2.8.0" in text
    assert "3. inv-dc-01 (Data center): 2.8.0 failed its check; rolled back to 2.7.3 and checked in 24s." in text
    assert "built for Java 11 (class file 55.0), this server runs Java 8" in text
    assert "4. inv-dc-02 (Data center): not started, the rollout stopped before it." in text
    assert text.endswith("Rollout stopped at inv-dc-01.")


def test_a_clean_rollout_has_no_stop():
    table = report.rows(fleet.servers(), [result(s["name"], "ok", "2.8.0") for s in fleet.servers()], "2.8.0")
    assert report.slack_line(table) is None
    assert report.summary(table, "2.8.0").endswith("All 4 servers run 2.8.0.")


def test_a_failure_before_the_switch_says_the_server_kept_its_release():
    table = report.rows(fleet.servers(), [result("inv-cloud-01", "failed", "2.7.3",
                                                 detail="FAIL inventory-2.8.0.zip has SHA-256 x, the build recorded y.")],
                        "2.8.0")
    assert report.slack_line(table) == "inv-cloud-01 failed before the switch. Still on 2.7.3. Rollout stopped."
    assert "the build recorded y." in report.line(table[0])
