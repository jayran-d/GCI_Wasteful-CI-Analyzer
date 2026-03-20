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
