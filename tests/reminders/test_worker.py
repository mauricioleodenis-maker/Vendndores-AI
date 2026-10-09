import app.worker as worker
from app.core.jobs import JOB_REGISTRY


def test_worker_lists_all_registered_jobs():
    worker.load_job_modules()
    for expected in (
        "reminders.enqueue_due",
        "reminders.send",
        "privacy.purge_retention",
        "channels.process_inbound",
        "conversation.summarize",
    ):
        assert expected in JOB_REGISTRY


def test_worker_crons_unique_and_include_reminders():
    crons = worker.load_job_modules()
    names = [c.name for c in crons]
    assert len(names) == len(set(names))
    assert "reminders.enqueue_due" in names and "privacy.purge_retention" in names


def test_missing_expected_crons_reports_gaps():
    class C:
        def __init__(self, name):
            self.name = name

    missing = worker.missing_expected_crons([C("reminders.enqueue_due")])
    assert "reminders.enqueue_due" not in missing and "outreach.dispatch" in missing
    assert worker.missing_expected_crons([C(n) for n in worker.EXPECTED_CRON_NAMES]) == []


def test_dedupe_crons_keeps_first():
    class C:
        def __init__(self, name, tag):
            self.name, self.tag = name, tag

    out = worker._dedupe_crons([C("a", 1), C("a", 2), C("b", 3)])
    assert [(c.name, c.tag) for c in out] == [("a", 1), ("b", 3)]
