"""The bottom window: every figure from the node's own answers, in 44 columns, and the scheduler's words unchanged."""

import pytest

import room
from fake_node import MIB, FakeNode, pod

SAID = ("0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes are available: "
        "1 No preemption victims found for incoming pod.")
PENDING = "catalog-79c5d6f4c8-lm2xp"


@pytest.mark.parametrize("text, expected", [("48Mi", 48 * MIB), ("1208512Ki", 1208512 * 1024), ("1Gi", 1 << 30),
                                            ("128974848", 128974848), ("0", 0), (None, 0), ("500M", 500 * 10 ** 6),
                                            ("1500m", 1.5), ("2k", 2000)])
def test_quantities(text, expected):
    assert room.quantity(text) == expected


def test_a_pods_request_is_what_the_scheduler_adds_up():
    spec = {"containers": [{"resources": {"requests": {"memory": "32Mi"}}}, {"resources": {"requests": {"memory": "16Mi"}}}, {}]}
    assert room.request_of(spec) == 48 * MIB
    assert room.request_of({**spec, "initContainers": [{"resources": {"requests": {"memory": "64Mi"}}}]}) == 64 * MIB
    assert room.request_of({**spec, "overhead": {"memory": "8Mi"}}) == 56 * MIB


def test_only_pods_placed_and_not_finished_count():
    pods = [pod("staging", "a", 48), pod("staging", "b", 48, phase="Succeeded"), pod("staging", "c", 48, phase="Failed"),
            pod("staging", "d", 48, phase="Pending", node=None), pod("staging", "e", 48, phase="Pending")]
    assert [item["metadata"]["name"] for item in room.on_the_node(pods)] == ["a", "e"]


def test_the_reading_on_a_full_node():
    reading = room.read(FakeNode().get)
    assert int(reading.allocatable // MIB) == 1180
    assert reading.reserved == 1158 * MIB
    assert reading.used == 256 * MIB
    assert reading.forecast_reserved == 768 * MIB
    assert int(reading.free // MIB) == 22
    assert reading.ask == 48 * MIB
    assert not reading.fits
    assert (reading.reserved_percent, reading.used_percent) == (98, 22)


def test_the_percents_round_as_they_should():
    """Reserved as kubectl describe node prints it, rounded down. Used rounded up, so it never flatters."""
    reading = room.Reading(allocatable=1000 * MIB, reserved=989.9 * MIB, used=200.1 * MIB, forecast_reserved=0, forecast_used=0, ask=0)
    assert reading.reserved_percent == 98
    assert reading.used_percent == 21


def test_used_is_the_pods_cgroup_and_falls_back_on_the_sum_of_the_pods():
    assert room.used_by_pods(FakeNode(used_mib=300).summary()) == 300 * MIB
    summary = FakeNode().summary()
    del summary["node"]["systemContainers"][1]
    assert room.used_by_pods(summary) == 51 * MIB // 3 * 3 + 20 * MIB


def test_a_pending_pod_doesnt_count_as_reserved():
    reading = room.read(FakeNode(pending=PENDING, said=SAID).get)
    assert reading.reserved == 1158 * MIB
    assert (reading.pending, reading.said) == (PENDING, SAID)


def test_the_scheduler_is_only_quoted_while_its_pod_waits():
    node = FakeNode(pending=PENDING, said=SAID)
    node.pods = [item for item in node.pods if item["metadata"]["name"] != PENDING]
    reading = room.read(node.get)
    assert (reading.pending, reading.said) == ("", "")


def test_a_pod_pending_in_another_namespace_isnt_the_hotfix():
    node = FakeNode()
    waiting = pod("forecast", "forecast-new", 48, phase="Pending", node=None)
    node.pods.append(waiting)
    node.events.append({"metadata": {"creationTimestamp": "2026-11-20T10:00:05Z"},
                        "involvedObject": {"name": "forecast-new"}, "message": SAID})
    assert room.read(node.get).pending == ""


def test_the_newest_of_the_schedulers_words_wins():
    node = FakeNode(pending=PENDING, said="old words")
    node.events.append({"metadata": {"creationTimestamp": "2026-11-20T10:01:00Z"}, "involvedObject": {"name": PENDING}, "message": SAID})
    assert room.read(node.get).said == SAID


def test_the_schedulers_words_are_unchanged_but_for_line_breaks():
    lines = room.wrapped(SAID)
    assert " ".join(lines) == SAID
    assert lines[:2] == ["0/1 nodes are available:", "1 Insufficient memory."]
    assert all(len(line) <= room.WIDTH for line in lines)


def test_the_window_as_the_hook_films_it():
    lines = room.frame(room.read(FakeNode(pending=PENDING, said=SAID).get))
    assert lines[0] == f"Scheduler on {PENDING}:"
    assert lines[1:3] == ["0/1 nodes are available:", "1 Insufficient memory."]
    text = "\n".join(lines)
    assert "Reserved                   98%    1,158 MiB" in text
    assert "Used                       22%      256 MiB" in text
    assert "forecast reserved                   768 MiB" in text
    assert "forecast uses                        51 MiB" in text
    assert "Free to reserve                      22 MiB" in text
    assert "catalog's next pod asks for          48 MiB" in text
    assert all(len(line) <= room.WIDTH for line in lines)
    assert len(lines) <= 20


def test_the_figures_keep_their_rows_whether_or_not_a_pod_waits():
    waiting, clear = room.frame(room.read(FakeNode(pending=PENDING, said=SAID).get)), room.frame(room.read(FakeNode().get))
    assert clear[0] == "Scheduler: nothing waiting."
    assert waiting.index(next(line for line in waiting if line.startswith("Reserved"))) == \
        clear.index(next(line for line in clear if line.startswith("Reserved")))


def test_after_the_memory_is_given_back():
    reading = room.read(FakeNode(forecast_mib=48).get)
    assert reading.fits
    assert (int(reading.reserved // MIB), reading.reserved_percent, int(reading.free // MIB)) == (534, 45, 646)


def test_used_is_rounded_up_and_free_down():
    reading = room.Reading(allocatable=1180.4 * MIB, reserved=1158 * MIB, used=255.1 * MIB, forecast_reserved=768 * MIB,
                           forecast_used=50.2 * MIB, ask=48 * MIB)
    text = "\n".join(room.frame(reading))
    assert "256 MiB" in text and "51 MiB" in text and "22 MiB" in text and "1,180 MiB" in text


def test_the_window_keeps_going_when_a_read_fails(capsys, monkeypatch):
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    def get(*args):
        if not calls:
            raise RuntimeError("Unable to connect to the server: the tunnel closed")
        return FakeNode().get(*args)

    monkeypatch.setattr(room.time, "sleep", sleep)
    room.watch(get)
    written = capsys.readouterr().out
    assert "No reading: Unable to connect" in written
    assert "98%" in written
    assert written.endswith("\033[?25h\n")  # the cursor back


def test_kubectl_failure_says_kubectls_words(monkeypatch):
    class Failed:
        returncode, stdout, stderr = 1, "", 'Error from server (Forbidden): nodes "tools-host" is forbidden\nmore'

    monkeypatch.setattr(room.subprocess, "run", lambda *args, **kwargs: Failed())
    with pytest.raises(RuntimeError, match="Forbidden"):
        room.kubectl_json("get", "node", "tools-host")


def test_it_prints_no_secret_and_needs_no_token_of_its_own():
    source = (room.Path(room.__file__)).read_text()
    assert "get secret" not in source and "token" not in source.lower()
