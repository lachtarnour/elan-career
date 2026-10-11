"""New operations use saved options; old page lookups cannot replace new intent."""

from copy import deepcopy

import pytest

from smartapply import config
from smartapply.desktop.llm_settings import save_application_settings
from tests.test_desktop_task_lifecycle import bridge as bridge
from tests.test_llm_settings import llm_env as llm_env


def test_saved_runtime_options_reach_new_consumers_without_changing_old_snapshots(
    llm_env, monkeypatch,
):
    from smartapply.dedup.deduplicator import Deduplicator
    from smartapply.llm.fit_context import build_fit_context, context_is_current
    from smartapply.llm_policy import resolve_route
    from smartapply.pipeline.application_renderer import ApplicationDocumentRenderer
    from smartapply.pipeline.processor import Processor
    from smartapply.profile import get_profile

    updates = {
        "DECLARED_EXPERIENCE": "Updated experience",
        "LLM_MAX_CONCURRENT": "1",
        "DEDUP_TITLE_THRESHOLD": "99",
        "CV_MERGE_SPARSE_SECONDARY_SKILLS": "false",
    }
    for key in updates:
        monkeypatch.delenv(key, raising=False)
    llm_env.write_text(
        llm_env.read_text() + "DECLARED_EXPERIENCE=Original experience\n"
        "LLM_MAX_CONCURRENT=5\nDEDUP_TITLE_THRESHOLD=85\n"
        "CV_MERGE_SPARSE_SECONDARY_SKILLS=true\n",
        encoding="utf-8",
    )
    config.get_settings.cache_clear()
    old = config.get_settings()
    profile = get_profile()
    old_processor = Processor(profile=profile, job_filter=None, llm=None)
    old_deduplicator = Deduplicator()
    old_renderer = ApplicationDocumentRenderer(profile)
    old_llm = config.get_llm_settings()
    route = resolve_route(old_llm, "job_fit_assessment")
    old_context = build_fit_context(
        profile, route.model, provider=route.provider, settings=old_llm,
    )
    frozen_context = deepcopy(old_context)

    saved = save_application_settings(updates)

    current = config.get_settings()
    assert current is config.get_settings() and current is not old
    assert {key: getattr(current, key) for key in config._PINNED_RUNTIME_FIELDS} == {
        key: getattr(old, key) for key in config._PINNED_RUNTIME_FIELDS
    }
    shown = {field["key"]: field["value"] for field in saved["app_fields"]}
    assert shown["LLM_MAX_CONCURRENT"] == "1"
    assert Processor(profile=profile, job_filter=None, llm=None).settings.llm_max_concurrent == 1
    assert Deduplicator().title_threshold == 99
    assert not ApplicationDocumentRenderer(profile).settings.cv_merge_sparse_secondary_skills
    assert old_processor.settings.llm_max_concurrent == 5
    assert old_deduplicator.title_threshold == 85
    assert old_renderer.settings.cv_merge_sparse_secondary_skills
    assert old.declared_experience == old_llm.declared_experience == "Original experience"
    assert config.get_llm_settings().declared_experience == "Updated experience"
    assert config.get_source_settings().declared_experience == "Updated experience"
    assert build_fit_context(
        profile, route.model, provider=route.provider, settings=old_llm,
    ) == frozen_context
    assert build_fit_context(
        profile, route.model, provider=route.provider, settings=config.get_llm_settings(),
    )["declared_experience"] == "Updated experience"
    assert old_context == frozen_context
    # An old signed context remains immutable, but changed candidate facts
    # must still make it inadmissible for publication.
    assert not context_is_current(old_context, profile)


