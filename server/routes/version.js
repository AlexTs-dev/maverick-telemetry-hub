/**
 * server/routes/version.js
 *   GET  /api/version         — { current, latest, updateAvailable, checkedAt }
 *   POST /api/version/update  — apply the latest release via deploy/pull-deploy.sh
 *
 * The update is run as a detached child process so it survives the
 * express_bridge restart that pull-deploy.sh triggers at the end.
 * NOTE: pull-deploy.sh's final `sudo systemctl restart` needs the
 * passwordless sudoers entry (see deploy/README.md) to succeed unattended.
 */

const express  = require('express');
const { spawn } = require('child_process');
const fs   = require('fs');
const os   = require('os');
const path = require('path');
const { status, refresh, REPO_ROOT } = require('../version');

const router = express.Router();

let updating = false;

router.get('/', (req, res) => {
  res.json(status());
});

router.post('/update', async (req, res) => {
  // Re-check against GitHub in case the cached view is stale
  if (!status().updateAvailable) {
    await refresh().catch(() => {});
    if (!status().updateAvailable) {
      return res.status(409).json({ error: 'No update available' });
    }
  }
  if (updating) return res.status(409).json({ error: 'Update already in progress' });

  const script = path.join(REPO_ROOT, 'deploy', 'pull-deploy.sh');
  if (!fs.existsSync(script)) {
    return res.status(500).json({ error: 'pull-deploy.sh not found' });
  }

  updating = true;

  // Log somewhere debuggable; pull-deploy restarts this service so we won't see its tail
  let out = 'ignore';
  try { out = fs.openSync(path.join(os.homedir(), '.maverick-update.log'), 'a'); } catch { /* fall back to ignore */ }

  const child = spawn('bash', [script], {
    cwd: REPO_ROOT,
    detached: true,
    stdio: ['ignore', out, out],
  });
  child.on('error', () => { updating = false; });
  child.unref();

  res.json({ status: 'started' });
});

module.exports = router;
