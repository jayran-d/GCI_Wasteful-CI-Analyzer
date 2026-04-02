"""
GCI i.e. Green CI Analyzer
Flask application with streaming analysis endpoint.

Routes:
  GET  /                          → Web UI
  POST /api/analyze/stream        → SSE streaming analysis
  POST /api/diagnose              → AI diagnosis for a flagged run or job
  GET  /api/health                → Health check
"""

import json
import os
import queue
import threading
import traceback
from collections import defaultdict
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from datetime import datetime, timezone
from dotenv import load_dotenv
from github_client import GitHubClient
from energy import estimate_energy, aggregate_estimates, detect_runner_type, CARBON_INTENSITY_G_PER_KWH
from impact import compute_impact
from utils import run_duration
from analyzers.zombie_workflows import ZombieWorkflowAnalyzer
from analyzers.flakiness import FlakinessAnalyzer
from analyzers.external_deps import ExternalDepsAnalyzer
from analyzers import (
    FlakinessAnalyzer,
    ExternalDepsAnalyzer,
    InefficientTriggerAnalyzer,
    ZombieWorkflowAnalyzer,
    WorkflowDependencyAnalyzer,
)

app = Flask(__name__)
CORS(app)
load_dotenv()

ANALYZER_LIST = [
    ("flakiness", FlakinessAnalyzer),
    ("zombie_scheduled", ZombieWorkflowAnalyzer),
    ("external_deps", ExternalDepsAnalyzer),
    ("inefficient_triggers", InefficientTriggerAnalyzer),
    ("workflow_dependencies", WorkflowDependencyAnalyzer), 
]
ANALYZER_MAP = dict(ANALYZER_LIST)


def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


def _extract_flagged_run_ids(obj, key=None):
    """
    Recursively extract run IDs that are explicitly present in an analyzer
    result's detail/findings structures. Skips summary, energy_waste, and
    recommendations so we only get IDs from actual flagged-run records.
    """
    ids = set()
    if obj is None or not isinstance(obj, (dict, list)):
        return ids
    if isinstance(obj, list):
        # Arrays named *_ids or run_ids contain bare ID numbers
        if key and (key.endswith('_ids') or key == 'run_ids'):
            for item in obj:
                if isinstance(item, (int, float)) and item > 0:
                    ids.add(int(item))
        else:
            for item in obj:
                ids.update(_extract_flagged_run_ids(item))
        return ids
    # Dict — only pick up explicit run-reference fields (not generic 'id')
    for field in ('run_id', 'child_run_id'):
        v = obj.get(field)
        if isinstance(v, (int, float)) and v > 0:
            ids.add(int(v))
    # Recurse but skip non-detail sections
    for k, v in obj.items():
        if k in ('summary', 'energy_waste', 'recommendations'):
            continue
        if isinstance(v, (dict, list)):
            ids.update(_extract_flagged_run_ids(v, k))
    return ids