def test_saved_paths_wait_for_an_explicit_runtime_reset(llm_env, monkeypatch, tmp_path):
    paths = {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'active' / 'db.sqlite'}",
        "PROFILE_DIR": str(tmp_path / "active" / "profile"),
        "OUTPUT_DIR": str(tmp_path / "active" / "output"),
        "CACHE_DIR": str(tmp_path / "active" / "cache"),
    }
    for key in paths:
        monkeypatch.delenv(key, raising=False)
    llm_env.write_text("".join(f"{key}={value}\n" for key, value in paths.items()), encoding="utf-8")
    config.get_settings.cache_clear()
    old = config.get_settings()
    changed = {key: value.replace("active", "next") for key, value in paths.items()}

    saved = save_application_settings(changed | {"DEDUP_TITLE_THRESHOLD": "99"})

    current = config.get_settings()
    for key in paths:
        assert getattr(current, key.lower()) == getattr(old, key.lower())
    assert current.dedup_title_threshold == 99
    shown = {field["key"]: field["value"] for field in saved["app_fields"]}
    assert shown["OUTPUT_DIR"] == changed["OUTPUT_DIR"]
    assert not (tmp_path / "next").exists()
    config.get_settings.cache_clear()
    restarted = config.get_settings()
    for key, value in changed.items():
        assert str(getattr(restarted, key.lower())) == value
    assert old.database_url == paths["DATABASE_URL"]


def test_invalid_settings_save_keeps_runtime_snapshot(llm_env):
    old = config.get_settings()
    persisted = llm_env.read_bytes()

    with pytest.raises(ValueError, match="LLM_MAX_CONCURRENT"):
        save_application_settings({"LLM_MAX_CONCURRENT": "0"})

    assert config.get_settings() is old and llm_env.read_bytes() == persisted


def _finish_read(bridge, key):
    worker = bridge._reads_active[key]
    bridge._read_pool.pending.remove(worker)
    worker.run()


def _page_bridge(bridge):
    pages = []

    def page(**kwargs):
        pages.append((kwargs["search"], kwargs["sort_key"], kwargs["page"]))
        job_id = 99 if kwargs["sort_key"] == "title" or kwargs["page"] == 2 else 1
        return {"rows": [{"id": job_id, "title": str(job_id)}], "total": 300, "page": kwargs["page"]}

    bridge.service.list_jobs_page = page
    bridge.service.get_job = lambda job: {"id": job}
    bridge.service.job_page_for = lambda job, **kwargs: 2
    bridge._active_route = "jobs"
    bridge._selected_job_id = 1
    bridge.loadJobs("", "analyzed", "llm_score", False, "", 0)
    _finish_read(bridge, "jobs")
    bridge.selectJob(99)
    return pages


@pytest.mark.parametrize(
    "next_query",
    [
        ("", "analyzed", "title", True, "", 0),
        ("Python", "analyzed", "llm_score", False, "", 0),
        ("", "sent", "llm_score", False, "", 0),
        ("", "analyzed", "llm_score", False, "", 1),
        ("", "analyzed", "llm_score", False, "", 0, "", "core"),
    ],
)
def test_old_locator_cannot_replace_new_filter_sort_or_page(bridge, next_query):
    pages = _page_bridge(bridge)
    old_locator = bridge._reads_active["locate_job"]

    bridge.loadJobs(*next_query)
    _finish_read(bridge, "jobs")
    bridge._read_pool.pending.remove(old_locator)
    old_locator.run()
    _finish_read(bridge, "job_detail")

    assert bridge.jobsPage == next_query[5]
    assert bridge.currentJob["id"] == 99
    assert len(pages) == 2 and not bridge._read_pool.pending


def test_passive_refresh_keeps_valid_dossier_locator(bridge):
    pages = _page_bridge(bridge)

    bridge.refreshJobs()
    _finish_read(bridge, "jobs")
    _finish_read(bridge, "locate_job")
    _finish_read(bridge, "jobs")
    _finish_read(bridge, "job_detail")

    assert bridge.jobsPage == 2 and bridge.jobs[0]["id"] == 99
    assert pages == [("", "llm_score", 0), ("", "llm_score", 0), ("", "llm_score", 2)]


def test_locator_does_not_move_a_dossier_that_refresh_already_made_visible(bridge):
    pages = _page_bridge(bridge)
    bridge.service.list_jobs_page = lambda **kwargs: {
        "rows": [{"id": 99, "title": "now on this page"}], "total": 300, "page": kwargs["page"],
    }

    bridge.refreshJobs()
    _finish_read(bridge, "jobs")
    _finish_read(bridge, "locate_job")
    _finish_read(bridge, "job_detail")

    assert bridge.jobsPage == 0 and bridge.jobs[0]["id"] == 99
    assert len(pages) == 1 and not bridge._read_pool.pending
