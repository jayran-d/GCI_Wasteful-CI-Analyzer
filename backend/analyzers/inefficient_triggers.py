from energy import estimate_energy, aggregate_estimates, detect_runner_type
from utils import run_duration


class InefficientTriggerAnalyzer:
    key = "inefficient_triggers"
    title = "Inefficient CI Triggering for Minor Changes"
    description = (
        "Detects likely unnecessary CI runs triggered by docs-only or other "
        "non-functional changes."
    )

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, deep_scan=True, progress_cb=None):
        ci_runs = [
            run for run in runs
            if run.get("event") in ("push", "pull_request")
        ]

        total_failed_runs = sum(1 for run in ci_runs if run.get("conclusion") == "failure")

        if progress_cb:
            progress_cb(f"Checking {len(ci_runs)} CI runs for docs-only changes...")

        inefficient_detail = []
        energy_estimates = []
        sha_cache = {}

        for index, run in enumerate(ci_runs, start=1):
            if run.get("conclusion") != "success":
                continue

            workflow_name = run.get("name", "")
            if not self._is_heavy_workflow(workflow_name):
                continue

            sha = run.get("head_sha")
            if not sha:
                continue

            if sha not in sha_cache:
                if progress_cb:
                    progress_cb(f"Fetching changed files for commit {sha[:8]}... [{index}/{len(ci_runs)}]")
                sha_cache[sha] = self.client.get_commit_files(owner, repo, sha)

            changed_files = sha_cache[sha]
            if not self._is_non_functional(changed_files):
                continue

            inefficient_detail.append({
                "run_id": run["id"],
                "workflow": workflow_name,
                "sha": sha[:8],
                "changed_files": changed_files,
                "trigger_paths": [],
                "reason": "Heavy CI workflow ran even though the commit only changed docs or other non-functional files.",
            })

            duration_seconds = run_duration(run)
            if duration_seconds > 0:
                energy_estimates.append(
                    estimate_energy(
                        duration_seconds,
                        detect_runner_type(run.get("labels", []))
                    )
                )

        inefficient_count = len(inefficient_detail)
        waste_percentage = round((inefficient_count / len(ci_runs)) * 100, 1) if ci_runs else 0

        if progress_cb:
            progress_cb(f"Found {inefficient_count} likely inefficient run(s).")

        return {
            "analyzer": self.key,
            "title": self.title,
            "summary": {
                "total_runs_analyzed": len(ci_runs),
                "total_failed_runs": total_failed_runs,
                "inefficient_run_count": inefficient_count,
                "waste_percentage": waste_percentage,
            },
            "energy_waste": aggregate_estimates(energy_estimates),
            "inefficient_runs": {
                "count": inefficient_count,
                "detail": inefficient_detail[:15],
            },
            "recommendations": self._build_recommendations(inefficient_count),
        }

    def _is_non_functional(self, files):
        if not files:
            return False

        doc_filenames = {
            "readme",
            "license",
            "changelog",
            "contributing",
            "code_of_conduct",
            "authors",
        }

        image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico")

        for file_path in files:
            lowered = file_path.lower()
            basename = lowered.split("/")[-1]

            if lowered.startswith("docs/") or lowered.startswith("documentation/"):
                continue

            if lowered.endswith(".md") or lowered.endswith(".rst") or lowered.endswith(".adoc"):
                continue

            if lowered.endswith(image_extensions):
                continue

            if basename in doc_filenames:
                continue

            return False

        return True

    def _is_heavy_workflow(self, workflow_name):
        name = workflow_name.lower()

        heavy_keywords = ["build", "test", "deploy", "docker", "ci", "release"]
        light_keywords = ["lint", "check", "format", "compliance"]

        if any(keyword in name for keyword in light_keywords):
            return False

        if any(keyword in name for keyword in heavy_keywords):
            return True

        return False

    def _build_recommendations(self, inefficient_count):
        if inefficient_count == 0:
            return ["No obvious docs-only trigger inefficiencies were detected."]

        return [
            "Add path filters or paths-ignore rules so docs-only changes do not trigger full CI workflows.",
            "Split lightweight checks from expensive build, test, or deploy workflows where possible.",
            "Consider using [skip ci] for trivial documentation-only updates when appropriate.",
        ]
