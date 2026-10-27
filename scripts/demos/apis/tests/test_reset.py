import reset


def test_later_versions_are_the_ones_a_take_releases():
    assert reset.later_versions() == ["2.1.0", "2.1.1", "2.1.2"]


def test_the_baseline_package_is_the_same_every_time():
    assert reset.baseline_package() == reset.baseline_package()


def test_the_main_reset_steps_point_at_pricing_api():
    shared = reset.main_reset()
    assert (shared.SERVICE, shared.BASELINE, shared.DEMO_ISSUES) == ("pricing-api", "2.0.4", ["ORD-52"])
    assert shared.after_baseline("2.1.2") and not shared.after_baseline("2.0.4")
    assert not shared.after_baseline("takes")


def test_a_dry_run_changes_nothing(capsys):
    done = []
    dry = reset.main_reset().Reset(dry_run=True)
    dry.act("archive a tag", lambda: done.append(1))
    assert done == [] and dry.actions == 1
    assert "would archive a tag" in capsys.readouterr().out
