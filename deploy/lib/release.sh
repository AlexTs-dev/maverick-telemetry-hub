# deploy/lib/release.sh
#
# Shared GitHub-Releases helpers for the pull-deploy scripts on both devices
# (the Pi's deploy/pull-deploy.sh and the Jetson's jetson/deploy/pull-deploy.sh).
#
# Sourced, never executed. Every function is prefixed `mav_` to keep the
# namespace out of the callers' way.
#
# Both devices only ever need OUTBOUND HTTPS to GitHub — no inbound SSH, no
# self-hosted runner. The repo is public, so GITHUB_TOKEN is optional and only
# raises the API rate limit (60 → 5000 requests/hour).

# ---------------------------------------------------------------------------
# mav_repo_slug <repo_dir>
# Prints "owner/repo" derived from the git remote, so nothing is hardcoded.
# ---------------------------------------------------------------------------
mav_repo_slug() {
  git -C "$1" remote get-url origin \
    | sed -E 's|.*github\.com[:/]||' \
    | sed 's|\.git$||'
}

# ---------------------------------------------------------------------------
# mav_curl <url> [curl args...]
# curl with the optional bearer token applied. Kept in one place so callers
# never have to reason about whether GITHUB_TOKEN is set.
# ---------------------------------------------------------------------------
mav_curl() {
  local url="$1"; shift
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -sf -H "Authorization: Bearer $GITHUB_TOKEN" \
         -H "Accept: application/vnd.github+json" "$@" "$url"
  else
    curl -sf -H "Accept: application/vnd.github+json" "$@" "$url"
  fi
}

# ---------------------------------------------------------------------------
# mav_fetch_release <slug> <ref>
# Prints the release JSON. <ref> is either "latest" or "tags/<tag>".
#
# Fetching a models release by explicit tag (rather than "latest") is what lets
# code releases and model releases coexist: /releases/latest only ever returns
# the code release, because publish-models.sh marks its release not-latest.
# ---------------------------------------------------------------------------
mav_fetch_release() {
  local slug="$1" ref="$2"
  mav_curl "https://api.github.com/repos/$slug/releases/$ref"
}

# ---------------------------------------------------------------------------
# mav_json_field <field>
# Reads release JSON on stdin, prints a top-level field ("" when absent).
# ---------------------------------------------------------------------------
mav_json_field() {
  python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('$1') or '')
except Exception:
    pass
"
}

# ---------------------------------------------------------------------------
# mav_asset_url <json> <asset_name>
# Prints the browser_download_url of a named asset ("" when absent).
# ---------------------------------------------------------------------------
mav_asset_url() {
  printf '%s' "$1" | python3 -c "
import sys, json
try:
    for a in json.load(sys.stdin).get('assets', []):
        if a['name'] == '$2':
            print(a['browser_download_url'])
            break
except Exception:
    pass
"
}

# ---------------------------------------------------------------------------
# mav_assets_stamp <json>
# Prints a stable fingerprint of every asset (name:size:updated_at), used to
# decide whether a fixed-tag release's payload actually changed. The models
# release keeps one tag forever, so its tag name can't serve as a version
# marker the way a code release's deploy-<sha> tag does.
# ---------------------------------------------------------------------------
mav_assets_stamp() {
  printf '%s' "$1" | python3 -c "
import sys, json
try:
    assets = json.load(sys.stdin).get('assets', [])
    print(';'.join(sorted(
        f\"{a['name']}:{a['size']}:{a.get('updated_at','')}\" for a in assets
    )))
except Exception:
    pass
"
}

# ---------------------------------------------------------------------------
# mav_download <url> <dest>
# Follows redirects to the asset CDN. Note: -H Accept:application/vnd.github+json
# is deliberately NOT applied here — browser_download_url serves raw bytes.
#
# -sS keeps the progress meter out of the journal (these run under systemd, and
# curl's progress bar renders as pages of carriage-return noise there) while
# still printing real errors.
# ---------------------------------------------------------------------------
mav_download() {
  local url="$1" dest="$2"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -fsSL --retry 3 --retry-delay 2 -H "Authorization: Bearer $GITHUB_TOKEN" -o "$dest" "$url"
  else
    curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
  fi
}
