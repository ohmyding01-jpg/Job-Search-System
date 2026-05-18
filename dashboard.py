"""
Job Agent Dashboard — self-service web UI for Stephen.
Runs on http://localhost:8080

Stephen's complete workflow:
1. python3 start.py          (first time — runs setup)
2. Browser opens automatically
3. Click START — agent begins scanning + applying
4. Check dashboard for live status and applications
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

BASE = Path(__file__).parent
CANDIDATE = "stephen"
CANDIDATE_DIR = BASE / "candidates" / CANDIDATE
LOG_DIR = CANDIDATE_DIR / "data"
APPLICATIONS_LOG = BASE / "logs" / "applications.log"
DB_PATH = CANDIDATE_DIR / "data" / "jobs.db"
ENV_FILE = BASE / ".env"

app = FastAPI()

# Agent subprocess handle
_agent_proc: Optional[subprocess.Popen] = None


# ─── helpers ──────────────────────────────────────────────────────────────────

def _agent_running() -> bool:
    global _agent_proc
    if _agent_proc is None:
        return False
    return _agent_proc.poll() is None


def _read_applications() -> list[dict]:
    if not APPLICATIONS_LOG.exists():
        return []
    lines = APPLICATIONS_LOG.read_text(encoding="utf-8").strip().splitlines()
    apps = []
    for line in reversed(lines[-50:]):
        parts = line.split("|")
        if len(parts) >= 4:
            ts = parts[0].strip().lstrip("[").rstrip("]").strip()
            platform = parts[1].strip()
            score_part = parts[2].strip()
            rest = "|".join(parts[3:]).strip()
            apps.append({
                "time": ts,
                "platform": platform,
                "score": score_part,
                "job": rest.split("|")[0].strip() if "|" in rest else rest,
            })
    return apps


def _read_recent_jobs(limit: int = 20) -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT title, company, location, score, status, source_platform, apply_url "
            "FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_stats() -> dict:
    if not DB_PATH.exists():
        return {"total": 0, "applied": 0, "today_applied": 0, "today_found": 0}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        today = datetime.now().strftime("%Y-%m-%d")
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='applied'").fetchone()[0]
        today_applied = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='applied' AND applied_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]
        conn.close()
        apps_today = len([a for a in _read_applications() if today in a.get("time", "")])
        return {"total": total, "applied": applied, "today_applied": apps_today}
    except Exception:
        return {"total": 0, "applied": 0, "today_applied": 0}


def _read_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"')
    return env


def _write_env(key: str, value: str):
    env = _read_env()
    env[key] = value
    lines = [f'{k}="{v}"' for k, v in env.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── API endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/start")
async def start_agent():
    global _agent_proc
    if _agent_running():
        return {"status": "already_running"}
    env_vars = {**os.environ}
    env_vars["OPENAI_BASE_URL"] = "http://localhost:20128/v1"
    env_vars["ANTHROPIC_API_BASE"] = "http://localhost:20128/v1"
    _agent_proc = subprocess.Popen(
        [sys.executable, "run.py", "--candidate", CANDIDATE],
        cwd=str(BASE),
        env=env_vars,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return {"status": "started", "pid": _agent_proc.pid}


@app.post("/api/stop")
async def stop_agent():
    global _agent_proc
    if not _agent_running():
        return {"status": "not_running"}
    try:
        _agent_proc.send_signal(signal.SIGTERM)
        await asyncio.sleep(2)
        if _agent_running():
            _agent_proc.kill()
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    return {"status": "stopped"}


@app.get("/api/status")
async def status():
    stats = _get_stats()
    return {
        "running": _agent_running(),
        "pid": _agent_proc.pid if _agent_running() else None,
        **stats,
    }


@app.get("/api/applications")
async def applications():
    return _read_applications()


@app.get("/api/jobs")
async def jobs():
    return _read_recent_jobs()


@app.get("/api/logs")
async def logs():
    log_file = BASE / "logs" / "agent.log"
    if not log_file.exists():
        return {"lines": []}
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    return {"lines": lines[-80:]}


@app.post("/api/config/apikey")
async def save_apikey(body: dict):
    key = body.get("key", "").strip()
    if not key:
        raise HTTPException(400, "Key is empty")
    is_gemini = key.startswith("AIza")
    env_key = "GEMINI_API_KEY" if is_gemini else "OPENAI_API_KEY"
    _write_env(env_key, key)

    # Update config.yaml provider to match the key type.
    cfg_path = CANDIDATE_DIR / "config.yaml"
    if cfg_path.exists():
        content = cfg_path.read_text()
        if is_gemini:
            content = content.replace('provider: "openai"\n  model: "ag/gemini-3-flash"',
                                      'provider: "gemini"\n  model: "gemini-2.0-flash-lite"')
        else:
            content = content.replace('provider: "gemini"\n  model: "gemini-2.0-flash-lite"',
                                      'provider: "openai"\n  model: "ag/gemini-3-flash"')
        cfg_path.write_text(content, encoding="utf-8")

    os.environ[env_key] = key
    return {"status": "saved", "provider": "gemini" if is_gemini else "openai"}


@app.post("/api/config/site-credentials")
async def save_site_credentials(body: dict):
    """Save Dice or CareerBuilder credentials to .env for silent auto-login."""
    site = body.get("site", "").lower()
    email = body.get("email", "").strip()
    password = body.get("password", "").strip()
    if not site or not email or not password:
        raise HTTPException(400, "site, email and password are required")

    if site == "dice":
        _write_env("DICE_EMAIL", email)
        _write_env("DICE_PASSWORD", password)
        os.environ["DICE_EMAIL"] = email
        os.environ["DICE_PASSWORD"] = password
    elif site == "careerbuilder":
        _write_env("CAREERBUILDER_EMAIL", email)
        _write_env("CAREERBUILDER_PASSWORD", password)
        os.environ["CAREERBUILDER_EMAIL"] = email
        os.environ["CAREERBUILDER_PASSWORD"] = password
    else:
        raise HTTPException(400, f"Unknown site: {site}")
    return {"status": "saved", "site": site}


@app.get("/api/config")
async def get_config():
    env = _read_env()
    has_key = bool(
        env.get("GEMINI_API_KEY") or
        env.get("OPENAI_API_KEY") or
        env.get("ANTHROPIC_API_KEY") or
        os.getenv("OPENAI_BASE_URL")
    )
    return {"has_api_key": has_key, "candidate": CANDIDATE}


# ─── HTML dashboard ────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Agent — Stephen</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f1117; color: #e2e8f0; min-height: 100vh; }
  .header { background: #1a1d27; border-bottom: 1px solid #2d3748;
            padding: 16px 24px; display: flex; align-items: center; gap: 16px; }
  .header h1 { font-size: 1.1rem; font-weight: 600; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .dot-green { background: #48bb78; box-shadow: 0 0 8px #48bb78; }
  .dot-red   { background: #f56565; }
  .dot-gray  { background: #718096; }
  .container { max-width: 1100px; margin: 0 auto; padding: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #1a1d27; border: 1px solid #2d3748; border-radius: 10px; padding: 20px; }
  .card-label { font-size: 0.75rem; color: #718096; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
  .card-value { font-size: 2rem; font-weight: 700; color: #fff; }
  .card-value.green { color: #48bb78; }
  .section { background: #1a1d27; border: 1px solid #2d3748; border-radius: 10px; margin-bottom: 24px; }
  .section-header { padding: 16px 20px; border-bottom: 1px solid #2d3748;
                    font-weight: 600; font-size: 0.9rem; display: flex; justify-content: space-between; align-items: center; }
  .section-body { padding: 16px 20px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; padding: 8px 12px; color: #718096; font-weight: 500;
       border-bottom: 1px solid #2d3748; font-size: 0.75rem; }
  td { padding: 10px 12px; border-bottom: 1px solid #1e2233; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
  .badge-green { background: #1c4532; color: #68d391; }
  .badge-blue  { background: #1a365d; color: #63b3ed; }
  .badge-yellow{ background: #44337a; color: #d6bcfa; }
  .badge-gray  { background: #2d3748; color: #a0aec0; }
  .btn { padding: 10px 20px; border-radius: 8px; border: none; cursor: pointer;
         font-size: 0.9rem; font-weight: 600; transition: opacity 0.15s; }
  .btn:hover { opacity: 0.85; }
  .btn-green { background: #48bb78; color: #000; }
  .btn-red   { background: #f56565; color: #fff; }
  .btn-blue  { background: #4299e1; color: #fff; }
  .controls { display: flex; gap: 12px; align-items: center; }
  .alert { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 0.9rem; }
  .alert-yellow { background: #2d2a14; border: 1px solid #744210; color: #f6e05e; }
  .alert-green  { background: #1c4532; border: 1px solid #276749; color: #68d391; }
  .score-high { color: #68d391; }
  .score-mid  { color: #f6e05e; }
  .score-low  { color: #f56565; }
  .log-box { background: #0d0f18; border-radius: 6px; padding: 12px; font-family: monospace;
             font-size: 0.72rem; max-height: 300px; overflow-y: auto; color: #a0aec0; }
  .setup-box { padding: 20px; }
  .setup-box input { background: #0d0f18; border: 1px solid #4a5568; border-radius: 6px;
                     color: #e2e8f0; padding: 10px 12px; width: 100%; font-size: 0.9rem; margin: 8px 0; }
  .setup-box p { color: #718096; font-size: 0.85rem; margin: 8px 0; }
  .setup-box a { color: #63b3ed; }
  #toast { position: fixed; bottom: 24px; right: 24px; background: #48bb78; color: #000;
           padding: 12px 20px; border-radius: 8px; font-weight: 600; display: none;
           box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
</style>
</head>
<body>
<div class="header">
  <span class="status-dot dot-gray" id="agentDot"></span>
  <h1>Job Agent — Stephen Muliokela</h1>
  <div style="margin-left: auto" class="controls">
    <span id="agentLabel" style="font-size:0.85rem; color:#718096">Checking...</span>
    <button class="btn btn-green" id="startBtn" onclick="startAgent()">Start</button>
    <button class="btn btn-red"   id="stopBtn"  onclick="stopAgent()" style="display:none">Stop</button>
  </div>
</div>
<div class="container">
  <div id="setupBanner" style="display:none">
    <div class="alert alert-yellow">
      ⚠ No API key configured. The agent cannot score or tailor resumes without one.
      <a href="#setup" style="color:#f6e05e; margin-left:8px">→ Set up API key</a>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">Total Jobs Found</div>
      <div class="card-value" id="statTotal">—</div>
    </div>
    <div class="card">
      <div class="card-label">Total Applied</div>
      <div class="card-value green" id="statApplied">—</div>
    </div>
    <div class="card">
      <div class="card-label">Applied Today</div>
      <div class="card-value green" id="statToday">—</div>
    </div>
    <div class="card">
      <div class="card-label">Next Apply Cycle</div>
      <div class="card-value" id="statNext" style="font-size:1rem; padding-top:8px">—</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">✅ Successful Applications <span id="appCount" style="color:#718096; font-weight:400; font-size:0.8rem"></span></div>
    <div class="section-body">
      <table id="appsTable">
        <thead><tr><th>Time</th><th>Platform</th><th>Job</th><th>Score</th></tr></thead>
        <tbody id="appsBody"><tr><td colspan="4" style="color:#718096; text-align:center; padding:20px">No applications yet — start the agent to begin</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <div class="section-header">🔍 Recent Jobs Found</div>
    <div class="section-body">
      <table id="jobsTable">
        <thead><tr><th>Title</th><th>Company</th><th>Platform</th><th>Score</th><th>Status</th></tr></thead>
        <tbody id="jobsBody"><tr><td colspan="5" style="color:#718096; text-align:center; padding:20px">No jobs yet</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="section" id="setup">
    <div class="section-header">⚙️ Setup & Credentials</div>
    <div class="setup-box">
      <p><strong>Google AI API Key</strong> — needed for AI-tailored resumes (free, 1M tokens/day)</p>
      <p>Get one at <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a></p>
      <input type="password" id="apiKeyInput" placeholder="Paste Google AI API key (starts with AIza...)">
      <button class="btn btn-blue" onclick="saveApiKey()" style="margin-top:6px">Save API Key</button>

      <hr style="border-color:#2d3748; margin:20px 0">

      <p><strong>Dice.com Credentials</strong> — agent silently re-logs when session expires, no popups</p>
      <div style="display:flex; gap:8px; margin:6px 0">
        <input type="email" id="diceEmail" placeholder="Dice email" style="flex:1">
        <input type="password" id="dicePass" placeholder="Dice password" style="flex:1">
      </div>
      <button class="btn btn-blue" onclick="saveSiteCredentials('dice')" style="margin-top:4px">Save Dice Credentials</button>

      <p style="margin-top:14px"><strong>CareerBuilder Credentials</strong></p>
      <div style="display:flex; gap:8px; margin:6px 0">
        <input type="email" id="cbEmail" placeholder="CareerBuilder email" style="flex:1">
        <input type="password" id="cbPass" placeholder="CareerBuilder password" style="flex:1">
      </div>
      <button class="btn btn-blue" onclick="saveSiteCredentials('careerbuilder')" style="margin-top:4px">Save CareerBuilder Credentials</button>

      <p style="margin-top:12px; color:#718096; font-size:0.8rem">
        Credentials stored only in .env on this machine — never sent anywhere.
      </p>
    </div>
  </div>

  <div class="section">
    <div class="section-header">📋 Agent Log <span style="font-size:0.75rem; color:#718096; font-weight:400">(last 80 lines)</span></div>
    <div class="section-body">
      <div class="log-box" id="logBox">Loading logs...</div>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
let nextRefresh = Date.now() + 15000;
let applyCycleCountdown = 15 * 60;

function toast(msg, color='#48bb78') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.background = color; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

async function fetchStatus() {
  const r = await fetch('/api/status'); const d = await r.json();
  const dot = document.getElementById('agentDot');
  const label = document.getElementById('agentLabel');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  if (d.running) {
    dot.className = 'status-dot dot-green';
    label.textContent = `Running (PID ${d.pid})`;
    startBtn.style.display = 'none'; stopBtn.style.display = '';
  } else {
    dot.className = 'status-dot dot-red';
    label.textContent = 'Stopped';
    startBtn.style.display = ''; stopBtn.style.display = 'none';
  }
  document.getElementById('statTotal').textContent = d.total ?? '—';
  document.getElementById('statApplied').textContent = d.applied ?? '—';
  document.getElementById('statToday').textContent = d.today_applied ?? '—';
}

async function fetchApplications() {
  const r = await fetch('/api/applications'); const apps = await r.json();
  const tbody = document.getElementById('appsBody');
  document.getElementById('appCount').textContent = apps.length ? `(${apps.length} total)` : '';
  if (!apps.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:#718096; text-align:center; padding:20px">No applications yet</td></tr>';
    return;
  }
  tbody.innerHTML = apps.map(a => {
    const plat = a.platform.toLowerCase().includes('linkedin') ? 'linkedin' :
                 a.platform.toLowerCase().includes('dice') ? 'dice' : 'careerbuilder';
    const cls = plat === 'linkedin' ? 'badge-blue' : plat === 'dice' ? 'badge-yellow' : 'badge-green';
    return `<tr>
      <td style="color:#718096; font-size:0.78rem">${a.time.split(' ').slice(1).join(' ')}</td>
      <td><span class="badge ${cls}">${plat.toUpperCase()}</span></td>
      <td>${a.job}</td>
      <td>${a.score}</td>
    </tr>`;
  }).join('');
}

async function fetchJobs() {
  const r = await fetch('/api/jobs'); const jobs = await r.json();
  const tbody = document.getElementById('jobsBody');
  if (!jobs.length) { tbody.innerHTML = '<tr><td colspan="5" style="color:#718096; text-align:center; padding:20px">No jobs yet</td></tr>'; return; }
  tbody.innerHTML = jobs.slice(0, 15).map(j => {
    const sc = parseInt(j.score || 0);
    const scCls = sc >= 70 ? 'score-high' : sc >= 50 ? 'score-mid' : 'score-low';
    const stCls = j.status === 'applied' ? 'badge-green' : j.status === 'failed' ? '' : 'badge-gray';
    const plat = (j.source_platform || 'linkedin').toLowerCase();
    const platCls = plat === 'linkedin' ? 'badge-blue' : plat === 'dice' ? 'badge-yellow' : 'badge-green';
    return `<tr>
      <td>${j.title}</td>
      <td>${j.company}</td>
      <td><span class="badge ${platCls}">${plat.toUpperCase()}</span></td>
      <td class="${scCls}">${sc || '—'}</td>
      <td><span class="badge ${stCls}">${j.status || '—'}</span></td>
    </tr>`;
  }).join('');
}

async function fetchLogs() {
  const r = await fetch('/api/logs'); const d = await r.json();
  const box = document.getElementById('logBox');
  box.textContent = d.lines.join('\n') || 'No logs yet.';
  box.scrollTop = box.scrollHeight;
}

async function fetchConfig() {
  const r = await fetch('/api/config'); const d = await r.json();
  document.getElementById('setupBanner').style.display = d.has_api_key ? 'none' : 'block';
}

async function startAgent() {
  await fetch('/api/start', {method:'POST'});
  toast('Agent started ✓');
  setTimeout(fetchStatus, 1000);
}

async function stopAgent() {
  await fetch('/api/stop', {method:'POST'});
  toast('Agent stopped', '#f56565');
  setTimeout(fetchStatus, 1000);
}

async function saveApiKey() {
  const key = document.getElementById('apiKeyInput').value.trim();
  if (!key) { toast('Please paste an API key', '#f56565'); return; }
  const provider = key.startsWith('AIza') ? 'gemini' : 'openai';
  await fetch('/api/config/apikey', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key, provider})});
  toast('API key saved ✓');
  document.getElementById('apiKeyInput').value = '';
  fetchConfig();
}

async function saveSiteCredentials(site) {
  const emailId = site === 'dice' ? 'diceEmail' : 'cbEmail';
  const passId  = site === 'dice' ? 'dicePass'  : 'cbPass';
  const email = document.getElementById(emailId).value.trim();
  const password = document.getElementById(passId).value.trim();
  if (!email || !password) { toast('Enter both email and password', '#f56565'); return; }
  const r = await fetch('/api/config/site-credentials', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({site, email, password})
  });
  if (r.ok) {
    toast(`${site.charAt(0).toUpperCase()+site.slice(1)} credentials saved ✓`);
    document.getElementById(emailId).value = '';
    document.getElementById(passId).value = '';
  } else {
    toast('Save failed — check console', '#f56565');
  }
}

function updateNextCycle() {
  const mins = Math.floor(applyCycleCountdown / 60);
  const secs = applyCycleCountdown % 60;
  document.getElementById('statNext').textContent = `${mins}:${secs.toString().padStart(2,'0')}`;
  if (applyCycleCountdown > 0) applyCycleCountdown--;
  else applyCycleCountdown = 15 * 60;
}

async function refresh() {
  await Promise.all([fetchStatus(), fetchApplications(), fetchJobs(), fetchLogs(), fetchConfig()]);
}

refresh();
setInterval(refresh, 15000);
setInterval(updateNextCycle, 1000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_HTML)


def run():
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")


if __name__ == "__main__":
    run()
