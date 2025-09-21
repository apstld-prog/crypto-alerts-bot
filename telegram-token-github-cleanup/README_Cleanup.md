# GitHub Secret Cleanup Toolkit (Telegram Token)

## What this contains
- `scripts/bfg_cleanup.ps1` — Windows PowerShell script that uses **BFG** to scrub leaked tokens from the entire Git history.
- `scripts/filter_repo_cleanup.ps1` — Alternative script using **git-filter-repo** (Python).
- `templates/.env.example` — Example env file (copy to `.env`, never commit).
- `templates/.gitignore` — Add to your repo to keep `.env` out of Git.
- `hooks/pre-commit` — Local Git hook to prevent committing Telegram-like tokens.

## Quick start (BFG, easiest)
1. Revoke your old token in Telegram **@BotFather** and get a NEW one.
2. Open **PowerShell**, then run:

```powershell
# Example:
#   - Replace REPO with your repo URL
#   - Replace OLD123:ABC with a unique piece of your old leaked token
.\scriptsfg_cleanup.ps1 -RepoUrl "https://github.com/USERNAME/crypto-alerts-bot.git" -OldTokenPart "OLD123:ABC"
```

This will:
- Mirror-clone your repo
- Download BFG
- Replace any Telegram-like tokens and the exact old token piece with `REDACTED`
- Force-push the cleaned history

3. Update your deployment (Render/Heroku/etc.) environment variables with the **NEW** token.
4. Ask collaborators to **fresh-clone**.

## Add safety
- Copy `templates/.gitignore` into your repo root (merge if needed).
- Copy `templates/.env.example` and then create your local `.env` from it.
- Install the pre-commit hook:

```bash
cp hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

