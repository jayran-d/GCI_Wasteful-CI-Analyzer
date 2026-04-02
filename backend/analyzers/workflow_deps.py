"""
Analyzer: Workflow Dependency & Cascade Failures.

Detects CI waste caused by child workflows (workflow_run trigger) that fail
or get cancelled solely because their parent workflow was cancelled or failed
— not because of any defect in the child's own code.

Layers:
  1. YAML inspection: discover dependent workflow pairs
  2. Run correlation: match child runs to parent runs via head SHA
  3. Classification: flag child runs whose parent never succeeded
  4. Log inspection: surface matched error patterns in failed jobs
"""

import yaml
from collections import defaultdict
from energy import estimate_energy, aggregate_estimates
from utils import get_run_duration_from_jobs


class WorkflowDependencyAnalyzer:

    key = "workflow_dependency"
    title = "Wasteful CI — Workflow Dependency Cascade Failures"
    description = (
        "Finds CI runs wasted because a child workflow (triggered via "
        "'workflow_run') was doomed from the start: its parent was cancelled "
        "or had already failed, so the child never had a chance to succeed. "
        "These failures reflect pipeline topology problems, not code defects."
    )

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def analyze(self, owner, repo, runs, deep_scan=False, deep_scan_limit=30, progress_cb=None):
        self._cb = progress_cb

        if progress_cb:
            progress_cb("Fetching all workflow definitions...")

        all_workflows = self.client.get_workflows(owner, repo)
        if progress_cb:
            progress_cb(f"→ {len(all_workflows)} workflows found")

        # Step 1: parse YAML to find workflow_run dependencies
        workflow_configs, dependent_workflows = self._fetch_workflow_configs(
            owner, repo, all_workflows, progress_cb
        )
        if progress_cb:
            progress_cb(f"→ {len(dependent_workflows)} dependent (child) workflows identified")

        # Step 2 & 3: correlate child runs with parent runs and classify
        (
            flaky_runs,
            total_child_runs,
            parent_cancelled_count,
            parent_failed_count,
            parent_notfound_count,
            log_pattern_count,
        ) = self._find_flaky_child_runs(owner, repo, dependent_workflows, runs, progress_cb)

        if progress_cb:
            progress_cb(f"→ {len(flaky_runs)} cascade-failure runs detected out of {total_child_runs} child runs")

        # Step 4: energy estimation
        energy_estimates = []
        for r in flaky_runs:
            jobs = self.client.get_jobs_for_run(owner, repo, r["child_run_id"])
            dur_child = get_run_duration_from_jobs(jobs)
            jobs = self.client.get_jobs_for_run(owner, repo, r["parent"]["run_id"])
            dur_parent = get_run_duration_from_jobs(jobs)
            if dur_child > 0:
                energy_estimates.append(
                    estimate_energy(dur_child, "linux")
                )
            if dur_parent > 0:
                energy_estimates.append(
                    estimate_energy(dur_parent, "linux")
                )

        return {
            "analyzer": self.key,
            "title": self.title,
            "summary": {
                "total_workflows_analyzed":  len(all_workflows),
                "dependent_workflows_found": len(dependent_workflows),
                "total_child_runs_analyzed": total_child_runs,
                "flaky_runs_detected":       len(flaky_runs),
                "waste_percentage": round(
                    len(flaky_runs) / len(runs) * 100, 1
                ) if total_child_runs > 0 else 0,
                "parent_cancelled_count": parent_cancelled_count,
                "parent_failed_count":    parent_failed_count,
                "parent_notfound_count":  parent_notfound_count,
                "log_pattern_matches":    log_pattern_count,
            },
            "dependent_workflows": dependent_workflows,
            "flaky_runs":          flaky_runs[:50],
            "workflows":           workflow_configs,
            "energy_waste":        aggregate_estimates(energy_estimates),
            "recommendations":     self._build_recommendations(
                flaky_runs,
                parent_cancelled_count,
                parent_failed_count,
                parent_notfound_count,
            ),
        }

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _fetch_workflow_configs(self, owner, repo, all_workflows, progress_cb=None):
        """
        Download and parse each workflow YAML file.
        Returns:
          workflow_configs     – list of dicts (for the 'workflows' result key)
          dependent_workflows  – dict of child-workflow-name → metadata
        """
        workflow_configs = []
        dependent_workflows = {}

        for wf in all_workflows:
            wf_path = wf.get("path", "")
            raw_yaml = self.client.get_workflow_file(owner, repo, wf_path)
            if raw_yaml is None:
                if progress_cb:
                    progress_cb(f"  ↳ Could not fetch {wf_path}, skipping")
                continue
            try:
                parsed = yaml.safe_load(raw_yaml)
            except yaml.YAMLError as exc:
                if progress_cb:
                    progress_cb(f"  ↳ YAML parse error in {wf_path}: {exc}")
                continue

            if not parsed or not isinstance(parsed, dict):
                if progress_cb:
                    progress_cb(f"  ↳ Unexpected format in {wf_path}, skipping")
                continue

            # PyYAML parses 'on:' as the boolean True — handle both keys
            triggers = parsed.get(True, parsed.get("on", {}))
            triggers = self._normalize_triggers(triggers, wf_path, progress_cb)
            if triggers is None:
                continue

            wf_name = wf.get("name")
            wf_id   = wf.get("id")

            # Detect workflow_run dependencies
            workflow_run_cfg = triggers.get("workflow_run")
            if isinstance(workflow_run_cfg, dict):
                parent_names = workflow_run_cfg.get("workflows", [])

                # FIX: "*" wildcard (or any plain string) comes in as a string
                # instead of a list — normalize it to always be a list.
                if isinstance(parent_names, str):
                    parent_names = [parent_names]

                on_types = workflow_run_cfg.get("types", [])
                dependent_workflows[wf_name] = {
                    "workflow_id":   wf_id,
                    "workflow_file": wf_path,
                    "triggered_by":  parent_names,
                    "on_types":      on_types,
                    "is_wildcard":   "*" in parent_names,  # flag for downstream use
                }
                if progress_cb:
                    base_url = f"https://github.com/{owner}/{repo}/blob/main"
                    child_url = f"{base_url}/{wf_path}"
                    parent_paths = [w.get("path", "") for w in all_workflows if w.get("name") in parent_names]
                    parent_urls = " | ".join(f"{base_url}/{p}" for p in parent_paths if p)
                    progress_cb(f"  workflow_run dependency: '{wf_name}' ← triggered by {parent_names}")
                    progress_cb(f"    child:  {child_url}")
                    if parent_urls:
                        progress_cb(f"    parent: {parent_urls}")
                    elif "*" in parent_names:
                        progress_cb(f"    parent: (wildcard — any workflow)")

            # Only keep JSON-serialisable fields
            safe_parsed = {
                str(k): v for k, v in parsed.items()
                if isinstance(v, (dict, list, str, int, float, bool, type(None)))
            }
            workflow_configs.append({
                "workflow_file": wf_path,
                "workflow_name": wf_name,
                "triggers":      triggers,
                "parsed":        safe_parsed,
            })

        return workflow_configs, dependent_workflows

    @staticmethod
    def _normalize_triggers(triggers, wf_path, progress_cb=None):
        """Coerce the 'on:' value into a plain dict, or return None on error."""
        if isinstance(triggers, str):
            return {triggers: {}}
        if isinstance(triggers, list):
            return {t: {} for t in triggers}
        if isinstance(triggers, dict):
            # Sanitise boolean keys injected by PyYAML
            return {str(k): v for k, v in triggers.items()}
        if progress_cb:
            progress_cb(f"  ↳ Unexpected triggers format in {wf_path}: {triggers!r}")
        return None

    def _find_flaky_child_runs(self, owner, repo, dependent_workflows, runs, progress_cb=None):
        """
        For each dependent (child) workflow, fetch its runs, correlate each
        run to its parent via head SHA, and classify whether the child failed
        solely because the parent did.
        """
        flaky_runs             = []
        total_child_runs       = 0
        parent_cancelled_count = 0
        parent_failed_count    = 0
        parent_notfound_count  = 0
        log_pattern_count      = 0

        for child_wf_name, child_wf_meta in dependent_workflows.items():
            child_wf_id  = child_wf_meta["workflow_id"]
            parent_names = child_wf_meta["triggered_by"]
            is_wildcard = child_wf_meta.get("is_wildcard", False)

            # Wildcard workflows (workflows: "*") are observers, not true
            # dependencies — cascade failure analysis doesn't apply.
            if is_wildcard:
                if progress_cb:
                    progress_cb(
                        f"Skipping wildcard workflow '{child_wf_name}' "
                        f"(triggered by any workflow — not a true dependency chain)"
                    )
                continue

            if progress_cb:
                progress_cb(
                    f"Analyzing dependent workflow '{child_wf_name}' "
                    f"(ID {child_wf_id}) triggered by {parent_names}..."
                )

            child_runs = [r for r in runs if r.get("workflow_id") == child_wf_id]
            if progress_cb:
                progress_cb(f"  → {len(child_runs)} child runs found")
            total_child_runs += len(child_runs)

            for idx, child_run in enumerate(child_runs, 1):
                child_run_id     = child_run.get("id")
                child_conclusion = child_run.get("conclusion")
                child_head_sha   = child_run.get("head_sha")
                child_run_url    = child_run.get("html_url")

                if progress_cb:
                    progress_cb(
                        f"  [{idx}/{len(child_runs)}] Child run #{child_run.get('run_number')} "
                        f"(id={child_run_id}, conclusion={child_conclusion}, "
                        f"sha={child_head_sha[:8] if child_head_sha else '?'}) "
                        f"{child_run_url or ''}"
                    )

                try:
                    parent_runs = self.client.find_parent_runs_by_sha(
                        owner, repo, child_head_sha, parent_names
                    )
                except Exception as exc:
                    if progress_cb:
                        progress_cb(
                            f"    ⚠ Error finding parent runs for sha "
                            f"{child_head_sha[:8] if child_head_sha else '?'}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    parent_runs = []

                parent_info       = parent_runs[0] if parent_runs else None
                parent_conclusion = parent_info.get("conclusion") if parent_info else None

                if progress_cb:
                    if parent_info:
                        progress_cb(
                            f"    ↳ Parent: '{parent_info.get('name')}' "
                            f"run #{parent_info.get('run_number')} "
                            f"(id={parent_info.get('id')}, conclusion={parent_conclusion}, "
                            f"sha={parent_info.get('head_sha', '?')[:8]}) "
                            f"{parent_info.get('html_url') or ''}"
                        )
                    else:
                        progress_cb(
                            f"    ↳ Parent: no matching parent run found "
                            f"for sha {child_head_sha[:8] if child_head_sha else '?'}"
                        )

                # A child run is a cascade failure when it failed/cancelled AND
                # its parent was cancelled, failed, or never found
                is_cascade_failure = (
                    child_conclusion in ("failure", "cancelled", None)
                    and (
                        parent_conclusion in ("cancelled", "failure")
                        or parent_info is None
                    )
                )

                if not is_cascade_failure:
                    if progress_cb:
                        progress_cb(
                            f"    → Not a cascade failure "
                            f"(child={child_conclusion}, parent={parent_conclusion})"
                        )
                    continue

                # Tally parent outcome bucket
                if parent_conclusion == "cancelled":
                    parent_cancelled_count += 1
                elif parent_conclusion == "failure":
                    parent_failed_count += 1
                else:
                    parent_notfound_count += 1

                # Inspect failed jobs & logs
                failed_jobs = self._inspect_failed_jobs(
                    owner, repo, child_run_id, log_pattern_count
                )
                log_pattern_count += sum(1 for j in failed_jobs if j["log_patterns"])

                reason = self._classify_parent_outcome(
                    child_wf_name, child_head_sha, parent_info, parent_conclusion
                )
                parent_run_url = parent_info.get("html_url") if parent_info else None
                flaky_runs.append({
                    "child_run_id":     child_run_id,
                    "child_workflow":   child_wf_name,
                    "child_run_number": child_run.get("run_number"),
                    "child_conclusion": child_conclusion,
                    "child_status":     child_run.get("status"),
                    "child_run_url":    child_run.get("html_url"),
                    "child_created_at": child_run.get("created_at"),
                    "child_started_at": child_run.get("run_started_at") or child_run.get("created_at"),
                    "child_ended_at":   child_run.get("updated_at"),
                    "labels":           child_run.get("labels", []),
                    "head_sha":         child_head_sha,
                    "parent": {
                        "name":       parent_info.get("name")        if parent_info else None,
                        "run_id":     parent_info.get("id")          if parent_info else None,
                        "run_number": parent_info.get("run_number")  if parent_info else None,
                        "conclusion": parent_conclusion,
                        "html_url":   parent_info.get("html_url")    if parent_info else None,
                        "started_at": (
                            parent_info.get("run_started_at") or parent_info.get("created_at")
                        ) if parent_info else None,
                        "ended_at":   parent_info.get("updated_at")  if parent_info else None,
                        "labels":     parent_info.get("labels", [])  if parent_info else [],
                    },
                    "failed_jobs":      failed_jobs,
                    "flakiness_reason": reason,
                })
                if progress_cb:
                    progress_cb(
                        f"    ✗ CASCADE FAILURE: child={child_conclusion}, "
                        f"parent={parent_conclusion}"
                    )

        return (
            flaky_runs,
            total_child_runs,
            parent_cancelled_count,
            parent_failed_count,
            parent_notfound_count,
            log_pattern_count,
        )

    def _inspect_failed_jobs(self, owner, repo, run_id, _log_pattern_count):
        """Return a list of failed-job dicts with log snippets."""
        failed_jobs = []
        try:
            jobs = self.client.get_jobs_for_run(owner, repo, run_id)
        except Exception:
            return failed_jobs

        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            log_info = self.client.get_job_log_snippet(owner, repo, job.get("id"))
            failed_jobs.append({
                "job_name":     job.get("name"),
                "html_url":     job.get("html_url"),
                "started_at":   job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "log_patterns": log_info["matched_patterns"] if log_info else [],
                "log_preview":  log_info["log_preview"]      if log_info else None,
                "labels":       job.get("labels", []),
            })
        return failed_jobs

    @staticmethod
    def _classify_parent_outcome(child_wf_name, child_head_sha, parent_info, parent_conclusion):
        """Return a human-readable reason string for the cascade failure."""
        if parent_info is None:
            return (
                f"Child workflow '{child_wf_name}' failed but no matching parent "
                f"run was found for head SHA {child_head_sha}. "
                "The parent may have been deleted or the workflow_run trigger fired "
                "with no parent context."
            )
        return (
            f"Child workflow '{child_wf_name}' failed because its parent "
            f"'{parent_info.get('name')}' (run #{parent_info.get('run_number')}) "
            f"concluded as '{parent_conclusion}'. "
            "No valid artifacts/outputs were passed through the workflow_run dependency."
        )

    @staticmethod
    def _build_recommendations(flaky_runs, cancelled, failed, notfound):
        recs = []
        if not flaky_runs:
            return ["No workflow dependency cascade failures detected."]

        if cancelled:
            recs.append(
                f"{cancelled} child run(s) were triggered by a cancelled parent. "
                "Add an 'if: ${{ github.event.workflow_run.conclusion == \\'success\\' }}' "
                "guard at the job level so child workflows skip automatically."
            )
        if failed:
            recs.append(
                f"{failed} child run(s) inherited a failing parent. "
                "Consider gating child workflows on parent success using the "
                "'workflow_run' conclusion check, or restructure the pipeline "
                "to use a single workflow with dependent jobs instead."
            )
        if notfound:
            recs.append(
                f"{notfound} child run(s) could not be correlated to a parent run. "
                "Verify that parent workflows are not being deleted and that the "
                "'workflows' list in the child's 'on.workflow_run' matches the "
                "parent's exact workflow name."
            )
        recs.append(
            "Consider consolidating tightly-coupled parent/child workflows into a "
            "single workflow with 'needs:' dependencies for simpler failure semantics."
        )
        return recs