# GCI Wasteful CI Analyzer

GCI Wasteful CI Analyzer helps you inspect GitHub Actions usage and spot CI waste that burns time, energy, and money. The project analyzes workflow runs, estimates impact, and highlights patterns such as flaky runs, zombie schedules, dependency-related failures, inefficient triggers, and cascading workflow problems.

It includes:
- A Flask backend that fetches workflow run data and runs analyzers
- A React frontend for entering a repository, date range, and GitHub token
- A pluggable analyzer structure in `backend/analyzers/` so contributors can add new detection logic

## Features

- Analyze GitHub Actions workflow runs for a repository over a date range
- Stream progress live while analysis is running
- Estimate CI energy use, carbon impact, and related waste
- Surface several categories of waste through separate analyzers
- Export analysis output as JSON from the frontend
- Optionally use Gemini for AI-assisted diagnosis of flagged runs

## Supported Analyzers

- `flakiness`: detects non-deterministic job failures and rerun patterns
- `zombie_scheduled`: detects scheduled workflows that appear unnecessary or stale
- `external_deps`: detects failures caused by unstable third-party services or external dependencies
- `inefficient_triggers`: detects CI runs triggered by minor or low-value changes
- `workflow_dependencies`: detects cascading failures across dependent workflows

## Repository Structure

```text
.
├── backend/
│   ├── analyzers/        # Waste analyzers live here
│   ├── app.py            # Flask API
│   ├── energy.py         # Energy estimation helpers
│   ├── github_client.py  # GitHub API wrapper
│   ├── impact.py         # Impact comparison helpers
│   └── requirements.txt
├── frontend/
│   ├── src/              # React frontend
│   ├── package.json
│   └── vite.config.js
└── README.md
```

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 18+ and npm
- A GitHub Personal Access Token for higher API limits

### 1. Clone the repository

```bash
git clone https://github.com/<your-org-or-user>/GCI_Wasteful-CI-Analyzer.git
cd GCI_Wasteful-CI-Analyzer
```

### 2. Set up the backend

Create your own virtual environment before installing Python dependencies.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start the backend:

```bash
python app.py
```

The backend runs at `http://localhost:5001`.

### 3. Set up the frontend

In a new terminal:

```bash
cd frontend
npm install
npm run dev
```

The frontend runs at `http://localhost:3000`.

## How to Use

1. Open the frontend in your browser.
2. Paste a GitHub repository URL such as `https://github.com/owner/repo`.
3. Choose a start date and end date.
4. Paste your GitHub Personal Access Token into the GitHub token field in the UI.
5. Click `Analyze`.

The frontend sends the token directly in the request body to the backend. You do not need to create a `.env` file for the GitHub token to use the app normally.

## GitHub Token

A token is strongly recommended because GitHub's unauthenticated API limit is low and larger repositories can fail or return incomplete results without one.

### Why you need it

- Without a token: 60 API requests per hour
- With a token: typically 5,000 API requests per hour

### Where to create one

Use one of GitHub's official token flows:
- Fine-grained personal access tokens: <https://github.com/settings/personal-access-tokens>
- Classic personal access tokens: <https://github.com/settings/tokens>
- GitHub documentation: <https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token>

### Recommended access

For public repositories, a token with read-only repository access is usually enough.

For private repositories, make sure the token can read repository contents and workflow metadata for the repositories you want to analyze.

Paste the token into the frontend when you run an analysis. Do not commit tokens to the repository.

## Optional AI Diagnosis

The frontend also accepts a Gemini API key for optional AI-assisted diagnosis of flagged runs. This is not required for the main CI waste analysis.

## Known Limitations

- The project currently targets GitHub Actions repositories, not other CI platforms
- Large repositories can hit GitHub API limits, especially without a token
- The current backend fetch path is capped to 3 pages of workflow runs per analysis request, so very active repositories may be only partially analyzed
- Deep scan depends on workflow log availability and may be disabled automatically when API budget is too low
- Some findings are heuristic and should be reviewed before making CI policy changes
- AI diagnosis is optional and requires a valid Gemini API key

## API

### Main endpoints

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/api/analyze/stream` | Run streaming analysis with progress events |
| `POST` | `/api/diagnose` | Run optional AI diagnosis for flagged workflow runs |
| `GET` | `/api/health` | Health check |
| `GET` | `/` | Basic API info |

### Example request

```bash
curl -X POST http://localhost:5001/api/analyze/stream \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/owner/repo",
    "start_date": "2026-01-01",
    "end_date": "2026-03-31",
    "github_token": "github_pat_or_ghp_token_here",
    "deep_scan": true
  }'
