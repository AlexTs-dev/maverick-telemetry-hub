/**
 * server/version.js
 * Maverick Telemetry Hub — version + update-check helpers
 *
 * "Current" version  = the tag pull-deploy last applied (~/.maverick-deployed-tag),
 *                      falling back to the repo's short commit (deploy-<hash>).
 * "Latest"  version  = the latest GitHub release tag, polled on an interval so
 *                      GET /api/version stays fast and within GitHub rate limits.
 *
 * The dashboard can't query GitHub directly (the Tauri CSP only allows
 * connect-src localhost:3000), so the comparison happens here on the server.
 */

const { execFileSync } = require('child_process');
const fs   = require('fs');
const os   = require('os');
const path = require('path');

const REPO_ROOT = path.join(__dirname, '..');
const TAG_FILE  = path.join(os.homedir(), '.maverick-deployed-tag');
const POLL_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes

let cache = { latest: null, checkedAt: null };

function git(args) {
  try {
    return execFileSync('git', ['-C', REPO_ROOT, ...args], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return null;
  }
}

// owner/repo from the origin remote, e.g. "AlexTs-dev/maverick-telemetry-hub"
function getRepoSlug() {
  const url = git(['remote', 'get-url', 'origin']);
  if (!url) return null;
  const m = url.match(/github\.com[:/](.+?)(?:\.git)?$/);
  return m ? m[1] : null;
}

// The version currently deployed/running on this machine
function getCurrentTag() {
  try {
    const tag = fs.readFileSync(TAG_FILE, 'utf8').trim();
    if (tag) return tag;
  } catch { /* tag file may not exist yet (e.g. before first pull-deploy) */ }
  const short = git(['rev-parse', '--short', 'HEAD']);
  return short ? `deploy-${short}` : null;
}

async function fetchLatestTag() {
  const slug = getRepoSlug();
  if (!slug) return null;
  const headers = {
    'User-Agent': 'maverick-telemetry',
    'Accept': 'application/vnd.github+json',
  };
  if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  try {
    const res = await fetch(`https://api.github.com/repos/${slug}/releases/latest`, { headers });
    if (!res.ok) return null;
    const body = await res.json();
    return body.tag_name || null;
  } catch {
    return null;
  }
}

async function refresh() {
  const latest = await fetchLatestTag();
  cache = { latest: latest ?? cache.latest, checkedAt: new Date().toISOString() };
  return cache;
}

function status() {
  const current = getCurrentTag();
  const latest  = cache.latest;
  return {
    current,
    latest,
    updateAvailable: Boolean(current && latest && current !== latest),
    checkedAt: cache.checkedAt,
  };
}

function startPolling() {
  refresh().catch(() => {});
  const timer = setInterval(() => refresh().catch(() => {}), POLL_INTERVAL_MS);
  if (timer.unref) timer.unref();
}

module.exports = { status, refresh, getCurrentTag, startPolling, REPO_ROOT };