@app.route("/")
def index():
    return jsonify({"service": "GCI API", "docs": "POST /api/analyze/stream"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/analyze/stream", methods=["POST"])
def analyze_stream():
    """Stream analysis progress via SSE over POST."""
    body = request.get_json(force=True)
    repo_url = body.get("repo_url", "").strip()
    token = body.get("github_token") or os.getenv("GITHUB_TOKEN") or None
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    deep_scan = body.get("deep_scan", False)
    all_events_raw = body.get("all_events", False)
    if isinstance(all_events_raw, str):
        all_events = all_events_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        all_events = bool(all_events_raw)

    def generate():
        nonlocal deep_scan
        # Phase 1: validate
        if not repo_url:
            yield _sse("error", {"message": "Missing repo_url"})
            return

        client = GitHubClient(token=token)
        try:
            owner, repo = client.parse_repo(repo_url)
        except ValueError as e:
            yield _sse("error", {"message": str(e)})
            return

        # Check rate limit (works with and without token)
        rate_info = {}
        warnings = []
        try:
            rate = client.get_rate_limit()
            core = rate.get("resources", {}).get("core", {})
            rate_info = {"remaining": core.get("remaining", 60), "limit": core.get("limit", 60)}
        except Exception:
            rate_info = {"remaining": 60 if not token else 5000, "limit": 60 if not token else 5000}

        if not client.authenticated:
            warnings.append(
                "No GitHub token i.e. limited to 60 API calls/hr. "
                "Analysis may fail on repos with many runs. Add a token for 5000/hr."
            )
            if deep_scan:
                deep_scan = False
                warnings.append("Deep scan auto-disabled (requires token i.e. too many API calls).")

        yield _sse("connected", {
            "owner": owner, "repo": repo,
            "start_date": start_date, "end_date": end_date,
            "all_events": all_events,
            "rate_limit": rate_info,
            "authenticated": client.authenticated,
            "warnings": warnings,
        })

        # Phase 2: fetch runs page by page
        created = None
        if start_date and end_date:
            created = f"{start_date}..{end_date}"
        elif start_date:
            created = f">={start_date}"
        elif end_date:
            created = f"<={end_date}"

        all_runs = []
        try:
            for page_num, page_runs, total_count in client.get_workflow_runs_paged(
                owner, repo, created=created,max_pages=3
            ):
                all_runs.extend(page_runs)
                yield _sse("runs_page", {
                    "page": page_num,
                    "page_size": len(page_runs),
                    "fetched_so_far": len(all_runs),
                    "total_available": total_count,
                    "rate_remaining": client.rate_remaining,
                    "runs": [
                        {
                            "id": r.get("id"),
                            "name": r.get("name", "unknown"),
                            "conclusion": r.get("conclusion"),
                            "html_url": r.get("html_url"),
                            "created_at": r.get("created_at"),
                            "head_branch": r.get("head_branch"),
                            "head_sha": r.get("head_sha", ""),
                            "commit_message": (
                                r.get("head_commit", {}).get("message", "").split("\n")[0]
                                if isinstance(r.get("head_commit"), dict)
                                else ""
                            ),
                        }
                        for r in page_runs
                    ],
                })
        except Exception as e:
            yield _sse("error", {"message": f"GitHub API error: {e}"})
            return

        if not all_runs:
            yield _sse("error", {"message": "No workflow runs found for this date range."})
            return

        # Budget check: estimate API calls needed for analysis
        n_failed = sum(1 for r in all_runs if r.get("conclusion") == "failure")
        n_unique_shas = len({r.get("head_sha") for r in all_runs if r.get("head_sha")})
        est_calls = n_failed + n_unique_shas + (n_failed if deep_scan else 0) + 5
        budget = client.rate_remaining or (60 if not client.authenticated else 5000)

        if est_calls > budget * 0.8:
            if deep_scan:
                deep_scan = False
                yield _sse("warning", {
                    "message": f"Deep scan disabled i.e. need ~{est_calls} API calls but only {budget} remaining."
                })
            if est_calls > budget:
                yield _sse("warning", {
                    "message": f"Low API budget: need ~{est_calls} calls, have {budget}. Some analyzers may skip detailed checks."
                })

        # Phase 3: summarize what we got
        wf_summary = defaultdict(lambda: {"total": 0, "success": 0, "failure": 0, "other": 0})
        event_counts = defaultdict(int)
        for r in all_runs:
            name = r.get("name", "unknown")
            wf_summary[name]["total"] += 1
            c = r.get("conclusion", "other")
            if c == "success":
                wf_summary[name]["success"] += 1
            elif c == "failure":
                wf_summary[name]["failure"] += 1
            else:
                wf_summary[name]["other"] += 1
            event_counts[r.get("event", "unknown")] += 1

        yield _sse("runs_complete", {
            "total_runs": len(all_runs),
            "workflows": [
                {"name": k, **v} for k, v in sorted(wf_summary.items(), key=lambda x: -x[1]["total"])
            ],
            "event_breakdown": dict(event_counts),
        })

        # Phase 4: run each analyzer (threaded for real-time progress)
        results = {}
        all_energy = []

        for key, AnalyzerClass in ANALYZER_LIST:
            yield _sse("analyzer_start", {
                "key": key,
                "title": AnalyzerClass.title,
                "description": AnalyzerClass.description,
            })

            analyzer = AnalyzerClass(client)
            progress_q = queue.Queue()
            result_box = {}

            # Wire client status (rate limit, timeouts) into progress stream
            client.on_status = lambda msg: progress_q.put(msg)

            def worker(a, o, r, runs, k, ds, q, rb):
                try:
                    cb = lambda msg: q.put(msg)
                    kwargs = {"progress_cb": cb}
                    if k in ("external_deps", "rate_limit_token"): #only for these two analyzers for now, we can add more if needed?
                        kwargs["deep_scan"] = ds
                    if k == "flakiness":
                        kwargs["all_events"] = all_events
                    rb["result"] = a.analyze(o, r, runs, **kwargs)
                except Exception as e:
                    q.put(f"Analyzer error: {e}")
                    rb["error"] = e
                    rb["tb"] = traceback.format_exc()
                q.put(None)  # sentinel

            t = threading.Thread(
                target=worker,
                args=(analyzer, owner, repo, all_runs, key, deep_scan, progress_q, result_box),
                daemon=True,
            )
            t.start()

            # Poll with short timeout so we detect if thread died without sentinel
            while True:
                try:
                    msg = progress_q.get(timeout=2)
                    if msg is None:
                        break
                    yield _sse("analyzer_progress", {"key": key, "msg": msg})
                except queue.Empty:
                    if not t.is_alive():
                        break  # thread died without sentinel
                    continue  # thread still working, keep waiting

            t.join(timeout=5)

            if "error" in result_box:
                result = {"error": str(result_box["error"]), "traceback": result_box.get("tb", "")}
            else:
                result = result_box.get("result", {"error": "No result"})

            results[key] = result
            ew = result.get("energy_waste", {})
            if ew.get("total_energy_kwh"):
                all_energy.append(ew)

            # ── Extract only the run IDs actually in the detail structures ──
            if "error" not in result:
                flagged_ids = _extract_flagged_run_ids(result)
                result["flagged_run_ids"] = sorted(flagged_ids)

            yield _sse("analyzer_complete", {"key": key, "result": result})

        # Phase 5: grand total
        # Categorized waste (from analyzers)
        grand_total = {
            "total_energy_kwh": round(sum(e.get("total_energy_kwh", 0) for e in all_energy), 6),
            "total_carbon_grams_co2": round(sum(e.get("total_carbon_grams_co2", 0) for e in all_energy), 3),
            "total_cost_usd": round(sum(e.get("total_cost_usd", 0) for e in all_energy), 4),
            "total_wasted_minutes": round(sum(e.get("total_duration_minutes", 0) for e in all_energy), 2),
        }

        # Total cost of ALL failures (baseline context)
        all_failed = [r for r in all_runs if r.get("conclusion") == "failure"]
        all_success = [r for r in all_runs if r.get("conclusion") == "success"]
        fail_estimates = [
            estimate_energy(run_duration(r), "linux")
            for r in all_failed if run_duration(r) > 0
        ]
        fail_totals = aggregate_estimates(fail_estimates) if fail_estimates else {}
        success_estimates = [
            estimate_energy(run_duration(r), "linux")
            for r in all_success if run_duration(r) > 0
        ]
        success_totals = aggregate_estimates(success_estimates) if success_estimates else {}

        # Impact comparisons for all failures
        fail_impact = compute_impact(
            fail_totals.get("total_energy_kwh", 0),
            fail_totals.get("total_carbon_grams_co2", 0),
        )

        failure_rate = round(len(all_failed) / len(all_runs) * 100, 1) if all_runs else 0


        # Uncategorized failure breakdown
        failure_by_workflow = defaultdict(int)
        for r in all_failed:
            failure_by_workflow[r.get("name", "unknown")] += 1
        top_failing_workflows = sorted(failure_by_workflow.items(), key=lambda x: -x[1])[:10]

        yield _sse("complete", {
            "repo": f"{owner}/{repo}",
            "total_runs": len(all_runs),
            "total_failed": len(all_failed),
            "total_success": len(all_success),
            "failure_rate": failure_rate,
            "grand_total": grand_total,
            "all_failures": {
                "count": len(all_failed),
                "total_energy_kwh": fail_totals.get("total_energy_kwh", 0),
                "total_energy_wh": fail_totals.get("total_energy_wh", 0),
                "total_energy_joules": fail_totals.get("total_energy_joules", 0),
                "total_carbon_grams_co2": fail_totals.get("total_carbon_grams_co2", 0),
                "total_carbon_grams_co2_lower": fail_totals.get("total_carbon_grams_co2_lower", 0),
                "total_carbon_grams_co2_upper": fail_totals.get("total_carbon_grams_co2_upper", 0),
                "total_cost_usd": fail_totals.get("total_cost_usd", 0),
                "total_duration_minutes": fail_totals.get("total_duration_minutes", 0),
                "methodology": fail_totals.get("methodology", {}),
            },
            "all_successes": {
                "count": len(all_success),
                "total_energy_kwh": success_totals.get("total_energy_kwh", 0),
                "total_cost_usd": success_totals.get("total_cost_usd", 0),
                "total_duration_minutes": success_totals.get("total_duration_minutes", 0),
            },
            "impact": fail_impact,
            "carbon_intensity_g_per_kwh": CARBON_INTENSITY_G_PER_KWH,
            "top_failing_workflows": [{"name": n, "failures": c} for n, c in top_failing_workflows],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/diagnose", methods=["POST"])
def diagnose_failure():
    """Use Gemini Flash to diagnose a failed workflow run from its logs."""
    body = request.get_json(force=True)
    repo_url = body.get("repo_url", "").strip()
    run_id = body.get("run_id")
    job_id = body.get("job_id")
    gemini_key = body.get("gemini_key", "").strip()
    github_token = body.get("github_token") or os.getenv("GITHUB_TOKEN") or None
    analyzer_context = body.get("analyzer_context")

    if not gemini_key:
        return jsonify({"error": "Gemini API key is required"}), 400
    if not repo_url:
        return jsonify({"error": "Missing repo_url"}), 400
    if not run_id and not job_id:
        return jsonify({"error": "Provide run_id or job_id"}), 400

    client = GitHubClient(token=github_token)
    try:
        owner, repo = client.parse_repo(repo_url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Fetch logs
    log_text = ""
    try:
        if job_id:
            snippet = client.get_job_log_snippet(owner, repo, int(job_id), tail_lines=150)
            if snippet:
                log_text = snippet.get("log_preview", "")
        elif run_id:
            logs = client.download_run_logs(owner, repo, int(run_id))
            # Concatenate all log files, keep last 300 lines total
            all_lines = []
            for fname, content in logs.items():
                all_lines.append(f"--- {fname} ---")
                all_lines.extend(content.splitlines()[-60:])
            log_text = "\n".join(all_lines[-300:])
    except Exception as e:
        return jsonify({"error": f"Failed to fetch logs: {e}"}), 502

    if not log_text.strip():
        return jsonify({"error": "No log content found (logs may have expired)"}), 404
    workflow_yaml_block = ""
    try:
        if run_id:
            run_data = client._get_json(f"/repos/{owner}/{repo}/actions/runs/{int(run_id)}")
            wf_path = run_data.get("path", "")
            if wf_path:
                raw_yaml = client.get_workflow_file(owner, repo, wf_path)
                if raw_yaml:
                    # Truncate if massive
                    workflow_yaml_block = raw_yaml[:4000]
    except Exception:
        pass  # non-critical, just skip
    # Build analyzer context block for the prompt
    context_block = ""
    if analyzer_context:
        ctx_parts = [f"Analyzer: {analyzer_context.get('title', 'Unknown')}"]
        ctx_parts.append(f"What it detects: {analyzer_context.get('description', '')}")
        if analyzer_context.get("summary"):
            ctx_parts.append(f"Metrics: {json.dumps(analyzer_context['summary'], indent=2)}")
        if analyzer_context.get("energy_waste"):
            ctx_parts.append(f"Energy waste: {json.dumps(analyzer_context['energy_waste'], indent=2)}")
        if analyzer_context.get("recommendations"):
            ctx_parts.append(
                "Existing recommendations:\n"
                + "\n".join(f"  - {r}" for r in analyzer_context["recommendations"])
            )
        context_block = "\n".join(ctx_parts)

    # Send to Gemini
    import requests as req
    gemini_url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={gemini_key}"
    )

    prompt = (
        "You are a CI/CD expert. Analyze this GitHub Actions workflow log from a FAILED run. "
    )

    if context_block:
        prompt += (
            "This run was flagged by our Green CI analyzer with the following context:\n\n"
            f"{context_block}\n\n"
            "Use this context to provide a more targeted diagnosis that connects "
            "the log evidence to the waste pattern detected. "
        )
    if workflow_yaml_block:
        prompt += (
            "Here is the workflow YAML file that defines this pipeline:\n\n"
            f"```yaml\n{workflow_yaml_block}\n```\n\n"
            "Reference specific lines or configuration issues from this YAML in your diagnosis. "
        )
    prompt += (
        "Give a concise diagnosis:\n"
        "1. **Root cause** — what specifically failed and why\n"
        "2. **Fix** — concrete steps to resolve it\n"
        "3. **Prevention** — how to prevent this in the future\n"
        "Keep it short and actionable. Here are the logs:\n\n"
        f"```\n{log_text[:12000]}\n```"
    )

    try:
        resp = req.post(gemini_url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.3},
        }, timeout=30)
        print(f"Gemini response status: {resp.status_code}, content: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        answer = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                answer += part.get("text", "")
        if not answer:
            return jsonify({"error": "Empty response from Gemini"}), 502
        return jsonify({"diagnosis": answer})
    except req.exceptions.HTTPError as e:
        return jsonify({"error": f"Gemini API error: {e.response.status_code} {e.response.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": f"Gemini request failed: {e}"}), 502


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
