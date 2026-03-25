# EcoCI-Waste-Analyzer

Analyzes GitHub Actions workflows to identify wasteful CI practices and quantify their environmental impact (energy consumption, CO2 emissions, cost).

## How to run

```bash
# 1. Install dependencies
cd backend && pip install -r requirements.txt

# 2. Get a GitHub token (see GitHub Token Setup)
# Save it to .env file:
echo "GITHUB_TOKEN=ghp_your_token_here" > .env

# 3. Run the server
python app.py
```

API runs at: `http://localhost:5001`

## Test

The token is automatically read from the `.env` file you created in step 2. Replace `your-org/your-repo` with the repository you want to analyze. In a new terminal paste:

```bash
curl -X POST http://localhost:5001/api/analyze/stream \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/your-org/your-repo"
  }'
```

---

## GitHub Token Setup

To use the EcoCI-Waste-Analyzer, you need a GitHub Personal Access Token for authentication.

### Creating a GitHub Token

1. Go to [GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Give it a descriptive name (e.g., "EcoCI-Analyzer")
4. Select the following scopes:
   - `repo` (full control of private repositories)
   - `read:org` (read org and team membership)
5. Click **Generate token**
6. **Copy the token immediately** (you won't see it again)

### Using the Token

**Option 1: Environment Variable (Recommended)**
Save the token to `backend/.env`:
```bash
GITHUB_TOKEN=ghp_your_token_here
```

**Option 2: Pass in API Request**
Include it in the JSON payload:
```json
{
  "repo_url": "https://github.com/owner/repo",
  "github_token": "ghp_your_token_here",
  "start_date": "2024-01-01",
  "end_date": "2024-03-21"
}
```

### Rate Limits
- **Without token**: 60 API calls/hour (unauthenticated)
- **With token**: 5,000 API calls/hour (authenticated)

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/analyze/stream` | Stream analysis with real-time progress |
| GET | `/api/health` | Health check |
| GET | `/` | API info |