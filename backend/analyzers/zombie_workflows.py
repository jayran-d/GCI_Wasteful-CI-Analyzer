"""
Analyzer: Zombie Workflows

Detects scheduled CI workflows that run repeatedly despite consistently failing,
wasting compute time, energy, and money without producing value.

Key insight (from research):
  - 54.8% of workflow reruns are triggered by "schedule" events
  - A major root cause is deprecated configurations (e.g., outdated Ubuntu runners)
  - A single scheduled workflow can fail daily for months, wasting hundreds of days of compute time because no one manually corrects the issue

Detection layers:
  1. Identify scheduled workflows with high failure rates (>80%)
  2. Detect long consecutive failure streaks (daily failures for weeks/months)
  3. Scan for deprecated runner labels and outdated configurations
  4. Estimate total wasted energy, CO2, and cost from zombie runs
"""

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from energy import estimate_energy, aggregate_estimates, detect_runner_type
from utils import run_duration


# Deprecated / EOL runner labels
DEPRECATED_RUNNERS = {
    "ubuntu-16.04": "Ubuntu 16.04 (EOL April 2021)",
    "ubuntu-18.04": "Ubuntu 18.04 (removed December 2022)",
    "ubuntu-20.04": "Ubuntu 20.04 (scheduled for deprecation)",
    "macos-10.15": "macOS 10.15 Catalina (removed May 2022)",
    "macos-11": "macOS 11 Big Sur (removed June 2024)",
    "macos-12": "macOS 12 Monterey (deprecated)",
    "windows-2016": "Windows Server 2016 (removed March 2022)",
    "windows-2019": "Windows Server 2019 (scheduled for deprecation)",
}

# Deprecated actions / patterns commonly found in zombie workflows
DEPRECATED_CONFIG_PATTERNS = [
    "actions/checkout@v1",
    "actions/checkout@v2",
    "actions/setup-node@v1",
    "actions/setup-python@v1",
    "actions/setup-python@v2",
    "actions/setup-java@v1",
    "actions/cache@v1",
    "actions/cache@v2",
    "::set-output",          # deprecated command syntax
    "save-state",            # deprecated command syntax
    "::set-env",             # deprecated (security risk)
    "node12",                # Node 12 actions deprecated
    "node16",                # Node 16 actions deprecated
]


