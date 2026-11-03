#!/usr/bin/env bash
# Install the harness (engine + active packs) into OpenClaw.
#   ./install.sh          back up, install, verify, restart
#   ./install.sh --check  show what's installed vs. this repo (changes nothing)
#
# Settings come from config.env (created from config.env.example on first run).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$REPO/config.env" ]] || { cp "$REPO/config.env.example" "$REPO/config.env"; echo "Created config.env from the example (edit it if needed)."; }
# shellcheck source=/dev/null
source "$REPO/config.env"

OC="$HOME/.openclaw"
EXT="$OC/extensions"
PACK_HOME="$OC/harness-packs"
CFG="$OC/openclaw.json"
IFS=',' read -r -a PACK_LIST <<< "$PACKS"

ver() { grep -o '"version": *"[^"]*"' "$1" 2>/dev/null | head -1 | grep -o '[0-9][0-9.]*' || echo "missing"; }
commit() { git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "not a git repo"; }

show_installed() {
  echo "grounded plugin:   $(ver "$EXT/grounded/package.json")"
  echo "audit-gate plugin: $(ver "$EXT/audit-gate/package.json")"
  for p in "$PACK_HOME"/*/; do [[ -d "$p" ]] && echo "pack:              $(basename "$p") $(ver "$p/pack.json")"; done
  echo "installed from:    $(cat "$PACK_HOME/.installed-from" 2>/dev/null || echo unknown)"
}

if [[ "${1:-}" == "--check" ]]; then
  echo "Installed now:"; show_installed
  echo; echo "In this repo (commit $(commit)):"
  echo "grounded plugin:   $(ver "$REPO/engine/extensions/grounded/package.json")"
  echo "audit-gate plugin: $(ver "$REPO/engine/extensions/audit-gate/package.json")"
  for p in "${PACK_LIST[@]}"; do echo "pack:              $p $(ver "$REPO/packs/$p/pack.json")"; done
  exit 0
fi

for p in "${PACK_LIST[@]}"; do
  [[ -f "$REPO/packs/$p/pack.json" ]] || { echo "Unknown pack '$p' in PACKS (no packs/$p/pack.json)"; exit 1; }
done
command -v jq >/dev/null || { echo "jq is required"; exit 1; }

# 1) Back up everything this script touches
TS=$(date +%Y%m%d-%H%M%S)
BK="$HOME/backups/harness-before-$TS.tgz"
mkdir -p "$HOME/backups"
tar czf "$BK" -C / \
  $(ls -d "$EXT/grounded" "$EXT/audit-gate" "$PACK_HOME" "$CFG" \
          "$AUDITOR_WS/AGENTS.md" "$AUDITOR_WS/SOUL.md" "$AUDITOR_WS/IDENTITY.md" "$AUDITOR_WS/USER.md" \
          "$WORKER_WS/AGENTS.md" 2>/dev/null | sed 's|^/||')
echo "Backup: $BK"

# 2) Engine plugins
mkdir -p "$EXT/grounded" "$EXT/audit-gate"
cp "$REPO"/engine/extensions/grounded/* "$EXT/grounded/"
cp "$REPO"/engine/extensions/audit-gate/* "$EXT/audit-gate/"

# 3) Packs: only the runtime file (pack.json). Eval cases and answer keys stay in the repo.
# Engine skills (e.g. research-101), referenced by path from the plugin config in experiments
rm -rf "$OC/harness-skills"; mkdir -p "$OC/harness-skills"
cp -r "$REPO"/engine/skills/* "$OC/harness-skills/"
rm -rf "$PACK_HOME"; mkdir -p "$PACK_HOME"
PACK_FILES=()
for p in "${PACK_LIST[@]}"; do
  mkdir -p "$PACK_HOME/$p"
  cp "$REPO/packs/$p/pack.json" "$PACK_HOME/$p/pack.json"
  PACK_FILES+=("$PACK_HOME/$p/pack.json")
done
echo "$REPO @ $(commit)" > "$PACK_HOME/.installed-from"

# 4) Auditor: persona files + AGENTS.md assembled from the engine template and the packs
mkdir -p "$AUDITOR_WS"
cp "$REPO"/engine/agents/auditor/{SOUL,IDENTITY,USER}.md "$AUDITOR_WS/"
python3 "$REPO/engine/tools/assemble.py" auditor --repo "$REPO" --packs "$PACKS" --user "$USER_NAME" > "$AUDITOR_WS/AGENTS.md.new"
mv "$AUDITOR_WS/AGENTS.md.new" "$AUDITOR_WS/AGENTS.md"
echo "Wrote auditor AGENTS.md (packs: $PACKS)"

# 5) Worker: replace the managed block in its AGENTS.md (the rest of the file is left alone)
BLOCK="$(python3 "$REPO/engine/tools/assemble.py" worker --repo "$REPO" --packs "$PACKS" \
          --user "$USER_NAME" --env-name "$ENV_NAME" --check-py "$CHECK_PY")"
if [[ -f "$WORKER_WS/AGENTS.md" ]]; then
  BLOCK="$BLOCK" python3 - "$WORKER_WS/AGENTS.md" <<'PY'
import os, re, sys
p = sys.argv[1]; s = open(p).read(); block = os.environ["BLOCK"].strip() + "\n"
if "<!-- harness:begin" in s:
    s = re.sub(r"<!-- harness:begin.*?<!-- harness:end -->\n?", block, s, flags=re.S)
elif "<!-- harness:env-note -->" in s:
    # v1.0 note: it was appended at the end of the file; replace it from its marker on
    s = s[: s.index("<!-- harness:env-note -->")].rstrip() + "\n\n" + block
else:
    s = s.rstrip() + "\n\n" + block
open(p, "w").write(s)
PY
  echo "Updated the harness block in $WORKER_WS/AGENTS.md"
else
  echo "Note: $WORKER_WS/AGENTS.md not found; skipped the worker note"
fi

# 6) Plugin config: values derived from config.env are always rewritten
PF_JSON=$(printf '%s\n' "${PACK_FILES[@]}" | jq -R . | jq -s .)
# Write roots: the worker workspace first (the plugin names it in reminders), then /tmp and project folders
read -r -a PROJ <<< "${PROJECT_ROOTS:-}"
WR_JSON=$(printf '%s\n' "$WORKER_WS" "/tmp" "${PROJ[@]}" | sed '/^$/d' | jq -R . | jq -s .)
jq --argjson pf "$PF_JSON" --argjson wr "$WR_JSON" --arg user "$USER_NAME" --arg py "$CHECK_PY" '
  .plugins.entries.grounded.config.packFiles = $pf
  | .plugins.entries.grounded.config.userName = $user
  | .plugins.entries.grounded.config.checkPython = $py
  | .plugins.entries.grounded.config.writeRoots = $wr
  | .plugins.entries["audit-gate"].config.packFiles = $pf
  | .plugins.entries["audit-gate"].config.userName = $user
  | .plugins.entries["audit-gate"].config.checkPython = $py
' "$CFG" > "$CFG.new" && mv "$CFG.new" "$CFG"
echo "Set plugin config (packs, user, checkPython, writeRoots)"

# 7) Validate, restart, verify
echo; echo "Validating config..."
openclaw config validate
echo; echo "Restarting gateway..."
openclaw gateway restart --wait 30
echo; echo "Installed now:"; show_installed
echo
openclaw plugins inspect grounded --runtime | grep -E "Status|Version"
openclaw plugins inspect audit-gate --runtime | grep -E "Status|Version"
grep -h "packs loaded" "$OC/logs/grounded-audit.log" "$OC/logs/audit-gate.log" 2>/dev/null | tail -2

# 8) Dedicated audit session (kept apart from the auditor's main chat; reset each round)
if ! openclaw sessions --agent "$AUDITOR_ID" --json 2>/dev/null | grep -q "\"agent:$AUDITOR_ID:audits\""; then
  echo; echo "Creating the dedicated audit session agent:$AUDITOR_ID:audits ..."
  openclaw agent --agent "$AUDITOR_ID" --session-key "agent:$AUDITOR_ID:audits" --message "Audit session ready. Reply with OK." >/dev/null 2>&1 \
    && echo "Created." || echo "Could not create it automatically; run: openclaw agent --agent $AUDITOR_ID --session-key agent:$AUDITOR_ID:audits --message OK"
else
  echo "Audit session agent:$AUDITOR_ID:audits exists."
fi
echo; echo "Done. To undo: tar xzf $BK -C / && openclaw gateway restart"
