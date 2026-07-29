#!/bin/bash
# tools/publish-models.sh
#
# Publishes the trained YOLO weights in jetson/models/*.pt to a dedicated
# GitHub release so the Jetson's pull-deploy can fetch them over the air.
#
# WHY A SEPARATE RELEASE
#   `.pt` files are gitignored on purpose (jetson/models/README.md): they are
#   trained artifacts, not build outputs, so GitHub Actions cannot regenerate
#   them. They also version on a different cadence than code — retraining
#   shouldn't require a code commit, and a code commit shouldn't reship 8 MB of
#   identical weights. This script is the bridge: you train on the desktop, run
#   this once, and every Jetson picks the weights up on its next poll.
#
#   The release is created with make_latest=false so /releases/latest keeps
#   returning the code release. If that were not the case the Pi's pull-deploy
#   would find a release with no `maverick-telemetry` asset and fail every poll.
#
# USAGE
#   ./tools/publish-models.sh                 # publish jetson/models/*.pt
#   ./tools/publish-models.sh a.pt b.pt       # publish specific files
#
# AUTH — either works:
#   gh auth login                             # GitHub CLI, or
#   export GITHUB_TOKEN=ghp_xxx               # needs the `repo` scope

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$REPO_ROOT/jetson/models"
TAG="models-latest"

SLUG=$(git -C "$REPO_ROOT" remote get-url origin \
  | sed -E 's|.*github\.com[:/]||' | sed 's|\.git$||')

# ---------------------------------------------------------------------------
# Collect the files to publish
# ---------------------------------------------------------------------------
FILES=()
if [ "$#" -gt 0 ]; then
  FILES=("$@")
else
  shopt -s nullglob
  FILES=("$MODELS_DIR"/*.pt)
  shopt -u nullglob
fi

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "[publish-models] No .pt files found in $MODELS_DIR" >&2
  echo "[publish-models] Train first — see jetson/training/README.md" >&2
  exit 1
fi

for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "[publish-models] Not a file: $f" >&2; exit 1; }
done

echo "[publish-models] Repo: $SLUG"
echo "[publish-models] Tag:  $TAG"
for f in "${FILES[@]}"; do
  printf '[publish-models]   %-28s %s bytes\n' "$(basename "$f")" "$(wc -c < "$f" | tr -d ' ')"
done

NOTES="Trained YOLO weights consumed by the Jetson's pull-deploy.

Not a code release — deliberately marked not-latest so /releases/latest keeps
resolving to the newest \`deploy-*\` release. Regenerate with
\`tools/publish-models.sh\` after retraining; see jetson/models/README.md."

# ---------------------------------------------------------------------------
# Path A — GitHub CLI, when available and authenticated
# ---------------------------------------------------------------------------
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo "[publish-models] Using gh CLI"
  if ! gh release view "$TAG" --repo "$SLUG" >/dev/null 2>&1; then
    gh release create "$TAG" --repo "$SLUG" \
      --title "Jetson models" --notes "$NOTES" --latest=false
    echo "[publish-models] Created release $TAG"
  fi
  # --clobber replaces same-named assets; GitHub rejects duplicate names.
  gh release upload "$TAG" --repo "$SLUG" --clobber "${FILES[@]}"
  echo "[publish-models] Uploaded ${#FILES[@]} file(s) to $TAG"
  exit 0
fi

# ---------------------------------------------------------------------------
# Path B — plain curl + GITHUB_TOKEN
# ---------------------------------------------------------------------------
if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "[publish-models] Need either an authenticated 'gh' or GITHUB_TOKEN set." >&2
  echo "[publish-models]   gh auth login          # or" >&2
  echo "[publish-models]   export GITHUB_TOKEN=ghp_xxx   (scope: repo)" >&2
  exit 1
fi

echo "[publish-models] Using curl + GITHUB_TOKEN"

api() {
  local method="$1" url="$2"; shift 2
  curl -sS -X "$method" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@" "$url"
}

# Look up the release by tag; create it if this is the first publish.
RELEASE=$(api GET "https://api.github.com/repos/$SLUG/releases/tags/$TAG" || true)
RELEASE_ID=$(printf '%s' "$RELEASE" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('id') or '')
except Exception:
    pass
")

if [ -z "$RELEASE_ID" ]; then
  BODY=$(python3 -c "
import json, sys
print(json.dumps({
    'tag_name': sys.argv[1],
    'name': 'Jetson models',
    'body': sys.argv[2],
    'make_latest': 'false',
}))
" "$TAG" "$NOTES")
  RELEASE=$(api POST "https://api.github.com/repos/$SLUG/releases" -d "$BODY")
  RELEASE_ID=$(printf '%s' "$RELEASE" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('id') or '')
except Exception:
    pass
")
  [ -n "$RELEASE_ID" ] || {
    echo "[publish-models] Failed to create release $TAG:" >&2
    printf '%s\n' "$RELEASE" >&2
    exit 1
  }
  echo "[publish-models] Created release $TAG (id $RELEASE_ID)"
fi

for f in "${FILES[@]}"; do
  name="$(basename "$f")"

  # GitHub returns 422 on a duplicate asset name, so delete any existing one.
  existing=$(printf '%s' "$RELEASE" | python3 -c "
import sys, json
try:
    for a in json.load(sys.stdin).get('assets', []):
        if a['name'] == '$name':
            print(a['id']); break
except Exception:
    pass
")
  if [ -n "$existing" ]; then
    api DELETE "https://api.github.com/repos/$SLUG/releases/assets/$existing" >/dev/null
    echo "[publish-models] Replaced existing asset $name"
  fi

  curl -sS --fail-with-body -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @"$f" \
    "https://uploads.github.com/repos/$SLUG/releases/$RELEASE_ID/assets?name=$name" \
    >/dev/null
  echo "[publish-models] Uploaded $name"
done

echo "[publish-models] Done — Jetsons will pick these up on their next poll."
