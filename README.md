# EcoCI-Waste-Analyzer

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

#### Option 1: Environment Variable (Recommended)
1. Create a `.env` file in the `backend/` directory:
   ```bash
   GITHUB_TOKEN=ghp_your_token_here
   ```
2. Make sure `.env` is in `.gitignore` 
3. Load it in your Python code with `python-dotenv`

#### Option 2: Request Body
Include the token in the JSON payload when calling the API:
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