```

`/api/analyze/stream` accepts `repo_url` and can also accept `github_token`, `start_date`, `end_date`, `deep_scan`, and `all_events`.

The current backend fetches up to 3 pages of workflow runs per request, or about 300 runs. That limit is a backend implementation choice and can be changed later if needed.

## Request Flow

When a user clicks `Analyze`, the request moves through these files:

1. In `frontend/src/App.jsx`, `startAnalysis()` sends a `POST` request to `http://localhost:5001/api/analyze/stream`.
2. The JSON body includes `repo_url`, `start_date`, `end_date`, `github_token`, and `deep_scan`. The frontend then starts reading the streamed response body line by line.
3. In `backend/app.py`, `analyze_stream()` parses that JSON, validates `repo_url`, and creates a `GitHubClient(token=token)`.
4. `GitHubClient` in `backend/github_client.py` parses the repository identifier with `parse_repo()` and uses `get_rate_limit()` and `get_workflow_runs_paged()` to talk to the GitHub REST API.
5. While workflow runs are being fetched, `backend/app.py` emits SSE messages such as `connected`, `runs_page`, `warning`, and `runs_complete`. In `frontend/src/App.jsx`, `handleEvent()` consumes those events and updates the progress UI.
6. Once all runs are collected, `backend/app.py` loops over `ANALYZER_LIST`. For each analyzer it creates an analyzer instance with the shared `GitHubClient`, starts a worker thread, and streams `analyzer_start`, `analyzer_progress`, and `analyzer_complete` events back to the frontend.
7. Each analyzer in `backend/analyzers/` inspects the same fetched run set and may make extra GitHub API calls through the shared `GitHubClient` for details such as logs, workflow files, jobs, reruns, or related workflow metadata.
8. Each analyzer returns a structured result dictionary. In practice, the backend expects keys such as `summary`, `energy_waste`, and `recommendations`, and some analyzers also return richer fields such as `frontend_summary` or detailed findings.
9. After all analyzers finish, `backend/app.py` computes aggregate totals. It uses `estimate_energy()` and `aggregate_estimates()` from `backend/energy.py` to estimate energy, carbon, runtime, and cost. It then uses `compute_impact()` from `backend/impact.py` to turn those totals into higher-level impact.
10. Finally, `backend/app.py` emits a `complete` SSE event. In `frontend/src/App.jsx`, `handleEvent()` stores that payload in state and the UI renders the report, analyzer cards, totals, and exportable JSON.

### What `GitHubClient` does

`backend/github_client.py` is the project’s wrapper around the GitHub REST API. It is responsible for:

- authenticating requests when a token is provided
- parsing repository URLs or `owner/repo` strings
- fetching workflow runs, jobs, workflow files, and logs
- paginating GitHub API responses
- tracking rate-limit state and surfacing rate-limit problems
- reusing ETag-cached responses when possible to reduce unnecessary API usage

The key reason it exists is to keep GitHub-specific concerns out of the analyzers and out of the Flask route. The route and analyzers ask for data; `GitHubClient` handles how that data is fetched safely and efficiently.

### How analyzers fit in

Each analyzer focuses on one type of CI waste. The backend gives each analyzer the same shared inputs:

- a configured `GitHubClient`
- the repository owner and name
- the list of workflow runs already fetched for the request
- optional per-analyzer flags when supported by the route logic, such as `deep_scan` for `external_deps` and `all_events` for `flakiness`

That keeps analyzer logic separate from request handling. Contributors can usually add a new analyzer without changing the frontend request flow, as long as they register it in `backend/app.py` and return a compatible result structure.

### How diagnosis differs from analysis

The `/api/diagnose` route is separate from the main analysis flow. It is used after analysis when a user wants AI help interpreting a specific failed run or job. In that route, the backend:

- receives a repo, run or job identifier, Gemini API key, and optional GitHub token
- uses `GitHubClient` to fetch logs and workflow context
- sends that context to Gemini
- returns an AI-generated diagnosis to the frontend

Unlike `/api/analyze/stream`, `/api/diagnose` is a normal JSON request-response route, not an SSE stream.

## Contributing

Contributions are welcome. The easiest way to contribute is to add or improve an analyzer.

### Ways to contribute

- Add a new analyzer in `backend/analyzers/`
- Improve existing analyzer logic or recommendations
- Improve the frontend visualizations or UX
- Improve energy, cost, or impact estimation logic
- Add tests, docs, or example datasets
- Fix bugs or cleanup project structure

### Adding a new analyzer

A good contribution path is to create your own analyzer module inside `backend/analyzers/` and wire it into the backend so it appears in the analysis flow.

General process:
1. Add a new analyzer file in `backend/analyzers/`.
2. Export it from `backend/analyzers/__init__.py` if needed.
3. Register it in `ANALYZER_LIST` inside `backend/app.py`.
4. Return a result object that is consistent with the existing analyzer output shape.
5. Test it against one or more repositories.

### Analyzer output shape

To fit into the current backend and frontend flow, new analyzers should generally return a dictionary with fields such as:

- `analyzer`: analyzer key
- `summary`: headline metrics for the analyzer
- `energy_waste`: estimated waste for flagged runs
- `recommendations`: suggested fixes or next steps

Some analyzers also include richer fields such as `frontend_summary`, `details`, or run-specific findings. If you add custom output, keep the structure consistent and easy for the frontend to render.

Before opening a PR:
- Create and use your own virtual environment in `backend/`
- Install dependencies with `pip install -r requirements.txt`
- Run the backend and frontend locally
- Make sure your change does not break the existing analyzers

### Contribution workflow

1. Fork the repository.
2. Create a feature branch.
3. Make your changes.
4. Test locally.
5. Open a pull request with a clear description of what changed and why.

## Development Notes

- The backend currently supports passing `github_token` in request payloads.
- The frontend is currently configured to call the backend at `http://localhost:5001`.
- The Vite dev server runs on port `3000`.

## Troubleshooting

- If analysis fails quickly, verify that the repository URL is a full GitHub URL such as `https://github.com/owner/repo`
- If you see rate-limit issues, use a GitHub token in the frontend instead of making unauthenticated requests
- If results seem incomplete on a very active repository, remember that the current backend fetch path is capped at 3 pages of workflow runs unless that limit is changed in code
- If deep scan does not run, the backend may have disabled it because too few GitHub API calls remained
- If AI diagnosis fails, check that your Gemini API key is valid and that logs for the selected run still exist
- If the frontend cannot connect, make sure the backend is running on `http://localhost:5001`

## Security

- Never commit API keys or access tokens
- Prefer least-privilege GitHub tokens
- Use local environment isolation such as a Python virtual environment

## License

No license file is currently included in this repository. If you want outside contributors to reuse or distribute the project under clear terms, add a LICENSE file.
