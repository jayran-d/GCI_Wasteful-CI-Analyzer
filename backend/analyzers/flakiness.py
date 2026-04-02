"""
Analyzer 1: Non-deterministic Test Flakiness.

Detects waste by finding the same job producing different outcomes across attempts or runs
on the same commit.

KEY INSIGHT: GitHub reruns do NOT create a new run_id. They create a new ATTEMPT on the
same run. The API listing returns only the latest attempt. Previous attempts are hidden
behind /runs/{id}/attempts/{n}/jobs.

So run #7 with run_attempt=2 means:
  - Attempt 1 existed (maybe passed, maybe failed) — invisible in the runs list
  - Attempt 2 is what the API shows

To detect flakiness, we must:
  1. Find runs where run_attempt > 1 (reruns happened)
  2. Fetch jobs for EVERY attempt (1 through N)
  3. Compare job outcomes across attempts WITHIN the same run_id
  4. Same job, different outcomes on same run = flaky

We intentionally only compare within the same run_id. Cross-run comparisons on the
same commit produce false positives because PR state (labels, assignees, review status)
can change between separate workflow triggers — those are state-dependent outcomes,
not non-deterministic flakiness.
"""

from collections import defaultdict
from energy import aggregate_estimates, detect_runner_type, estimate_energy
from utils import run_duration


