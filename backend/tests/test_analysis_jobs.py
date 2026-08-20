import time

from app.analysis_jobs import get_analysis_job, start_analysis_job


def test_analysis_job_completes_successfully():
    job = start_analysis_job(
        params={"source_filename": "fall.pdf"},
        message="Analyse wurde gestartet.",
        target=lambda: {"analysis_id": "analysis-1"},
    )

    for _ in range(20):
        current = get_analysis_job(job["id"])
        if current["status"] == "succeeded":
            break
        time.sleep(0.01)

    assert current["status"] == "succeeded"
    assert current["result"] == {"analysis_id": "analysis-1"}
