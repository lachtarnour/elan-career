"""Provider health is checked on demand and reused until expired or reconfigured."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from smartapply.config import get_source_settings
from smartapply.desktop.services import DesktopService
from smartapply.desktop.source_health import SourceHealth


def test_health_cache_expires_and_invalidates_on_configuration_changes(monkeypatch):
    import smartapply.desktop.services as services

    now = [0.0]
    settings = [get_source_settings()]
    checks = []
    result = {"serpapi": SourceHealth(True, True, "ready", "Ready")}
    monkeypatch.setattr(services, "monotonic", lambda: now[0])
    monkeypatch.setattr(services, "get_source_settings", lambda: settings[0])
    monkeypatch.setattr(
        services, "check_source_health", lambda value: checks.append(value) or result
    )
    service = DesktopService()
    service.diagnostics()
    assert checks == []
    assert service.source_diagnostics_stale()
    assert service.diagnostics(check_sources=True).source_health == result
    service.diagnostics(check_sources=True)
    assert len(checks) == 1
    assert not service.source_diagnostics_stale()
    now[0] = 300.0
    assert service.source_diagnostics_stale()
    service.diagnostics(check_sources=True)
    assert len(checks) == 2
    settings[0] = settings[0].model_copy(update={"serpapi_api_key": "changed"})
    assert service.source_diagnostics_stale()
    service.diagnostics(check_sources=True)
    assert len(checks) == 3
    service.invalidate_diagnostics_cache()
    service.diagnostics(check_sources=True)
    assert len(checks) == 4


@pytest.mark.parametrize("change", ["invalidate", "configuration"])
def test_cache_inspection_and_invalidation_do_not_wait_for_a_provider(monkeypatch, change):
    entered, release, inspected = Event(), Event(), Event()
    service = DesktopService()
    settings = [get_source_settings()]

    def blocked_check(settings):
        entered.set()
        assert release.wait(10)
        return {"serpapi": SourceHealth(True, True, "ready", "Ready")}

    def inspect_cache():
        if change == "configuration":
            settings[0] = settings[0].model_copy(update={"serpapi_api_key": "changed"})
        assert service.source_diagnostics_stale()
        if change == "invalidate":
            service.invalidate_diagnostics_cache()
        inspected.set()

    monkeypatch.setattr("smartapply.desktop.services.get_source_settings", lambda: settings[0])
    monkeypatch.setattr("smartapply.desktop.services.check_source_health", blocked_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        check = pool.submit(service.diagnostics, check_sources=True)
        assert entered.wait(10)
        inspection = pool.submit(inspect_cache)
        try:
            assert inspected.wait(2), "Cache-only actions waited for the network request"
        finally:
            release.set()
        inspection.result(timeout=10)
        result = check.result(timeout=10)
    assert not result.source_health["serpapi"].ready
    assert service.source_diagnostics_stale()
