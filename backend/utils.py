from datetime import datetime, timezone


def run_duration(run: dict) -> float:
    """Compute run duration in seconds from GitHub API timestamps."""
    started = run.get("run_started_at") or run.get("created_at")
    ended = run.get("updated_at")
    if not started or not ended:
        return 0
    try:
        t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return max(0, (t1 - t0).total_seconds())
    except Exception:
        return 0
def get_run_duration_from_jobs(jobs) -> int:
    """
    Calculate the wall-clock duration (in seconds) of a run by finding
    the earliest job start and latest job completion across all its jobs.
    Falls back to 0 if no valid timestamps are found.
    """
    from datetime import datetime

    def parse_dt(s):
        if not s:
            return None
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    starts      = [parse_dt(j.get("started_at"))  for j in jobs]
    completions = [parse_dt(j.get("completed_at")) for j in jobs]

    valid_starts      = [t for t in starts      if t is not None]
    valid_completions = [t for t in completions  if t is not None]

    if not valid_starts or not valid_completions:
        return 0

    return max(0, int((max(valid_completions) - min(valid_starts)).total_seconds()))