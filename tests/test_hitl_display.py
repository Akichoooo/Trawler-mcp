import types


def test_windows_display_rejects_services_session(monkeypatch):
    from trawler.fetcher import hitl_rung

    monkeypatch.setattr(hitl_rung.sys, "platform", "win32")
    monkeypatch.setenv("SESSIONNAME", "Services")

    assert hitl_rung.has_display() is False


def test_windows_display_detects_explorer_when_sessionname_missing(monkeypatch):
    import sys

    from trawler.fetcher import hitl_rung

    class FakeUser32:
        def OpenInputDesktop(self, flags, inherit, access):
            return 0

        def CloseDesktop(self, desktop):
            return 1

    class FakePsutil:
        @staticmethod
        def process_iter(attrs):
            return [
                types.SimpleNamespace(
                    info={"name": "explorer.exe", "username": "DESKTOP\\JY"}
                )
            ]

    monkeypatch.setattr(hitl_rung.sys, "platform", "win32")
    monkeypatch.delenv("SESSIONNAME", raising=False)
    monkeypatch.setenv("USERNAME", "JY")
    monkeypatch.setitem(
        sys.modules,
        "ctypes",
        types.SimpleNamespace(windll=types.SimpleNamespace(user32=FakeUser32())),
    )
    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)

    assert hitl_rung.has_display() is True