class ZombieWorkflowAnalyzer:
    """Detects scheduled workflows that fail repeatedly without being fixed."""

    key = "zombie_scheduled"
    title = "Zombie Scheduled Workflows"
    description = (
        "Finds scheduled (cron) workflows that fail repeatedly without being "
        "fixed — often due to deprecated runners or outdated configs — wasting "
        "energy on runs that will never succeed."
    )

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, progress_cb=None):
        self._cb = progress_cb
        self._status = progress_cb or (lambda msg: None)

        scheduled_runs = [r for r in runs if r.get("event") == "schedule"]
        all_failed = [r for r in runs if r.get("conclusion") == "failure"]

        self._status(
            f"Analyzing {len(runs)} runs ({len(scheduled_runs)} scheduled, "
            f"{len(all_failed)} total failures)..."
        )

        # --- Layer 1: Scheduled workflows with high failure rates ---
        self._status("Detecting zombie scheduled workflows (high failure rate)...")
        zombies = self._detect_zombie_scheduled(scheduled_runs)
        self._status(f"→ {len(zombies)} zombie workflows found")

        # --- Layer 2: Long consecutive failure streaks ---
        self._status("Scanning for long consecutive failure streaks...")
        streaks = self._detect_failure_streaks(scheduled_runs)
        self._status(f"→ {len(streaks)} long failure streaks found")

        # --- Layer 3: Deprecated runner detection ---
        self._status("Checking for deprecated runner configurations...")
        deprecated = self._detect_deprecated_runners(owner, repo, runs)
        self._status(f"→ {len(deprecated)} workflows with deprecated configs")

        # --- Layer 4: Deep scan workflow files for deprecated patterns ---
        deprecated_configs = []
        if self.client.has_budget(5):
            self._status("Scanning workflow YAML files for outdated patterns...")
            deprecated_configs = self._scan_workflow_files(owner, repo)
            self._status(f"→ {len(deprecated_configs)} deprecated config patterns found")

        # --- Compute energy waste ---
        self._status("Computing energy waste from zombie workflows...")
        flagged_run_ids = set()
        for z in zombies:
            flagged_run_ids.update(z.get("failed_run_ids", []))
        for s in streaks:
            flagged_run_ids.update(s.get("run_ids", []))

        energy_estimates = []
        for r in scheduled_runs:
            if r["id"] in flagged_run_ids and r.get("conclusion") == "failure":
                dur = run_duration(r)
                if dur > 0:
                    energy_estimates.append(
                        estimate_energy(dur, detect_runner_type(r.get("labels")))
                    )

        # Compute total wasted time
        total_wasted_seconds = sum(e.duration_seconds for e in energy_estimates)
        total_wasted_days = total_wasted_seconds / 86400

        self._status(
            f"Total zombie waste: {len(energy_estimates)} failed runs, "
            f"{total_wasted_days:.1f} days of compute time"
        )

        # --- Build result ---
        result = {
            "analyzer": self.key,
            "title": self.title,
            "summary": {
                "total_runs_analyzed": len(runs),
                "total_scheduled_runs": len(scheduled_runs),
                "scheduled_percentage": round(
                    len(scheduled_runs) / len(runs) * 100, 1
                ) if runs else 0,
                "zombie_workflows": len(zombies),
                "total_zombie_failed_runs": len(flagged_run_ids),
                "longest_streak_days": max(
                    (s.get("streak_days", 0) for s in streaks), default=0
                ),
                "total_wasted_days": round(total_wasted_days, 1),
                "deprecated_configs_found": len(deprecated) + len(deprecated_configs),
            },
            "energy_waste": aggregate_estimates(energy_estimates) if energy_estimates else {
                "total_energy_kwh": 0,
                "total_carbon_grams_co2": 0,
            },
            "zombie_workflows": zombies[:20],
            "failure_streaks": streaks[:15],
            "deprecated_runners": deprecated[:15],
            "deprecated_configs": deprecated_configs[:15],
            "recommendations": self._build_recommendations(
                zombies, streaks, deprecated, deprecated_configs
            ),
        }

        return result

    def _detect_zombie_scheduled(self, scheduled_runs, min_runs=3, fail_threshold=0.8):
        """
        Find scheduled workflows where >80% of runs fail.
        These are "zombies" — running on autopilot, always failing, never fixed.
        """
        by_workflow = defaultdict(lambda: {
            "name": "", "total": 0, "failed": 0, "success": 0,
            "failed_run_ids": [], "first_fail": None, "last_fail": None,
        })

        for r in scheduled_runs:
            wf_id = r.get("workflow_id") or r.get("name", "unknown")
            entry = by_workflow[wf_id]
            entry["name"] = r.get("name", "unknown")
            entry["total"] += 1

            if r.get("conclusion") == "failure":
                entry["failed"] += 1
                entry["failed_run_ids"].append(r["id"])
                ts = r.get("created_at", "")
                if not entry["first_fail"] or ts < entry["first_fail"]:
                    entry["first_fail"] = ts
                if not entry["last_fail"] or ts > entry["last_fail"]:
                    entry["last_fail"] = ts
            elif r.get("conclusion") == "success":
                entry["success"] += 1

        zombies = []
        for wf_id, data in by_workflow.items():
            if data["total"] < min_runs:
                continue
            fail_rate = data["failed"] / data["total"]
            if fail_rate < fail_threshold:
                continue

            # Calculate how long this zombie has been running
            span_days = 0
            if data["first_fail"] and data["last_fail"]:
                try:
                    t0 = datetime.fromisoformat(data["first_fail"].replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(data["last_fail"].replace("Z", "+00:00"))
                    span_days = (t1 - t0).days
                except Exception:
                    pass

            # Compute wasted time for this workflow
            wasted_seconds = 0
            for r in scheduled_runs:
                rid = r.get("workflow_id") or r.get("name", "unknown")
                if rid == wf_id and r.get("conclusion") == "failure":
                    wasted_seconds += run_duration(r)

            zombies.append({
                "workflow_id": wf_id,
                "workflow_name": data["name"],
                "total_scheduled_runs": data["total"],
                "failed_runs": data["failed"],
                "success_runs": data["success"],
                "failure_rate": round(fail_rate * 100, 1),
                "zombie_span_days": span_days,
                "wasted_compute_seconds": round(wasted_seconds, 1),
                "wasted_compute_days": round(wasted_seconds / 86400, 1),
                "first_failure": data["first_fail"],
                "last_failure": data["last_fail"],
                "failed_run_ids": data["failed_run_ids"],
                "reason": (
                    f"Scheduled workflow '{data['name']}' has {data['failed']}/{data['total']} "
                    f"failures ({round(fail_rate*100,1)}%) over {span_days} days — "
                    f"wasting {round(wasted_seconds/86400,1)} days of compute time."
                ),
            })

        # Sort by wasted time descending
        zombies.sort(key=lambda z: z["wasted_compute_seconds"], reverse=True)
        return zombies

    def _detect_failure_streaks(self, scheduled_runs, min_streak=5):
        """
        Detect long consecutive failure streaks in scheduled workflows.
        A streak of 30+ daily failures = ~1 month of zombie behavior.
        """
        by_workflow = defaultdict(list)
        for r in scheduled_runs:
            wf_id = r.get("workflow_id") or r.get("name", "unknown")
            by_workflow[wf_id].append(r)

        streaks = []
        for wf_id, wf_runs in by_workflow.items():
            # Sort by created_at
            sorted_runs = sorted(wf_runs, key=lambda r: r.get("created_at", ""))
            name = sorted_runs[0].get("name", "unknown") if sorted_runs else "unknown"

            current_streak = []
            best_streak = []

            for r in sorted_runs:
                if r.get("conclusion") == "failure":
                    current_streak.append(r)
                else:
                    if len(current_streak) > len(best_streak):
                        best_streak = current_streak[:]
                    current_streak = []

            # Check final streak
            if len(current_streak) > len(best_streak):
                best_streak = current_streak[:]

            if len(best_streak) >= min_streak:
                first_ts = best_streak[0].get("created_at", "")
                last_ts = best_streak[-1].get("created_at", "")
                streak_days = 0
                try:
                    t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                    streak_days = (t1 - t0).days
                except Exception:
                    pass

                wasted_seconds = sum(run_duration(r) for r in best_streak)
                still_active = best_streak == current_streak  # streak hasn't been broken

                streaks.append({
                    "workflow_id": wf_id,
                    "workflow_name": name,
                    "consecutive_failures": len(best_streak),
                    "streak_days": streak_days,
                    "wasted_compute_seconds": round(wasted_seconds, 1),
                    "wasted_compute_days": round(wasted_seconds / 86400, 1),
                    "first_failure": first_ts,
                    "last_failure": last_ts,
                    "still_active": still_active,
                    "run_ids": [r["id"] for r in best_streak],
                    "reason": (
                        f"'{name}' failed {len(best_streak)} times consecutively "
                        f"over {streak_days} days"
                        + (" (still ongoing!)" if still_active else "")
                        + f" — wasting {round(wasted_seconds/86400,1)} days of compute."
                    ),
                })

        streaks.sort(key=lambda s: s["consecutive_failures"], reverse=True)
        return streaks

    def _detect_deprecated_runners(self, owner, repo, runs):
        """
        Check job-level data for deprecated runner labels.
        Uses API calls to fetch jobs for a sample of failed scheduled runs.
        """
        scheduled_failed = [
            r for r in runs
            if r.get("event") == "schedule" and r.get("conclusion") == "failure"
        ]
        if not scheduled_failed:
            return []

        # Sample up to 10 runs to check (preserve API budget)
        sample = scheduled_failed[:10]
        deprecated_found = []
        seen_workflows = set()

        for r in sample:
            if not self.client.has_budget(2):
                self._status("⏳ Low API budget, skipping remaining runner checks")
                break

            wf_name = r.get("name", "unknown")
            if wf_name in seen_workflows:
                continue
            seen_workflows.add(wf_name)

            try:
                jobs = self.client.get_jobs_for_run(owner, repo, r["id"])
                for job in jobs:
                    labels = job.get("labels", [])
                    runner = job.get("runner_name", "")
                    for label in labels:
                        label_lower = label.lower().strip()
                        if label_lower in DEPRECATED_RUNNERS:
                            deprecated_found.append({
                                "workflow_name": wf_name,
                                "workflow_id": r.get("workflow_id"),
                                "job_name": job.get("name", "unknown"),
                                "runner_label": label,
                                "deprecation_info": DEPRECATED_RUNNERS[label_lower],
                                "run_id": r["id"],
                                "reason": (
                                    f"'{wf_name}' uses deprecated runner '{label}' "
                                    f"({DEPRECATED_RUNNERS[label_lower]}). "
                                    f"This is a common cause of zombie failures."
                                ),
                            })
            except Exception:
                continue

        return deprecated_found

    def _scan_workflow_files(self, owner, repo):
        """Scan workflow YAML files for deprecated patterns."""
        deprecated_found = []

        try:
            workflows = self.client.get_workflows(owner, repo)
        except Exception:
            return []

        for wf in workflows[:10]:  # limit to 10 workflow files
            if not self.client.has_budget(1):
                break

            path = wf.get("path", "")
            if not path:
                continue

            content = self.client.get_workflow_file(owner, repo, path)
            if not content:
                continue

            for pattern in DEPRECATED_CONFIG_PATTERNS:
                if pattern.lower() in content.lower():
                    deprecated_found.append({
                        "workflow_name": wf.get("name", path),
                        "file_path": path,
                        "pattern": pattern,
                        "reason": f"Workflow '{wf.get('name', path)}' uses deprecated pattern: {pattern}",
                    })

        return deprecated_found

    @staticmethod
    def _build_recommendations(zombies, streaks, deprecated, deprecated_configs):
        recs = []

        if zombies:
            recs.append(
                "Disable or fix zombie scheduled workflows that consistently fail. "
                "Each daily failure wastes compute, energy, and API quota for no value."
            )

        if any(s.get("still_active") for s in streaks):
            active = [s for s in streaks if s.get("still_active")]
            names = ", ".join(s["workflow_name"] for s in active[:3])
            recs.append(
                f"URGENT: {len(active)} workflow(s) are still failing on every scheduled run "
                f"({names}). Disable or fix them immediately to stop ongoing waste."
            )

        if deprecated:
            runners = set(d["runner_label"] for d in deprecated)
            recs.append(
                f"Update deprecated runner labels ({', '.join(runners)}). "
                f"Deprecated runners are a top cause of zombie failures — "
                f"update to 'ubuntu-latest' or a current version."
            )

        if deprecated_configs:
            patterns = set(d["pattern"] for d in deprecated_configs[:5])
            recs.append(
                f"Update deprecated action versions/patterns found: {', '.join(patterns)}. "
                f"Outdated actions can cause silent failures in scheduled workflows."
            )

        if streaks:
            max_streak = max(s["consecutive_failures"] for s in streaks)
            recs.append(
                f"Set up alerts for consecutive CI failures. The longest streak found was "
                f"{max_streak} consecutive failures — an alert after 3-5 failures would "
                f"catch zombie workflows early."
            )

        if not recs:
            recs.append(
                "No zombie scheduled workflows detected. Scheduled workflows appear healthy."
            )

        return recs
