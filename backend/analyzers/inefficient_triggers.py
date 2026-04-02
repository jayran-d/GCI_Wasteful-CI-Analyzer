from energy import estimate_energy, aggregate_estimates, detect_runner_type
from utils import run_duration


class InefficientTriggerAnalyzer:
    key = "inefficient_triggers"
    title = "Inefficient CI Triggering for Minor Changes"
    description = (
        "Detects likely unnecessary CI runs triggered by docs-only or other "
        "non-functional changes.")

    def __init__(self, client):
        self.client = client

    def analyze(self, owner, repo, runs, deep_scan=True, progress_cb=None):
        ci_runs = [
            run for run in runs if run.get("event") in ("push", "pull_request")
        ]

        total_failed_runs = sum(1 for run in ci_runs
                                if run.get("conclusion") == "failure")

        if progress_cb:
            progress_cb(
                f"Checking {len(ci_runs)} CI runs for docs-only changes...")

        inefficient_detail = []
        energy_estimates = []
        sha_cache = {}
        workflow_cache = {}

        for index, run in enumerate(ci_runs, start=1):
            if run.get("conclusion") not in {"success", "failure"}:
                continue

            workflow_name = run.get("name", "")
            workflow_path = run.get("path", "")

            print(workflow_path)
            if not self._is_heavy_workflow(workflow_name, workflow_path):
                continue

            sha = run.get("head_sha")
            if not sha:
                continue

            if sha not in sha_cache:
                if progress_cb:
                    progress_cb(
                        f"Fetching changed files for commit {sha[:8]}... [{index}/{len(ci_runs)}]"
                    )
                sha_cache[sha] = self.client.get_commit_files(owner, repo, sha)

            changed_files = sha_cache[sha]
            if not self._is_non_functional(changed_files):
                continue

            print(
                f"these are the files that we are checking: {changed_files} and the sha {sha} and the event type {run.get('event')} and this is the id {run.get('id')}"
            )

            # workflow_path = run.get("path", "")
            if workflow_path not in workflow_cache:
                workflow_cache[workflow_path] = self._get_trigger_info(
                    owner, repo, workflow_path)

            trigger_info = workflow_cache[workflow_path]
            reason = self._build_reason(trigger_info)

            inefficient_detail.append({
                "run_id": run["id"],
                "workflow": workflow_name,
                "sha": sha,
                "changed_files": changed_files,
                "trigger_paths":trigger_info["trigger_paths"],
                "reason":reason,
            })

            duration_seconds = run_duration(run)
            if duration_seconds > 0:
                energy_estimates.append(
                    estimate_energy(duration_seconds,
                                    detect_runner_type(run.get("labels", []))))

        inefficient_count = len(inefficient_detail)
        waste_percentage = round(
            (inefficient_count / len(ci_runs)) * 100, 1) if ci_runs else 0

        if progress_cb:
            progress_cb(
                f"Found {inefficient_count} likely inefficient run(s).")

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
                "detail": inefficient_detail,
            },
            "recommendations": self._build_recommendations(inefficient_detail),
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
            lowered = file_path.lower().replace("\\", "/")
            basename = lowered.split("/")[-1]

            if lowered.startswith(
                ("docs/", "documentation/", "images/", "assets/")):
                continue

            if lowered.endswith((".md", ".rst", ".adoc")):
                continue

            if lowered.endswith(image_extensions):
                continue

            if basename in doc_filenames:
                continue

            return False

        return True

    def _is_heavy_workflow(self, workflow_name, workflow_path=""):
        name = (workflow_name or "").lower()
        path = (workflow_path or "").lower()
        filename = path.split("/")[-1]
        heavy_keywords = [
            "build", "test", "deploy", "docker", "ci", "release",
            "integration", "pipeline", "compile", "gradle"
        ]
        light_keywords = ["lint", "format", "compliance"]

        searchable_text = f"{name} {filename} {path}"
        print(f"this is the searchable text: {searchable_text}")

        has_heavy = any(keyword in searchable_text
                        for keyword in heavy_keywords)
        has_light = any(keyword in searchable_text
                        for keyword in light_keywords)

        if has_heavy:
            return True

        if has_light:
            return False

        return False

    def _get_trigger_info(self, owner, repo, workflow_path):
        if not workflow_path:
            return {
                "trigger_paths": [],
                "has_path_filters": False,
                "has_paths_ignore": False,
            }

        yaml_text = self.client.get_workflow_file(owner, repo, workflow_path)
        if not yaml_text:
            return {
                "trigger_paths": [],
                "has_path_filters": False,
                "has_paths_ignore": False,
            }

        trigger_paths = []
        for line in yaml_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                value = stripped[2:].strip().strip("'\"")
                if any(token in value
                       for token in ["*", "/", ".md", "docs", "README"]):
                    trigger_paths.append(value)

        return {
            "trigger_paths": trigger_paths[:10],
            "has_path_filters": "paths:" in yaml_text,
            "has_paths_ignore": "paths-ignore:" in yaml_text,
        }

    def _build_reason(self, trigger_info):
        if not trigger_info["has_path_filters"] and not trigger_info[
                "has_paths_ignore"]:
            return (
                "This run was flagged because the workflow appears heavy, the commit only "
                "changed documentation or other non-functional files, and the workflow "
                "appears to have no path-based filtering.")

        return (
            "This run was flagged because the workflow appears heavy and the commit only "
            "changed documentation or other non-functional files. The workflow has some "
            "path-based trigger configuration, so this result is less certain."
        )

    def _build_recommendations(self, inefficient_runs):
        if not inefficient_runs:
            return [
                "No obvious docs-only trigger inefficiencies were detected."
            ]

        has_broad_triggers = any(not run["trigger_paths"]
                                 for run in inefficient_runs)

        recommendations = [
            "Split lightweight checks from expensive build, test, or deploy workflows where possible.",
            "Consider using [skip ci] for trivial documentation-only updates when appropriate.",
        ]

        if has_broad_triggers:
            recommendations.insert(
                0,
                "Add path filters or paths-ignore rules so docs-only changes do not trigger full CI workflows.",
            )

        return recommendations
