"""
Analyzer 1: Non-deterministic Test Flakiness.

Detects waste caused by rerun chains showing outcome transitions from failure to success (F→S).
This indicates non-deterministic test/environment behavior. Flags failed rerun attempts that
eventually resolved, evidencing flakiness.

Approach:
  - Group by rerun chain: (workflow_id, workflow_name, run_number, head_sha)
  - Detect F→S transitions: chain must contain failures AND end in success
  - Flag failed rerun attempts (run_attempt > 1) that came before eventual success
  - Exclude scheduled/automatic reruns and action_required chains
"""

from collections import defaultdict
from energy import aggregate_estimates, detect_runner_type, estimate_energy
from utils import run_duration


class FlakinessAnalyzer:
    key = "flakiness"
    title = "Non-deterministic Test Flakiness"
    description = (
        "Detects flaky builds via outcome transitions (F→S): flakiness proven by failed reruns "
        "followed by eventual success on same code. Flags wasted rerun attempts."
    )

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, progress_cb=None, all_events=False):
        """
        Detect flaky reruns via outcome transitions (F→S).
        
        - Group by rerun chain: (workflow_id, workflow_name, run_number, head_sha)
        - Detect F→S transitions: chain must contain FAILURES and END in SUCCESS
        - Flag: all failed rerun attempts (run_attempt > 1) before eventual success
        - These failed reruns are evidence of flakiness (non-deterministic resolution)
        - Exclude: chains ending in failure (pure bugs), action_required chains, scheduled reruns
        """
        if progress_cb:
            progress_cb(f"Analyzing flakiness in {len(runs)} runs...")

        # A rerun chain is one logical workflow execution retried multiple times.
        grouped = self._group_by_rerun_chain(runs)
        if progress_cb:
            progress_cb(f"Grouped into {len(grouped)} rerun-chain buckets")

        # Repo-wide diagnostics help manual validation of what data was seen.
        rerun_runs_all = [r for r in runs if (r.get("run_attempt") or 1) > 1]
        rerun_workflow_ids = sorted({r.get("workflow_id") for r in rerun_runs_all if r.get("workflow_id") is not None})

        flagged_runs = []
        details = []

        for (workflow_id, workflow_name, run_number, sha), group in grouped.items():
            sorted_group = sorted(group, key=lambda r: r.get("run_attempt") or 1)
            conclusions = [r.get("conclusion") for r in sorted_group]
            has_failure = "failure" in conclusions
            final_conclusion = sorted_group[-1].get("conclusion") if sorted_group else None

            # Paper-aligned signal: failure(s) followed by eventual success (F->S).
            if not (has_failure and final_conclusion == "success"):
                continue

            # Approval-gated chains are not test/environment flakiness.
            if "action_required" in conclusions:
                continue

            # Waste is the failed rerun attempts before the chain resolves.
            for run in sorted_group:
                run_attempt = run.get("run_attempt") or 1

                if run_attempt <= 1:
                    continue

                if run.get("conclusion") != "failure":
                    continue

                # Keep manual reruns; scheduled runs belong to scheduled-workflow analysis.
                if run.get("event") == "schedule":
                    continue

                flagged_runs.append(run)
                details.append({
                    "run_id": run["id"],
                    "run_number": run_number,
                    "workflow_id": workflow_id,
                    "workflow": workflow_name,
                    "sha": sha[:8] if sha else "?",
                    "event": run.get("event"),
                    "created_at": run.get("created_at"),
                    "run_attempt": run_attempt,
                    "reason": f"Rerun attempt #{run_attempt} failed; chain eventually succeeded (F→S transition = flakiness waste)",
                })

        # Estimate energy/carbon only for flagged waste attempts.
        energy_estimates = []
        for run in flagged_runs:
            dur = run_duration(run)
            if dur > 0:
                energy_estimates.append(
                    estimate_energy(dur, detect_runner_type(run.get("labels")))
                )

        total_failed_runs = sum(1 for r in runs if r.get("conclusion") == "failure")
        total_unique_shas = len({r.get("head_sha") for r in runs if r.get("head_sha")})
        flaky_unique_shas = len({r.get("head_sha") for r in flagged_runs if r.get("head_sha")})

        if progress_cb:
            progress_cb(
                f"Flakiness detection complete: {len(flagged_runs)} flaky rerun attempts in "
                f"{flaky_unique_shas} unique commits with F->S transitions"
            )

        return {
            "analyzer": self.key,
            "title": self.title,
            "summary": {
                "total_runs_analyzed": len(runs),
                "total_failed_runs": total_failed_runs,
                "flaky_rerun_attempts": len(flagged_runs),
                "flaky_unique_shas": flaky_unique_shas,
                "flakiness_rate_of_failures": self._pct(len(flagged_runs), total_failed_runs),
                "flakiness_rate_of_commits": self._pct(flaky_unique_shas, total_unique_shas),
            },
            "energy_waste": aggregate_estimates(energy_estimates),
            "flaky_runs": {
                "count": len(flagged_runs),
                "detail": details[:50],
            },
            "rerun_diagnostics": {
                "total_rerun_runs": len(rerun_runs_all),
                "total_rerun_workflows": len(rerun_workflow_ids),
                "rerun_run_ids_first10": [
                    r.get("id")
                    for r in rerun_runs_all[:10]
                    if r.get("id") is not None
                ],
                "rerun_runs_first10": [
                    {
                        "run_id": r.get("id"),
                        "run_number": r.get("run_number"),
                        "workflow_id": r.get("workflow_id"),
                        "workflow": r.get("name") or "",
                        "run_attempt": r.get("run_attempt") or 1,
                        "conclusion": r.get("conclusion"),
                        "event": r.get("event"),
                        "created_at": r.get("created_at"),
                    }
                    for r in rerun_runs_all[:10]
                    if r.get("id") is not None
                ],
            },
            "recommendations": self._build_recommendations(len(flagged_runs), total_failed_runs),
        }


    def _group_by_rerun_chain(self, runs):
        """Group runs by rerun chain: (workflow_id, workflow_name, run_number, head_sha)."""
        grouped = defaultdict(list)
        for run in runs:
            sha = run.get("head_sha")
            workflow_id = run.get("workflow_id")
            run_number = run.get("run_number")
            if not sha or workflow_id is None or run_number is None:
                continue
            workflow_name = run.get("name") or ""
            grouped[(workflow_id, workflow_name, run_number, sha)].append(run)
        return grouped

    @staticmethod
    def _pct(part, total):
        if not total:
            return 0.0
        return round((part / total) * 100, 1)

    @staticmethod
    def _build_recommendations(flaky_count, total_failures):
        if flaky_count == 0:
            return [
                "No flaky builds detected (no F→S transitions). CI appears stable."
            ]

        recommendations = [
            "Quarantine and fix top flaky test cases—they require multiple reruns to pass.",
            "Add deterministic seeding and clock-stable timeouts to reduce environment-dependent test failures.",
            "Use per-job retries (rather than full workflow reruns) to reduce wasted compute on flaky tests.",
        ]

        flakiness_pct = (flaky_count / total_failures * 100) if total_failures > 0 else 0
        if flakiness_pct > 20:
            recommendations.insert(
                0,
                f"URGENT: {flakiness_pct:.1f}% of failures are flaky (F→S transitions). "
                f"This causes significant resource waste. Prioritize stabilization of test suite."
            )

        return recommendations

