
import pytest

from trawler import signals


def test_install_handlers():
    # Only run on platforms that support signal handlers (Unix/Linux)
    import platform
    if platform.system() == "Windows":
        pytest.skip("Signals not fully supported on Windows")

    # Install handlers
    signals.install_handlers()
    
    # Just verify they don't crash
    assert True