class FlakinessAnalyzer:
    key = "flakiness"
    title = "Non-deterministic Job Flakiness"
    description = (
        "Detects flaky jobs by finding different outcomes for the same job "
        "across rerun attempts within the same workflow run."
    )

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, progress_cb=None, all_events=False):
        if progress_cb:
            progress_cb(f"Analyzing flakiness in {len(runs)} runs...")

        run_url_base = f"https://github.com/{owner}/{repo}/actions/runs"

        # ── Step 1: Identify runs that have multiple attempts ───────
        #
        # Only runs with run_attempt > 1 are interesting — they have
        # hidden earlier attempts we need to fetch and compare.
        # We no longer compare across different run_ids on the same
        # commit, since that captures state-dependent changes (e.g.
        # PR labels added between runs), not true flakiness.

        runs_by_sha = defaultdict(list)
        rerun_runs = []  # runs where run_attempt > 1

        for run in runs:
            sha = run.get("head_sha")
            if not sha:
                continue
            if run.get("event") == "schedule" and not all_events:
                continue
            runs_by_sha[sha].append(run)
            if (run.get("run_attempt") or 1) > 1:
                rerun_runs.append(run)

        if progress_cb:
            progress_cb(
                f"Found {len(rerun_runs)} rerun(s) to inspect for flakiness"
            )

        if not rerun_runs:
            return self._empty_result(runs)

        # ── Step 2: Fetch jobs for all attempts of rerun runs ───────
        #
        # For a run with run_attempt=3, we fetch:
        #   /runs/{id}/attempts/1/jobs
        #   /runs/{id}/attempts/2/jobs
        #   /runs/{id}/attempts/3/jobs
        #
        # Group by (run_id, job_name) so we only compare attempts
        # within the same run — not across different runs.

        job_groups = defaultdict(list)  # (run_id, job_name) → [job_info, ...]
        api_calls = 0

        for run in rerun_runs:
            run_id = run.get("id")
            if not run_id:
                continue

            sha = run["head_sha"]
            max_attempt = run.get("run_attempt") or 1

            for attempt_num in range(1, max_attempt + 1):
                jobs = self._fetch_jobs_for_attempt(
                    owner, repo, run_id, attempt_num
                )
                api_calls += 1

                for job in jobs:
                    job_name = job.get("name") or ""
                    conclusion = job.get("conclusion")
                    if not conclusion:
                        continue

                    job_groups[(run_id, job_name)].append({
                        "run_id": run_id,
                        "run_number": run.get("run_number"),
                        "run_attempt": attempt_num,
                        "max_attempt": max_attempt,
                        "workflow_id": run.get("workflow_id"),
                        "workflow": run.get("name") or "",
                        "event": run.get("event"),
                        "created_at": (
                            job.get("started_at")
                            or run.get("created_at")
                        ),
                        "conclusion": conclusion,
                        "job_name": job_name,
                        "job_id": job.get("id"),
                        "duration_seconds": self._job_duration(job),
                        "labels": job.get("labels") or run.get("labels"),
                        "head_sha": sha,
                    })

            if progress_cb and api_calls % 5 == 0:
                progress_cb(f"Fetched jobs from {api_calls} API calls...")

        if progress_cb:
            progress_cb(
                f"Fetched jobs via {api_calls} API calls, "
                f"grouped into {len(job_groups)} (run, job) pairs"
            )

        # ── Step 3: Find flaky groups ───────────────────────────────
        #
        # A group (run_id, job_name) is flaky if it contains BOTH
        # "failure" and "success" conclusions across its attempts.
        # Since all attempts share the same run_id (same commit,
        # same trigger, same PR state), differing outcomes indicate
        # genuine non-determinism.

        flagged_jobs = []
        flaky_job_names = defaultdict(int)
        flaky_details = []

        for (run_id, job_name), job_list in job_groups.items():
            conclusions = {j["conclusion"] for j in job_list}

            if not ("failure" in conclusions and "success" in conclusions):
                continue

            # Approval gates are not flakiness.
            if "action_required" in conclusions:
                continue

            # Build the transition string once for this group.
            sorted_attempts = sorted(job_list, key=lambda j: j["run_attempt"])
            transition = " → ".join(
                "F" if j["conclusion"] == "failure" else "S"
                for j in sorted_attempts
            )

            # Flag every failed instance as waste.
            for job in job_list:
                if job["conclusion"] != "failure":
                    continue

                flagged_jobs.append(job)
                flaky_job_names[job_name] += 1

                run_url = f"{run_url_base}/{job['run_id']}/attempts/{job['run_attempt']}"
                if progress_cb:
                    progress_cb(
                        f"Flaky: '{job_name}' attempt {job['run_attempt']} "
                        f"({transition}) {run_url}"
                    )

                flaky_details.append({
                    "run_id": job["run_id"],
                    "run_number": job["run_number"],
                    "run_attempt": job["run_attempt"],
                    "run_url": run_url,
                    "workflow_id": job["workflow_id"],
                    "workflow": job["workflow"],
                    "job_name": job_name,
                    "job_id": job["job_id"],
                    "sha": job["head_sha"][:8],
                    "event": job["event"],
                    "created_at": job["created_at"],
                    "duration_seconds": job["duration_seconds"],
                    "transition": transition,
                    "reason": (
                        f"Job '{job_name}' on commit {job['head_sha'][:8]}: "
                        f"attempt {job['run_attempt']} failed, but another "
                        f"attempt of the same run succeeded ({transition})"
                    ),
                })

        # ── Step 4: Energy estimates ────────────────────────────────
        energy_estimates = []
        for job in flagged_jobs:
            dur = job.get("duration_seconds") or 0
            if dur > 0:
                energy_estimates.append(
                    estimate_energy(dur, detect_runner_type(job.get("labels")))
                )

        # ── Step 5: Stats ───────────────────────────────────────────
        total_failed_runs = sum(
            1 for r in runs if r.get("conclusion") == "failure"
        )
        total_unique_shas = len(runs_by_sha)
        flaky_unique_shas = len({j["head_sha"] for j in flagged_jobs})

        top_flaky_jobs = sorted(
            flaky_job_names.items(), key=lambda x: x[1], reverse=True
        )

        if progress_cb:
            progress_cb(
                f"Flakiness detection complete: {len(flaky_job_names)} flaky "
                f"job(s) ({len(flagged_jobs)} failure events across "
                f"{len(top_flaky_jobs)} distinct job names)"
            )

        # ── Step 6: Build result ────────────────────────────────────
        summary = {
            "total_runs_analyzed": len(runs),
            "total_failed_runs": total_failed_runs,
            "rerun_runs_inspected": len(rerun_runs),
            "job_groups_checked": len(job_groups),
            "flaky_job_failures": len(flaky_job_names),
            "flaky_failure_events": len(flagged_jobs),
            "flaky_unique_shas": flaky_unique_shas,
            "flaky_runs_detected": len({j["run_id"] for j in flagged_jobs}),
            "flakiness_rate_of_failures": self._pct(
                len(flaky_job_names), total_failed_runs
            ),
            # "flakiness_rate_of_commits": self._pct(
            #     flaky_unique_shas, total_unique_shas
            # ),
        }

        return {
            "analyzer": self.key,
            "title": self.title,
            "summary": summary,
            "run_ids": sorted({j["run_id"] for j in flagged_jobs}),
            "frontend_summary": self._build_frontend_summary(
                summary, top_flaky_jobs, flaky_details
            ),
            "energy_waste": aggregate_estimates(energy_estimates),
            "flaky_runs": {
                "count": len(flagged_jobs),
                "detail": flaky_details[:50],
            },
            "flaky_jobs": {
                "top": [
                    {"job_name": name, "failure_count": count}
                    for name, count in top_flaky_jobs
                ],
                "all_names": sorted(flaky_job_names.keys()),
            },
            "rerun_diagnostics": {
                "total_rerun_runs": len(rerun_runs),
                "total_job_groups_checked": len(job_groups),
                "total_flaky_groups": sum(
                    1
                    for jl in job_groups.values()
                    if "failure" in {j["conclusion"] for j in jl}
                    and "success" in {j["conclusion"] for j in jl}
                ),
                "api_calls_made": api_calls,
            },
            "recommendations": self._build_recommendations(
                len(flaky_job_names), total_failed_runs, top_flaky_jobs
            ),
        }

    # ─── Empty result (no reruns found) ───────────────────────────────

    def _empty_result(self, runs):
        total_failed = sum(1 for r in runs if r.get("conclusion") == "failure")
        summary = {
            "total_runs_analyzed": len(runs),
            "total_failed_runs": total_failed,
            "rerun_runs_inspected": 0,
            "job_groups_checked": 0,
            "flaky_job_failures": 0,
            "flaky_unique_shas": 0,
            "flaky_runs_detected": 0,
            "flakiness_rate_of_failures": 0.0,
            # "flakiness_rate_of_commits": 0.0,
        }
        return {
            "analyzer": self.key,
            "title": self.title,
            "summary": summary,
            "frontend_summary": {
                "status": "clean",
                "headline": "No reruns detected — nothing to analyze for flakiness",
                "stats": [
                    {"label": "Runs analyzed", "value": len(runs)},
                    {"label": "Reruns found", "value": 0},
                ],
                "flaky_jobs": [],
                "sample_evidence": [],
            },
            "energy_waste": aggregate_estimates([]),
            "flaky_runs": {"count": 0, "detail": []},
            "flaky_jobs": {"top": [], "all_names": []},
            "rerun_diagnostics": {},
            "recommendations": [
                "No reruns detected. Either CI is stable or reruns are not being used."
            ],
        }

    # ─── Job fetching ─────────────────────────────────────────────────

    def _fetch_jobs_for_attempt(self, owner, repo, run_id, attempt):
        """
        Fetch jobs for a specific attempt of a workflow run.
        Always uses the explicit /attempts/{n}/jobs endpoint to avoid
        the filter=latest default on /runs/{id}/jobs.
        """
        try:
            url = (
                f"/repos/{owner}/{repo}/actions/runs/{run_id}"
                f"/attempts/{attempt}/jobs"
            )
            resp = self.client._get_json(url, params={"per_page": 100})
            if resp and "jobs" in resp:
                return resp["jobs"]
        except Exception:
            pass
        return []

    @staticmethod
    def _job_duration(job):
        """Calculate job duration in seconds."""
        started = job.get("started_at")
        completed = job.get("completed_at")
        if not started or not completed:
            return 0
        try:
            from datetime import datetime

            fmt = "%Y-%m-%dT%H:%M:%SZ"
            start_dt = datetime.strptime(started, fmt)
            end_dt = datetime.strptime(completed, fmt)
            return max(0, int((end_dt - start_dt).total_seconds()))
        except Exception:
            return 0

    # ─── Frontend summary ─────────────────────────────────────────────

    def _build_frontend_summary(self, summary, top_flaky_jobs, flaky_details):
        """
        Structured block for the frontend to render directly.

        Returns:
          status: "clean" | "warning" | "critical"
          headline: one-line summary
          stats: [{label, value}, ...] for cards
          flaky_jobs: [{name, failures}, ...] for table
          sample_evidence: [{sha, job, attempt, transition, reason}, ...]
        """
        flaky_count = summary["flaky_job_failures"]
        flakiness_rate = summary["flakiness_rate_of_failures"]

        if flaky_count == 0:
            status = "clean"
            headline = "No flaky tests detected — CI is stable"
        elif flakiness_rate > 20:
            status = "critical"
            headline = (
                f"{flaky_count} flaky job failure(s) — "
                f"{flakiness_rate}% of all failures are non-deterministic"
            )
        else:
            status = "warning"
            headline = (
                f"{flaky_count} flaky job failure(s) — "
                f"same code passes and fails across rerun attempts"
            )

        stats = [
            {"label": "Runs analyzed", "value": summary["total_runs_analyzed"]},
            {"label": "Reruns inspected", "value": summary["rerun_runs_inspected"]},
            {"label": "Job groups checked", "value": summary["job_groups_checked"]},
            {"label": "Flaky job failures", "value": flaky_count},
            {"label": "Flaky runs detected", "value": summary["flaky_runs_detected"]},
            {"label": "Flakiness rate", "value": f"{flakiness_rate}%"},
            {"label": "Commits affected", "value": summary["flaky_unique_shas"]},
        ]

        flaky_jobs_table = [
            {"name": name, "failures": count}
            for name, count in top_flaky_jobs[:10]
        ]

        # Deduplicated evidence samples from flaky_details (which has
        # transition and reason already populated).
        seen = set()
        sample_evidence = []
        for detail in flaky_details[:20]:
            key = (detail["sha"], detail["job_name"])
            if key in seen:
                continue
            seen.add(key)
            sample_evidence.append({
                "sha": detail["sha"],
                "job": detail["job_name"],
                "failed_in_run": detail["run_id"],
                "run_url": detail["run_url"],
                "attempt": detail["run_attempt"],
                "transition": detail["transition"],
                "reason": detail["reason"],
            })
            if len(sample_evidence) >= 5:
                break

        return {
            "status": status,
            "headline": headline,
            "stats": stats,
            "flaky_jobs": flaky_jobs_table,
            "sample_evidence": sample_evidence,
        }

    # ─── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _pct(part, total):
        if not total:
            return 0.0
        return round((part / total) * 100, 1)

    @staticmethod
    def _build_recommendations(flaky_count, total_failures, top_flaky_jobs=None):
        if flaky_count == 0:
            return ["No flaky builds detected. CI appears stable."]

        recs = [
            "Quarantine and fix top flaky test cases — they pass and fail on identical code.",
            "Add deterministic seeding and clock-stable timeouts to reduce environment-dependent failures.",
            "Use per-job retries (rather than full workflow reruns) to limit wasted compute.",
        ]

        flakiness_pct = (
            (flaky_count / total_failures * 100) if total_failures > 0 else 0
        )
        if flakiness_pct > 20:
            recs.insert(
                0,
                f"URGENT: {flakiness_pct:.1f}% of failures are flaky. "
                f"Prioritize test suite stabilization.",
            )

        if top_flaky_jobs:
            worst_name, worst_count = top_flaky_jobs[0]
            recs.append(
                f"Start with job '{worst_name}' — it failed {worst_count} "
                f"time(s) on commits where it also passed."
            )

        return recs

