import reset


def test_later_versions_are_the_ones_a_take_releases():
    assert reset.later_versions() == ["2.1.0", "2.1.1", "2.1.2"]


def test_the_baseline_package_is_the_same_every_time():
    assert reset.baseline_package() == reset.baseline_package()
