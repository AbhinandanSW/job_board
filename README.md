# Job Tracker

A personal job-opening tracker that runs free on GitHub Actions. Every day it pulls openings from public job
APIs (and optionally LinkedIn/Naukri via Apify), keeps only roles matching **your** title, skills, experience
and location, and publishes them to `JOBS.md` and a small dashboard website (Save / Applied / Hide tracking). Both list jobs **day by day**, newest first.

## Setup (about 10 minutes)

1. **Fork** this repo, or create a new **public** repo and upload these files (public is needed for free GitHub Pages).
2. **Enable Actions:** open the *Actions* tab and click the green button. Forks start with workflows disabled.
3. **Edit `job_tracker/config.yml`, section 1:** search terms, title patterns, skills, experience years, locations.
   In section 2, swap the example companies for ones you care about.
4. **(Optional) LinkedIn + Naukri:** create an [Apify](https://apify.com) account, copy your API token
   (Settings → Integrations), and add it to your fork under *Settings → Secrets and variables → Actions →
   New repository secret* named `APIFY_TOKEN`. No token? Apify is skipped and everything else still works.
5. **Dashboard:** *Settings → Pages → Deploy from a branch → `main` → `/docs`*.
6. **First run:** *Actions → Job opening tracker → Run workflow*. After it finishes, open `JOBS.md` or your Pages URL.

It then runs daily at 03:30 UTC (change the `cron` line in `.github/workflows/job-tracker.yml`).

## What it costs

- Free sources (Greenhouse, Lever, Ashby, Remotive, Remote OK) and GitHub Actions: free.
- Apify (optional): about $0.005 per job returned. With the defaults (3 search terms x 2 sites x 20 results,
  every 2 days) that is at most $0.60 per run, roughly $9/month; Apify's free plan includes $5/month.
  `max_charge_usd` caps each run. Tune `search_terms`, `maxResults` and `min_interval_days`.

## Customising

| I want to... | Change |
|---|---|
| Track a different role (e.g. Python backend) | `search_terms`, `title_include`, `title_exclude`, `skills`, `must_have_any_skill` |
| Match my experience | `experience.min` / `experience.max` |
| Only certain cities or remote | `location_mode: filter` and `locations` |
| Add companies | `sources.greenhouse / lever / ashby` (slug from the company's careers URL) |
| Run less often / cheaper | `cron` in the workflow, `apify.min_interval_days`, `maxResults` |

## Good to know

- **No secrets in the repo.** The only token is `APIFY_TOKEN`, stored as a GitHub secret.
- **Public by default:** a public fork exposes `JOBS.md`, the dashboard and your config. Your Save/Applied/Hide
  choices stay in your browser only (not synced across devices).
- GitHub pauses scheduled workflows after ~60 days without repo activity; re-enable them in the Actions tab.
- Only public job APIs are used. LinkedIn/Naukri come through a third-party Apify Actor, which is subject to
  those sites' terms and can break when they change. Check the Actor's page before relying on it.
- Remotive and Remote OK ask for attribution; the report footer provides it. Remotive allows ~4 requests/day.
- If a company slug is wrong, the run log shows a warning and that company is skipped.
