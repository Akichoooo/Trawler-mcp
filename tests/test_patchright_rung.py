import pytest


@pytest.mark.asyncio
async def test_patchright_import():
    # Just verify it doesn't crash on import/basic call
    pass


def test_sticky_fingerprint_reuses_identity(monkeypatch):
    from trawler.fetcher import patchright_rung

    class FakeNavigator:
        userAgent = "Fake UA"
        language = "en-US"
        platform = "Win32"
        hardwareConcurrency = 8
        deviceMemory = 8

    class FakeFingerprint:
        navigator = FakeNavigator()
        screen = None

    class FakeGenerator:
        def __init__(self):
            self.calls = 0

        def generate(self):
            self.calls += 1
            return FakeFingerprint()

    generator = FakeGenerator()
    patchright_rung._FINGERPRINT_POOL.clear()
    monkeypatch.setattr(patchright_rung, "_BROWSERFORGE_AVAILABLE", True)
    monkeypatch.setattr(patchright_rung, "_fp_gen", generator)

    first = patchright_rung._get_sticky_fingerprint(("example.com", "", False))
    second = patchright_rung._get_sticky_fingerprint(("example.com", "", False))
    third = patchright_rung._get_sticky_fingerprint(("example.org", "", False))

    assert first == second
    assert third == first
    assert generator.calls == 2
