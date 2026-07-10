from trawler.fetcher import hitl_rung


def test_hitl_has_display():
    # hitl_rung.has_display() returns bool
    res = hitl_rung.has_display()
    assert isinstance(res, bool)
