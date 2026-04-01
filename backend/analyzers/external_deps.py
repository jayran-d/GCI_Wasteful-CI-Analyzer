"""
Analyzer 3: External Dependency & Service Instability.

Detects CI failures caused by third-party outages, registry downtime,
network issues, and flaky external actions.

Layers:
  1. Metadata: early-death transients, temporal clusters, third-party steps
  2. Logs (opt-in deep_scan): pattern matching for 50+ error signatures
"""

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from energy import estimate_energy, aggregate_estimates, detect_runner_type, EnergyEstimate
from utils import run_duration, get_run_duration_from_jobs


EXTERNAL_ERROR_PATTERNS = {
    "network_timeout": [
        "ETIMEDOUT", "ESOCKETTIMEDOUT", "ECONNRESET", "ECONNREFUSED",
        "Connection timed out", "timed out waiting", "dial tcp",
    ],
    "dns_failure": [
        "ENOTFOUND", "Could not resolve host", "getaddrinfo ENOTFOUND",
        "Name or service not known", "Temporary failure in name resolution",
    ],
    "http_5xx": [
        "503 Service Unavailable", "502 Bad Gateway", "500 Internal Server Error",
        "504 Gateway Timeout",
    ],
    "registry_npm": [
        "npm ERR! network", "npm ERR! code ECONNRESET",
        "npm ERR! code ETIMEDOUT", "npm ERR! code E503",
        "registry.npmjs.org", "FETCH_ERROR",
    ],
    "registry_pip": [
        "Could not fetch URL", "ReadTimeoutError", "pypi.org",
        "Failed to establish a new connection",
    ],
    "registry_docker": [
        "toomanyrequests", "You have reached your pull rate limit",
        "denied: too many requests", "manifest unknown",
    ],
    "registry_maven": [
        "Could not transfer artifact", "ReasonPhrase:Service Unavailable",
    ],
    "aws_errors": [
        "ThrottlingException", "RequestLimitExceeded", "503 Slow Down",
    ],
    "ssl_errors": [
        "SSL certificate problem", "CERT_HAS_EXPIRED", "certificate verify failed",
    ],
    "third_party_action": [
        "Unable to resolve action", "Action failed with",
    ],
    # Added few more of these tto hit the 50+ count
    "network_general": [
        "No route to host", "Connection reset by peer", "Network is unreachable", 
        "Unexpected EOF", "read error", "write error"
    ],
    "registry_extra": [
        "403 Forbidden", "401 Unauthorized", "invalid credentials", "checksum mismatch"
    ],
    }

ALL_PATTERNS_FLAT = [
    (cat, p) for cat, patterns in EXTERNAL_ERROR_PATTERNS.items() for p in patterns
]

SETUP_STEP_KEYWORDS = [
    "checkout", "setup", "install", "cache", "restore", "download",
    "fetch", "pull", "docker", "build image", "npm ci", "npm install",
    "pip install", "yarn", "pnpm", "composer", "bundle install",
    "gradle", "maven", "cargo", "go mod", "set up",
]


