import time

import pytest

from app.admin_jobs import JobAlreadyRunningError, get_job, start_catalog_job


def wait_for_job(job_id: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


def test_catalog_job_runs_in_background():
    job = start_catalog_job(
        kind="test_catalog_job",
        params={"quarter": "2099/Q1"},
        message="starting",
        target=lambda: {"ok": True},
    )

    finished = wait_for_job(job["id"])

    assert finished["status"] == "succeeded"
    assert finished["result"] == {"ok": True}


def test_catalog_job_rejects_parallel_run():
    started = {"value": False}

    def slow_job():
        started["value"] = True
        time.sleep(0.1)
        return {"ok": True}

    job = start_catalog_job(
        kind="slow_catalog_job",
        params={},
        message="starting",
        target=slow_job,
    )

    while not started["value"]:
        time.sleep(0.01)

    with pytest.raises(JobAlreadyRunningError):
        start_catalog_job(
            kind="second_catalog_job",
            params={},
            message="starting",
            target=lambda: {"ok": True},
        )

    assert wait_for_job(job["id"])["status"] == "succeeded"
