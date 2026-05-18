# Multi-Board Job Agent — Samiha Chowdhury

Automated job search agent that scans LinkedIn, Dice, CareerBuilder, and supporting boards every 15 minutes, scores jobs against your resume using AI, generates tailored resumes + cover letters, and auto-applies where the board exposes an internal apply flow.

---

## What it does every 15 minutes

```
Scan LinkedIn + Dice + CareerBuilder → Score jobs (0-100) → Generate tailored resume + cover letter → Auto-apply
```

| Score | Action |
|-------|--------|
| < 40  | Skip silently |
| 40–64 | Log only |
| 65–81 | Generate apply pack, send email notification (you apply manually) |
| 82–100 | Generate apply pack + **auto-apply** via Easy Apply |

---

## Quick Start

```bash
cd linkedin_job_agent
./setup.sh              # installs deps + Playwright browser

# Fill in credentials
cp .env.example .env
nano .env               # add AI key + LinkedIn/Dice/CareerBuilder creds

# Fill in your real resume details
nano resume_profile.yaml

# Start the agent
source .venv/bin/activate
python main.py
```

On first run it will restore any saved cookies in the shared Playwright context. If a board session is missing, the agent can fall back to the corresponding board credentials in `.env`.

---

## Files

```
linkedin_job_agent/
├── main.py                  ← Orchestrator + APScheduler (runs every 15 min)
├── config.yaml              ← Search keywords, thresholds, browser settings
├── resume_profile.yaml      ← Samiha's master resume profile (EDIT THIS)
├── tracker.py               ← SQLite job database
├── notifier.py              ← Console logging + email alerts
├── linkedin/
│   ├── auth.py              ← Session management (cookies)
│   ├── scanner.py           ← LinkedIn search + description extraction
│   └── applier.py           ← LinkedIn Easy Apply form filler
├── boards/
│   ├── dispatcher.py        ← Platform-aware auto-apply routing
│   ├── dice.py              ← Dice Apply Now adapter
│   ├── careerbuilder.py     ← CareerBuilder Quick Apply adapter
│   └── common.py            ← Shared board form helpers
├── ai/
│   ├── scorer.py            ← Claude relevance scoring (0-100)
│   ├── resume_writer.py     ← Claude resume tailoring
│   └── cover_letter_writer.py ← Claude cover letter generation
├── documents/
│   └── generator.py         ← .docx file creation
├── output/
│   ├── resumes/             ← Generated tailored resumes
│   └── cover_letters/       ← Generated cover letters
├── data/jobs.db             ← SQLite tracking database
└── session/                 ← LinkedIn session cookies
```

---

## Configuration

### `config.yaml` — key settings

```yaml
scoring:
  generate_pack_threshold: 65   # score needed to generate resume + cover letter
  auto_apply_threshold: 82      # score needed to auto-apply
  max_applications_per_day: 15  # safety cap

scheduler:
  scan_interval_minutes: 15     # how often to scan all configured boards
  quiet_hours_start: 23         # no scanning between 11pm–7am
  quiet_hours_end: 7

dice:
  enabled: true
  results_per_keyword: 10

careerbuilder:
  enabled: true
  results_per_keyword: 10
```

### `resume_profile.yaml` — fill in your real data

The system uses this as the source of truth for all AI generation. Fill in:
- Your real experience entries with real companies/dates/metrics
- Certifications you actually hold
- Real contact info (phone, LinkedIn URL)

---

## Resume variants

The agent selects the most relevant of Samiha's 7 existing resume files as the base template, then overlays AI-tailored content:

| Variant | Used when job title contains |
|---------|------------------------------|
| `tech_pm` | Technical Project Manager |
| `it_pm` | IT Project Manager |
| `agile_pm` | Agile |
| `senior_pm` | Senior Project Manager |
| `ops_manager` | Operations Manager |
| `program_manager` | Program Manager |
| `general_pm` | Everything else |

---

## Monitoring

View job database:
```bash
sqlite3 data/jobs.db "SELECT title, company, score, status FROM jobs ORDER BY score DESC LIMIT 20;"
```

View logs:
```bash
tail -f logs/agent.log
```

View stats:
```bash
python -c "import asyncio; from tracker import get_stats; print(asyncio.run(get_stats()))"
```

---

## Email Notifications

Fill in `.env`:
```
NOTIFY_EMAIL=your@email.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASS=your_gmail_app_password   # Gmail → Settings → App Passwords
```

You'll get an email when:
- A pack is generated (score 65–81) — you apply manually
- An auto-application fires (score 82+)

---

## Safety limits

- Max 15 applications/day (configurable)
- Max 3 applications/run
- Quiet hours: no scanning 11pm–7am
- Human-like delays between all actions (1.5–4 seconds)
- Session cookies (not password) used after first login
- LinkedIn Easy Apply only — no external form navigation

---

## Notes

- LinkedIn, Dice, and CareerBuilder all rate-limit aggressive automation — the 15-minute interval and human-like delays are designed to keep the browser behaviour conservative
- If a board presents a security challenge or unexpected login wall, that application will fall back to failed/manual-needed for that cycle
- The agent never applies to the same job twice (SQLite dedup)
- All generated files are stored locally — nothing is uploaded anywhere