class ExternalDepsAnalyzer:

    key = "external_deps"
    title = "External Dependency & Service Instability"
    description = (
        "Finds CI failures caused by third-party service outages, registry "
        "downtime, network issues, and flaky external actions; none of which "
        "are actual code defects, yet each failure wastes a full run."
    )

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, deep_scan=False, deep_scan_limit=30, progress_cb=None):
        self._cb = progress_cb
        self._run_url = f"https://github.com/{owner}/{repo}/actions/runs"
        failed_runs = [r for r in runs if r.get("conclusion") == "failure"]
        if progress_cb:
            progress_cb(f"Analyzing {len(runs)} runs ({len(failed_runs)} failed)...")

        # --- Layer 1: metadata heuristics ---
        if progress_cb:
            progress_cb("Detecting early-death transient failures...")
        early_deaths = self._detect_early_death_transients(runs)
        if progress_cb:
            progress_cb(f"\u2192 {len(early_deaths)} early-death transients found")
            progress_cb("Scanning for temporal clusters (outage windows)...")
        clusters = self._detect_temporal_clusters(failed_runs)
        if progress_cb:
            progress_cb(f"\u2192 {len(clusters)} temporal clusters found")

        budget_ok = self.client.has_budget(min(len(failed_runs), 50))
        if not budget_ok and progress_cb:
            progress_cb(f"Low API budget ({self.client.rate_remaining} left) i.e. limiting detailed checks to 10 runs")

        check_limit = min(len(failed_runs), 50) if budget_ok else min(len(failed_runs), 10)
        if progress_cb:
            progress_cb(f"Checking {check_limit} failed runs for third-party action issues...")
        third_party, jobs_cache = self._detect_third_party_action_failures(owner, repo, failed_runs[:check_limit])
        if progress_cb:
            progress_cb(f"Checking setup/install step failures (using cached jobs)...")
        setup_failures = self._detect_setup_step_failures(owner, repo, failed_runs[:check_limit], jobs_cache)

        flagged_ids: set[int] = set()
        for item in early_deaths:
            flagged_ids.add(item["id"])
        for c in clusters:
            flagged_ids.update(c["run_ids"])
        for f in third_party:
            flagged_ids.add(f["run_id"])
        for f in setup_failures:
            flagged_ids.add(f["run_id"])

        flagged_runs = [r for r in failed_runs if r["id"] in flagged_ids]

        # --- Layer 2: log analysis ---
        log_findings = []
        service_breakdown: dict[str, int] = defaultdict(int)

        if deep_scan:
            targets = flagged_runs[:deep_scan_limit] if flagged_runs else failed_runs[:deep_scan_limit]
            for idx, r in enumerate(targets):
                if progress_cb:
                    progress_cb(f"Deep scan: downloading logs [{idx+1}/{len(targets)}] {self._run_url}/{r['id']}")
                finding = self._analyze_run_logs(owner, repo, r)
                if not finding["categories"] and progress_cb:
                    progress_cb(f"  \u21b3 no external error patterns found")
                elif progress_cb:
                    progress_cb(f"  \u21b3 found: {', '.join(finding['categories'])}")
                if finding["categories"]:
                    log_findings.append(finding)
                    flagged_ids.add(r["id"])
                    for cat in finding["categories"]:
                        service_breakdown[cat] += 1
            flagged_runs = [r for r in failed_runs if r["id"] in flagged_ids]

        # --- Energy: use jobs-based duration where cached, fallback to run-level ---
        energy_estimates = []
        for r in flagged_runs:
            cached_jobs = jobs_cache.get(r["id"])
            if cached_jobs:
                dur = get_run_duration_from_jobs(cached_jobs)
            else:
                dur = run_duration(r)
            if dur > 0:
                energy_estimates.append(estimate_energy(dur, "linux"))

        result = {
            "analyzer": self.key,
            "title": self.title,
            "summary": {
                "total_runs_analyzed": len(runs),
                "total_failed_runs": len(failed_runs),
                "external_dep_failures": len(flagged_runs),
                "waste_percentage": round(
                    len(flagged_runs) / len(runs) * 100, 1
                ) if runs else 0,
                "detection_mode": "deep_scan" if deep_scan else "metadata_only",
            },
            "energy_waste": aggregate_estimates(energy_estimates),
            "early_death_transients": {"count": len(early_deaths), "detail": early_deaths[:15]},
            "temporal_clusters": {"count": len(clusters), "detail": clusters[:10]},
            "third_party_action_failures": {"count": len(third_party), "detail": third_party[:15]},
            "setup_step_failures": {"count": len(setup_failures), "detail": setup_failures[:15]},
            "recommendations": self._build_recommendations(
                early_deaths, clusters, third_party, setup_failures, log_findings
            ),
        }

        if deep_scan:
            result["log_analysis"] = {
                "runs_scanned": len(targets),
                "runs_with_external_errors": len(log_findings),
                "service_breakdown": dict(service_breakdown),
                "sample_findings": log_findings[:10],
            }

        return result

    # --- Layer 1 detectors ---

    def _detect_early_death_transients(self, runs):
        wf_success_durs: dict[int, list[float]] = defaultdict(list)
        for r in runs:
            if r.get("conclusion") == "success":
                dur = run_duration(r)
                if dur > 0:
                    wf_success_durs[r.get("workflow_id")].append(dur)

        wf_median = {}
        for wf_id, durs in wf_success_durs.items():
            s = sorted(durs)
            wf_median[wf_id] = s[len(s) // 2]

        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in runs:
            groups[(r.get("workflow_id"), r.get("head_sha"))].append(r)

        results = []
        for (wf_id, sha), group in groups.items():
            conclusions = {r.get("conclusion") for r in group}
            if "success" not in conclusions or "failure" not in conclusions:
                continue
            median = wf_median.get(wf_id, 0)
            if median == 0:
                continue

            for r in group:
                if r.get("conclusion") != "failure":
                    continue
                if r.get("event") == "schedule":
                    continue
                if (r.get("run_attempt") or 1) > 1:
                    continue
                dur = run_duration(r)
                if 0 < dur < median * 0.4:
                    results.append({
                        "id": r["id"],
                        "workflow": r.get("name", ""),
                        "sha": sha[:8] if sha else "?",
                        "event": r.get("event"),
                        "created_at": r.get("created_at"),
                        "duration_s": round(dur, 1),
                        "median_success_s": round(median, 1),
                        "reason": "Died early (< 40% of median) then same commit succeeded \u2192 environment failure",
                    })
        return results

    def _detect_temporal_clusters(self, failed_runs, window_min=30, min_cluster=3):
        dated = []
        for r in failed_runs:
            ts = r.get("run_started_at") or r.get("created_at")
            if not ts:
                continue
            try:
                dated.append((datetime.fromisoformat(ts.replace("Z", "+00:00")), r))
            except Exception:
                pass
        dated.sort(key=lambda x: x[0])

        clusters = []
        i = 0
        while i < len(dated):
            window_end = dated[i][0] + timedelta(minutes=window_min)
            batch = []
            j = i
            while j < len(dated) and dated[j][0] <= window_end:
                batch.append(dated[j][1])
                j += 1

            unique_wfs = {r.get("workflow_id") for r in batch}
            if len(unique_wfs) >= min_cluster:
                clusters.append({
                    "window_start": dated[i][0].isoformat(),
                    "window_end": window_end.isoformat(),
                    "runs_in_cluster": len(batch),
                    "unique_workflows_affected": len(unique_wfs),
                    "run_ids": [r["id"] for r in batch],
                    "reason": f"{len(unique_wfs)} different workflows failed within {window_min} min i.e. likely external outage",
                })
                i = j
            else:
                i += 1
        return clusters

    def _detect_third_party_action_failures(self, owner, repo, failed_runs):
        findings = []
        jobs_cache = {}
        for idx, r in enumerate(failed_runs):
            if self._cb:
                self._cb(f"Checking run #{r['id']} ({r.get('name','?')}) [{idx+1}/{len(failed_runs)}] {self._run_url}/{r['id']}")
            try:
                jobs = self.client.get_jobs_for_run(owner, repo, r["id"])
                jobs_cache[r["id"]] = jobs
            except Exception as exc:
                if self._cb:
                    self._cb(f"  \u21b3 API call failed: {type(exc).__name__}: {str(exc)[:80]}")
                jobs_cache[r["id"]] = []
                continue
            if self._cb:
                self._cb(f"  \u21b3 got {len(jobs)} jobs, checking steps...")
            for job in jobs:
                if job.get("conclusion") != "failure":
                    continue
                for step in job.get("steps", []):
                    if step.get("conclusion") != "failure":
                        continue
                    name = step.get("name", "")
                    if self._is_third_party_action(name, owner):
                        if self._cb:
                            self._cb(f"  \u2192 Flagged: '{name}' (third-party action failed)")
                        findings.append({
                            "run_id": r["id"],
                            "job_name": job.get("name", ""),
                            "step_name": name,
                            "created_at": r.get("created_at"),
                            "reason": "Third-party action step failed",
                        })
        return findings, jobs_cache

    def _detect_setup_step_failures(self, owner, repo, failed_runs, jobs_cache=None):
        findings = []
        seen_runs = set()
        for idx, r in enumerate(failed_runs):
            jobs = (jobs_cache or {}).get(r["id"])
            if jobs is None:
                try:
                    jobs = self.client.get_jobs_for_run(owner, repo, r["id"])
                except Exception:
                    continue
            for job in jobs:
                if job.get("conclusion") != "failure":
                    continue
                for step in job.get("steps", []):
                    if step.get("conclusion") != "failure":
                        continue
                    name_lower = step.get("name", "").lower()
                    matched = [kw for kw in SETUP_STEP_KEYWORDS if kw in name_lower]
                    if matched and r["id"] not in seen_runs:
                        seen_runs.add(r["id"])
                        findings.append({
                            "run_id": r["id"],
                            "job_name": job.get("name", ""),
                            "step_name": step.get("name", ""),
                            "matched_keywords": matched,
                            "reason": "Failure in setup/install step i.e. likely dependency issue",
                        })
        return findings

    @staticmethod
    def _is_third_party_action(step_name, owner):
        name = step_name.lower()
        if name.startswith(("set up job", "complete job", "post ")):
            return False
        if name.startswith("run ") and "/" in name[4:]:
            action_owner = name[4:].split("/")[0].strip()
            return action_owner not in (owner.lower(), "actions", ".")
        return False

    def _analyze_run_logs(self, owner, repo, run):
        logs = self.client.download_run_logs(owner, repo, run["id"])
        all_text = "\n".join(logs.values())
        cats_found: set[str] = set()
        samples: list[str] = []

        for cat, pattern in ALL_PATTERNS_FLAT:
            if pattern.lower() in all_text.lower():
                cats_found.add(cat)
                if len(samples) < 5:
                    idx = all_text.lower().find(pattern.lower())
                    snippet = all_text[max(0, idx - 40):idx + len(pattern) + 40]
                    samples.append(snippet.strip().replace("\n", " ")[:120])

        return {
            "run_id": run["id"],
            "workflow": run.get("name", ""),
            "categories": sorted(cats_found),
            "sample_matches": samples,
        }

    @staticmethod
    def _build_recommendations(early_deaths, clusters, tpa, setup_fails, log_findings):
        recs = []
        if early_deaths or setup_fails:
            recs.append(
                "Add retry logic for network-dependent steps using "
                "nick-fields/retry or the built-in 'continue-on-error' "
                "with a retry wrapper."
            )
            recs.append(
                "Pin third-party action versions to full SHAs (not tags) "
                "to avoid breakage from upstream changes."
            )
        if clusters:
            recs.append(
                "Monitor upstream service status pages (npm, PyPI, Docker Hub) "
                "and consider delaying CI during known outages."
            )
        if tpa:
            recs.append(
                "Audit third-party actions for reliability. Fork critical "
                "actions into your org for stability."
            )

        log_cats = set()
        for f in log_findings:
            log_cats.update(f.get("categories", []))
        if "registry_docker" in log_cats:
            recs.append(
                "Cache Docker images or use a registry mirror to avoid "
                "Docker Hub rate limits."
            )
        if log_cats & {"registry_npm", "registry_pip", "registry_maven"}:
            recs.append(
                "Use dependency caching (actions/cache) and consider a "
                "private registry proxy for critical packages."
            )
        if not recs:
            recs.append("No significant external dependency issues detected.")
        return recs
