"""
Analyzer 1: Non-deterministic Test Flakiness.

Detects waste caused by reruns where the same commit (head_sha) has mixed
outcomes (failure and success) in the same workflow context, or explicit
GitHub rerun attempts that fail. This usually indicates flaky tests,
timeouts, or unstable execution environments rather than code defects.
"""

from collections import defaultdict
from datetime import datetime
from energy import aggregate_estimates, detect_runner_type, estimate_energy
from utils import run_duration


class FlakinessAnalyzer:
    key = "flakiness"
    title = "Non-deterministic Test Flakiness"
    description = (
        "Finds failures associated with unstable CI behavior (explicit reruns "
        "or mixed outcomes on the same commit/workflow), indicating flakiness "
        "rather than deterministic product defects."
    )

    # Tighter window for strict CI, broader window for all-events analysis.
    STRICT_MIXED_OUTCOME_MAX_HOURS = 24
    ALL_EVENTS_MIXED_OUTCOME_MAX_HOURS = 72
    # Caps how many mixed-outcome groups get job-level deep checks.
    MAX_GROUPS_FOR_JOB_FETCH = 300

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, progress_cb=None, all_events=False):
        if progress_cb:
            progress_cb(f"Analyzing flakiness in {len(runs)} runs...")

        default_branch = self._get_default_branch(owner, repo)
        if progress_cb and default_branch:
            progress_cb(f"Using default branch '{default_branch}' for CI event filtering")

        ci_runs = self._filter_ci_runs(runs, default_branch)
        if progress_cb:
            progress_cb(
                f"Filtered to {len(ci_runs)} strict CI runs (from {len(runs)} total); "
                f"also computing all-events scope for comparison"
            )

        # Always compute both scopes so output can compare strict CI vs all-events.
        ci_grouped = self._group_by_workflow_and_sha(ci_runs)
        all_grouped = self._group_by_workflow_and_sha(runs)
        if progress_cb:
            progress_cb(
                f"Grouped strict CI scope into {len(ci_grouped)} buckets; "
                f"all-events scope into {len(all_grouped)} buckets"
            )

        ci_wf_median = self._workflow_success_medians(ci_runs)
        all_wf_median = self._workflow_success_medians(runs)

        (
            ci_flagged_runs,
            ci_details,
        ) = self._detect_flaky_failures(
            owner,
            repo,
            ci_grouped,
            ci_wf_median,
            nearby_window_hours=self.STRICT_MIXED_OUTCOME_MAX_HOURS,
            apply_short_duration_filter=True,
        )

        (
            all_flagged_runs,
            all_details,
        ) = self._detect_flaky_failures(
            owner,
            repo,
            all_grouped,
            all_wf_median,
            nearby_window_hours=self.ALL_EVENTS_MIXED_OUTCOME_MAX_HOURS,
            apply_short_duration_filter=False,
        )

        # Primary scope controls top-level summary/energy numbers only.
        primary_scope = "all" if all_events else "ci_only"
        if primary_scope == "all":
            flagged_runs = all_flagged_runs
            details = all_details
            primary_runs = runs
        else:
            flagged_runs = ci_flagged_runs
            details = ci_details
            primary_runs = ci_runs

        energy_estimates = []
        for run in flagged_runs:
            dur = run_duration(run)
            if dur <= 0:
                continue
            energy_estimates.append(
                estimate_energy(dur, detect_runner_type(run.get("labels")))
            )

        total_unique_shas = self._count_unique_shas(primary_runs)
        total_failed_runs = sum(1 for run in primary_runs if run.get("conclusion") == "failure")
        flaky_unique_shas = self._count_unique_shas(flagged_runs)
        ci_scope_summary = self._build_scope_summary(ci_runs, ci_flagged_runs, ci_details)
        all_scope_summary = self._build_scope_summary(runs, all_flagged_runs, all_details)

        summary = {
            "scope": "all_events_flakiness" if all_events else "ci_test_flakiness",
            "event_scope": primary_scope,
            "ci_runs_analyzed": len(primary_runs),
            "total_runs_in_fetch": len(runs),
            "total_failed_runs": total_failed_runs,
            "flaky_failures": len(flagged_runs),
            "flaky_unique_shas": flaky_unique_shas,
            "flakiness_rate_of_failures": self._pct(len(flagged_runs), total_failed_runs),
            "flakiness_rate_of_commits": self._pct(flaky_unique_shas, total_unique_shas),
            "rerun_failures": sum(1 for d in details if d.get("signal") == "run_attempt"),
            "fail_then_success_failures": sum(
                1 for d in details if d.get("signal") == "fail_then_success"
            ),
        }

        if progress_cb:
            progress_cb(
                "Scope comparison complete: "
                f"strict CI flaky={ci_scope_summary['flaky_failures']} "
                f"({ci_scope_summary['flakiness_rate_of_failures']:.1f}% of CI failures), "
                f"all-events flaky={all_scope_summary['flaky_failures']} "
                f"({all_scope_summary['flakiness_rate_of_failures']:.1f}% of all-event failures)"
            )

        return {
            "analyzer": self.key,
            "title": self.title,
            "summary": summary,
            "energy_waste": aggregate_estimates(energy_estimates),
            "strict_policy": {
                "summary": ci_scope_summary,
                "flaky_runs": {
                    "count": len(ci_flagged_runs),
                    "detail": ci_details[:50],
                },
            },
            "all_events_policy": {
                "summary": all_scope_summary,
                "flaky_runs": {
                    "count": len(all_flagged_runs),
                    "detail": all_details[:50],
                },
            },
            "recommendations": self._build_recommendations(
                ci_scope_summary,
                all_scope_summary,
            ),
        }

    def _group_by_workflow_and_sha(self, runs):
        grouped = defaultdict(list)
        for run in runs:
            sha = run.get("head_sha")
            workflow_id = run.get("workflow_id")
            if not sha or workflow_id is None:
                continue

            event = run.get("event") or ""
            head_branch = run.get("head_branch") or ""
            pr_number = self._extract_pr_number(run)
            workflow_name = run.get("name") or ""
            grouped[(workflow_id, workflow_name, sha, event, head_branch, pr_number)].append(run)

        for key in grouped:
            grouped[key].sort(key=self._sort_key)
        return grouped

    def _workflow_success_medians(self, runs):
        success_durations = defaultdict(list)
        for run in runs:
            if run.get("conclusion") != "success":
                continue
            workflow_id = run.get("workflow_id")
            if workflow_id is None:
                continue
            dur = run_duration(run)
            if dur > 0:
                success_durations[workflow_id].append(dur)

        medians = {}
        for workflow_id, durations in success_durations.items():
            values = sorted(durations)
            mid = len(values) // 2
            if len(values) % 2 == 1:
                medians[workflow_id] = values[mid]
            else:
                medians[workflow_id] = (values[mid - 1] + values[mid]) / 2
        return medians

    def _detect_flaky_failures(
        self,
        owner,
        repo,
        grouped,
        wf_median,
        nearby_window_hours,
        apply_short_duration_filter,
    ):
        flagged_runs = []
        details = []
        groups_fetched_for_jobs = 0

        for (
            workflow_id,
            _workflow_name,
            sha,
            event,
            head_branch,
            pr_number,
        ), group in grouped.items():
            conclusions = {run.get("conclusion") for run in group}
            has_failure = "failure" in conclusions
            has_success = "success" in conclusions
            if not has_failure:
                continue

            job_ctx = {"available": False, "run_failed_jobs": {}, "mixed_job_keys": set()}
            if has_success and groups_fetched_for_jobs < self.MAX_GROUPS_FOR_JOB_FETCH:
                candidate_job_ctx = self._build_group_job_context(owner, repo, group)
                job_ctx = candidate_job_ctx
                # Only count against the cap if job data was actually retrieved.
                if candidate_job_ctx.get("available"):
                    groups_fetched_for_jobs += 1

            for run in group:
                if run.get("conclusion") != "failure" or run.get("event") == "schedule":
                    continue

                run_attempt = run.get("run_attempt") or 1
                nearby_success_ids = (
                    self._nearby_success_run_ids(group, run, nearby_window_hours)
                    if has_success
                    else []
                )
                has_nearby_success = bool(nearby_success_ids)
                signal = self._pick_signal(
                    group,
                    run,
                    workflow_id,
                    wf_median,
                    has_success,
                    has_nearby_success,
                    job_ctx,
                )
                if not signal:
                    continue

                dur = run_duration(run)
                median = wf_median.get(workflow_id, 0)
                if (
                    apply_short_duration_filter
                    and median > 0
                    and dur > 0
                    and dur < (median * 0.4)
                ):
                    # Fast-fail runs are often config/startup issues, not flaky tests.
                    continue

                detail = {
                    "run_id": run.get("id"),
                    "run_number": run.get("run_number"),
                    "run_attempt": run_attempt,
                    "workflow_id": workflow_id,
                    "workflow_name": run.get("name") or "",
                    "sha": sha[:8],
                    "event": event,
                    "head_branch": head_branch,
                    "pr_number": pr_number,
                    "signal": signal,
                    "created_at": run.get("created_at"),
                    "duration_seconds": round(dur, 1),
                    "workflow_median_success_seconds": round(median, 1) if median else 0,
                    "nearby_success_run_ids": nearby_success_ids[:5],
                    "reason": self._reason(signal),
                }

                if job_ctx.get("available"):
                    run_failed_job_keys = job_ctx["run_failed_jobs"].get(run.get("id"), set())
                    mixed_job_keys = job_ctx.get("mixed_job_keys", set())
                    detail["failed_job_keys"] = sorted(list(run_failed_job_keys))[:10]
                    detail["matched_mixed_job_keys"] = sorted(
                        list(run_failed_job_keys.intersection(mixed_job_keys))
                    )[:10]

                if signal in ("fail_then_success_distant", "fail_then_success_no_job_data"):
                    # Keep weaker signals out of strict flaky counts.
                    continue

                flagged_runs.append(run)
                details.append(detail)

        return (
            flagged_runs,
            details,
        )

    def _pick_signal(
        self,
        group,
        run,
        workflow_id,
        wf_median,
        has_success,
        has_nearby_success,
        job_ctx,
    ):
        signal = None
        run_attempt = run.get("run_attempt") or 1

        if run_attempt > 1:
            first_attempt = next(
                (r for r in group if (r.get("run_attempt") or 1) == 1),
                None,
            )
            if first_attempt:
                first_dur = run_duration(first_attempt)
                median = wf_median.get(workflow_id, 0)
                if first_dur > 0 and not (median > 0 and first_dur < (median * 0.3)):
                    signal = "run_attempt"
                elif has_success and has_nearby_success:
                    signal = "fail_then_success"
            elif has_success and has_nearby_success:
                signal = "fail_then_success"
        elif has_success and has_nearby_success:
            signal = "fail_then_success"

        # When jobs are available, require per-job mixed outcomes for stronger evidence.
        if signal == "fail_then_success" and job_ctx.get("available"):
            run_failed_jobs = job_ctx["run_failed_jobs"].get(run.get("id"), set())
            mixed_job_keys = job_ctx.get("mixed_job_keys", set())
            if not run_failed_jobs or not any(job in mixed_job_keys for job in run_failed_jobs):
                signal = None
        elif signal == "fail_then_success" and not job_ctx.get("available"):
            signal = "fail_then_success_no_job_data"

        if not signal and has_success and not has_nearby_success:
            signal = "fail_then_success_distant"

        return signal

    def _build_group_job_context(self, owner, repo, group):
        run_failed_jobs = {}
        job_outcomes = defaultdict(set)
        any_jobs = False

        for run in group:
            run_id = run.get("id")
            if run_id is None:
                continue
            if not self.client.has_budget(needed=1):
                break

            try:
                jobs = self.client.get_jobs_for_run(owner, repo, run_id)
            except Exception:
                continue

            if not jobs:
                continue
            any_jobs = True

            failed_keys = set()
            for job in jobs:
                job_key = self._normalize_job_key(job.get("name") or "")
                if not job_key:
                    continue
                conclusion = job.get("conclusion")
                if conclusion in ("success", "failure"):
                    job_outcomes[job_key].add(conclusion)
                if conclusion == "failure":
                    failed_keys.add(job_key)

            if failed_keys:
                run_failed_jobs[run_id] = failed_keys

        mixed_job_keys = {
            key
            for key, outcomes in job_outcomes.items()
            if "success" in outcomes and "failure" in outcomes
        }
        return {
            "available": any_jobs,
            "run_failed_jobs": run_failed_jobs,
            "mixed_job_keys": mixed_job_keys,
        }

    @staticmethod
    def _normalize_job_key(name):
        return " ".join((name or "").lower().split())

    def _nearby_success_run_ids(self, group, failure_run, nearby_window_hours):
        failure_ts = self._parse_ts(failure_run.get("created_at"))
        if failure_ts == datetime.min:
            return []

        # Nearby means within the configured window for the active scope.
        max_seconds = nearby_window_hours * 3600
        success_ids = []
        for run in group:
            if run.get("conclusion") != "success":
                continue
            success_ts = self._parse_ts(run.get("created_at"))
            if success_ts == datetime.min:
                continue
            if abs((failure_ts - success_ts).total_seconds()) <= max_seconds:
                run_id = run.get("id")
                if run_id is not None:
                    success_ids.append(run_id)
        return success_ids

    @staticmethod
    def _reason(signal):
        if signal == "run_attempt":
            base = "GitHub rerun attempt failed (run_attempt > 1), indicating instability."
        elif signal == "fail_then_success_no_job_data":
            base = (
                "Same commit had mixed workflow outcomes, but job-level data was unavailable "
                "(kept as potential signal, not strict flakiness)."
            )
        elif signal == "fail_then_success_distant":
            base = (
                "Same commit had mixed outcomes, but the matching success is not temporally close "
                "(likely PR lifecycle/process-trigger effect)."
            )
        else:
            base = (
                "Same commit had mixed outcomes (both failure and success) in the "
                "same workflow, indicating non-deterministic behavior."
            )
        return base

    @staticmethod
    def _extract_pr_number(run):
        prs = run.get("pull_requests") or []
        if not prs:
            return None
        return (prs[0] or {}).get("number")

    @staticmethod
    def _parse_ts(ts):
        if not ts:
            return datetime.min
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return datetime.min

    def _sort_key(self, run):
        return (
            run.get("run_number") or 0,
            run.get("run_attempt") or 1,
            self._parse_ts(run.get("created_at")),
        )

    def _get_default_branch(self, owner, repo):
        try:
            if not self.client.has_budget():
                return None

            url = f"https://api.github.com/repos/{owner}/{repo}"
            response = self.client.get(url)
            if not response or response.status_code != 200:
                return None

            data = (
                response.json()
                if hasattr(response, "json")
                else response.get("repository", {})
            )
            return data.get("default_branch", "main")
        except Exception:
            return None

    @staticmethod
    def _filter_ci_runs(runs, default_branch):
        ci_runs = []
        for run in runs:
            event = run.get("event")
            if event == "pull_request":
                ci_runs.append(run)
            elif event == "push" and default_branch and run.get("head_branch") == default_branch:
                ci_runs.append(run)
            elif event == "push" and not default_branch:
                ci_runs.append(run)
        return ci_runs

    @staticmethod
    def _count_unique_shas(runs):
        return len({run.get("head_sha") for run in runs if run.get("head_sha")})

    @staticmethod
    def _pct(part, total):
        if not total:
            return 0.0
        return round((part / total) * 100, 1)

    def _build_scope_summary(
        self,
        scope_runs,
        flagged_runs,
        details,
    ):
        total_failed_runs = sum(1 for run in scope_runs if run.get("conclusion") == "failure")
        total_unique_shas = self._count_unique_shas(scope_runs)
        flaky_unique_shas = self._count_unique_shas(flagged_runs)

        return {
            "runs_analyzed": len(scope_runs),
            "total_failed_runs": total_failed_runs,
            "flaky_failures": len(flagged_runs),
            "flaky_unique_shas": flaky_unique_shas,
            "flakiness_rate_of_failures": self._pct(len(flagged_runs), total_failed_runs),
            "flakiness_rate_of_commits": self._pct(flaky_unique_shas, total_unique_shas),
            "rerun_failures": sum(1 for d in details if d.get("signal") == "run_attempt"),
            "fail_then_success_failures": sum(
                1 for d in details if d.get("signal") == "fail_then_success"
            ),
        }

    @staticmethod
    def _build_recommendations(strict_summary, all_events_summary):
        strict_flaky = strict_summary.get("flaky_failures", 0)
        all_flaky = all_events_summary.get("flaky_failures", 0)

        if strict_flaky == 0 and all_flaky == 0:
            return [
                "No flaky behavior detected in either strict CI scope or all-events scope. Well done.",
            ]

        recommendations = [
            "Enable selective retries for known flaky tests instead of rerunning full workflows.",
            "Quarantine and stabilize top flaky test files based on repeated mixed-outcome commit patterns.",
            "Use tighter dependency pinning and deterministic test seeds to reduce run-to-run variance.",
        ]

        if strict_flaky == 0 and all_flaky > 0:
            recommendations.append(
                "No strict CI flakiness detected, but all-events mode found flaky behavior; review non-PR/push automation workflows before changing primary reporting metrics."
            )

        return recommendations
