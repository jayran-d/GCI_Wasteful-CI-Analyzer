"""
GCI i.e. Green CI Analyzer
Flask application with streaming analysis endpoint.

Routes:
  GET  /                          → Web UI
  POST /api/analyze/stream        → SSE streaming analysis
  POST /api/analyze               → Batch analysis (original)
  POST /api/analyze/<key>         → Single analyzer
  GET  /api/health                → Health check
"""

import json
import queue
import threading
import traceback
from collections import defaultdict
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from datetime import datetime, timezone
from github_client import GitHubClient
from energy import estimate_energy, aggregate_estimates, detect_runner_type
from impact import compute_impact
from utils import run_duration
from analyzers import (
    FlakinessAnalyzer,
    ZombieWorkflowAnalyzer,
    ExternalDepsAnalyzer,
    InefficientTriggerAnalyzer,
    RateLimitAnalyzer,
)

app = Flask(__name__)
CORS(app)

ANALYZER_LIST = [
    ("flakiness", FlakinessAnalyzer),
    ("zombie_scheduled", ZombieWorkflowAnalyzer),
    ("external_deps", ExternalDepsAnalyzer),
    ("inefficient_triggers", InefficientTriggerAnalyzer),
    ("rate_limit_token", RateLimitAnalyzer),
] #TENTATIVE, let's start with these 5 as discussed, we might have to change one or 2 TODO
ANALYZER_MAP = dict(ANALYZER_LIST)


def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


@app.route("/")
def index():
    return jsonify({"service": "GCI API", "docs": "POST /api/analyze/stream or /api/analyze"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/analyze/stream", methods=["POST"])
def analyze_stream():
    """Stream analysis progress via SSE over POST."""
    body = request.get_json(force=True)
    repo_url = body.get("repo_url", "").strip()
    token = body.get("github_token") or None
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    deep_scan = body.get("deep_scan", False)

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
                owner, repo, created=created, max_pages=10
            ):
                all_runs.extend(page_runs)
                yield _sse("runs_page", {
                    "page": page_num,
                    "page_size": len(page_runs),
                    "fetched_so_far": len(all_runs),
                    "total_available": total_count,
                    "rate_remaining": client.rate_remaining,
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
            estimate_energy(run_duration(r), detect_runner_type(r.get("labels")))
            for r in all_failed if run_duration(r) > 0
        ]
        fail_totals = aggregate_estimates(fail_estimates) if fail_estimates else {}
        success_estimates = [
            estimate_energy(run_duration(r), detect_runner_type(r.get("labels")))
            for r in all_success if run_duration(r) > 0
        ]
        success_totals = aggregate_estimates(success_estimates) if success_estimates else {}

        # Impact comparisons for all failures
        fail_impact = compute_impact(
            fail_totals.get("total_energy_kwh", 0),
            fail_totals.get("total_carbon_grams_co2", 0),
        )

        # Uncategorized failure breakdown i.e. what are the failures actually from?
        # Group by workflow name and show conclusion counts
        failure_by_workflow = defaultdict(int)
        for r in all_failed:
            failure_by_workflow[r.get("name", "unknown")] += 1
        top_failing_workflows = sorted(failure_by_workflow.items(), key=lambda x: -x[1])[:10]

        yield _sse("complete", {
            "repo": f"{owner}/{repo}",
            "total_runs": len(all_runs),
            "total_failed": len(all_failed),
            "total_success": len(all_success),
            "failure_rate": round(len(all_failed) / len(all_runs) * 100, 1) if all_runs else 0,
            "grand_total": grand_total,
            "all_failures": {
                "count": len(all_failed),
                "total_energy_kwh": fail_totals.get("total_energy_kwh", 0),
                "total_energy_wh": fail_totals.get("total_energy_wh", 0),
                "total_energy_joules": fail_totals.get("total_energy_joules", 0),
                "total_carbon_grams_co2": fail_totals.get("total_carbon_grams_co2", 0),
                "total_cost_usd": fail_totals.get("total_cost_usd", 0),
                "total_duration_minutes": fail_totals.get("total_duration_minutes", 0),
            },
            "all_successes": {
                "count": len(all_success),
                "total_energy_kwh": success_totals.get("total_energy_kwh", 0),
                "total_cost_usd": success_totals.get("total_cost_usd", 0),
                "total_duration_minutes": success_totals.get("total_duration_minutes", 0),
            },
            "impact": fail_impact,
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


# # keep the original batch endpoint for API consumers ? DECIDE TODO
# @app.route("/api/analyze", methods=["POST"])
# def analyze_full():
#     body = request.get_json(force=True)
#     repo_url = body.get("repo_url", "").strip()
#     if not repo_url:
#         return jsonify({"error": "Missing repo_url"}), 400

#     token = body.get("github_token") or None
#     start_date = body.get("start_date")
#     end_date = body.get("end_date")
#     deep_scan = body.get("deep_scan", False)

#     client = GitHubClient(token=token)
#     try:
#         owner, repo = client.parse_repo(repo_url)
#     except ValueError as e:
#         return jsonify({"error": str(e)}), 400

#     created = None
#     if start_date and end_date:
#         created = f"{start_date}..{end_date}"
#     elif start_date:
#         created = f">={start_date}"
#     elif end_date:
#         created = f"<={end_date}"

#     try:
#         runs = client.get_workflow_runs(owner, repo, created=created, max_pages=10)
#     except Exception as e:
#         return jsonify({"error": f"GitHub API error: {e}"}), 502

#     if not runs:
#         return jsonify({"repo": f"{owner}/{repo}", "warning": "No workflow runs found.", "analyzers": {}})

#     results = {}
#     all_energy = []
#     for key, AnalyzerClass in ANALYZER_LIST:
#         analyzer = AnalyzerClass(client)
#         try:
#             if key in ("external_deps", "rate_limit_token"):
#                 result = analyzer.analyze(owner, repo, runs, deep_scan=deep_scan)
#             else:
#                 result = analyzer.analyze(owner, repo, runs)
#             results[key] = result
#             ew = result.get("energy_waste", {})
#             if ew.get("total_energy_kwh"):
#                 all_energy.append(ew)
#         except Exception as e:
#             results[key] = {"error": str(e)}

#     grand_total = {
#         "total_energy_kwh": round(sum(e.get("total_energy_kwh", 0) for e in all_energy), 6),
#         "total_carbon_grams_co2": round(sum(e.get("total_carbon_grams_co2", 0) for e in all_energy), 3),
#         "total_cost_usd": round(sum(e.get("total_cost_usd", 0) for e in all_energy), 4),
#         "total_wasted_minutes": round(sum(e.get("total_duration_minutes", 0) for e in all_energy), 2),
#     }

#     return jsonify({
#         "repo": f"{owner}/{repo}",
#         "date_range": {"start": start_date, "end": end_date},
#         "total_runs_fetched": len(runs),
#         "grand_total_waste": grand_total,
#         "analyzers": results,
#         "generated_at": datetime.now(timezone.utc).isoformat(),
#     })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
