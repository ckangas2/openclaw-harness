#!/usr/bin/env bash
# Overnight comparison on one pack. Each arm runs the same cases REPEAT times and differs
# from its neighbour in one thing:
#   harness   full harness (current install): grounding, answer checks, auditor
#   noaudit   harness without the auditor              (harness vs noaudit = auditor's value)
#   split     noaudit, but each case is two calls: research-only (writes the brief), then the task from the brief
#   brief     noaudit + research brief before each task (noaudit vs brief = brief's value)
#   mapcheck  brief with a map step first + claim check (brief vs mapcheck = both front and back ends)
#   briefcheck brief + claim check at the end (brief vs briefcheck = the claim check's value)
#   brief101  brief + research-101 skill read before searching (brief vs brief101 = the skill's value)
#   noaudit101 noaudit + research-101 skill (skill without the brief)
#   baseline  gates only: installs and outside writes still gated; nothing else
# Choose arms with ARMS (default: harness baseline); FILTER limits to matching case names. Every arm starts from the saved config,
# and the original openclaw.json is restored at the end, even if a step fails.
#
# usage (from the repo root):
#   ARMS="harness noaudit brief baseline" REPEAT=3 nohup bash engine/evals/overnight.sh scrna-qc-heldout > data/agent-evals/overnight.log 2>&1 &
#
# Pack "scbench" (external benchmark, setup in engine/evals/scbench/SETUP.md): stages the evals'
# data and writes the cases first, blocks the worker from the benchmark's repo, answer keys and
# run folders in every arm, clears each task's old outputs before a case, and grades each arm
# with scBench's own graders (benchmark.md and <case>/result.json in the run folder).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
[[ -f "$REPO/config.env" ]] && source "$REPO/config.env"
PACK="${1:-scrna-qc}"
REPEAT="${REPEAT:-3}"
ARMS="${ARMS:-harness baseline}"
WS="${WORKER_WS:-$HOME/.openclaw/workspace/workspace-grounded}"
CFG="$HOME/.openclaw/openclaw.json"
TS="$(date +%Y%m%d-%H%M)"
BK="$HOME/backups/openclaw.json.before-overnight-$TS"
mkdir -p "$HOME/backups" && cp "$CFG" "$BK"

restore() {
  echo "[$(date +%H:%M)] restoring config from $BK"
  cp "$BK" "$CFG"
  openclaw gateway restart --wait 30 >/dev/null 2>&1 || echo "WARNING: gateway restart failed; run: openclaw gateway restart"
  echo "[$(date +%H:%M)] restored"
}
trap restore EXIT

G='.plugins.entries.grounded.config'
RUNS="${RUNS_DIR:-${HARNESS_DATA:-$REPO/data}/agent-evals/runs}"
extra='.'; pre=""; answerfile=""
if [[ "$PACK" == scbench ]]; then
  SCBENCH_SRC="${SCBENCH_SRC:-$HOME/.bench-src/scbench}"
  SCBENCH_PY="${SCBENCH_PY:-$HOME/.bench-src/venv/bin/python}"
  [[ -x "$SCBENCH_PY" ]] || { echo "scBench venv python not found at $SCBENCH_PY (see engine/evals/scbench/SETUP.md)"; exit 1; }
  "$SCBENCH_PY" "$REPO/engine/evals/scbench/prepare.py" --src "$SCBENCH_SRC" --ws "$WS" ${FILTER:+--filter "$FILTER"} \
    || { echo "scBench prepare failed; stopping"; exit 1; }
  # Off-limits in every arm: the benchmark (repo, published trajectories), its local clone, and run folders
  extra="$G.blockedPatterns = (($G.blockedPatterns // []) + [\"scbench\", \"latch-eval-tools\", \"bench-src\", \"\\\\.latch/\", \"bench/\\\\.cache\", \"$RUNS\"]) | $G.answerFiles = [\"analysis_output.json\"]"
  answerfile=analysis_output.json
  pre='find "'"$WS"'/bench/$1/agent_workspace" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +'
fi
for arm in $ARMS; do
  skip=0; clear=""; settle=""; split=0
  case "$arm" in
    harness)  patch='.' ;;
    noaudit)  patch="$G.auditAgents = []"; skip=1 ;;
    split)    patch="$G.auditAgents = []"; skip=1; split=1; clear="$WS/briefs" ;;
    brief)    patch="$G.auditAgents = [] | $G.briefFirst = true | $G.briefDir = \"$WS/briefs\""; skip=1; clear="$WS/briefs" ;;
    mapcheck) patch="$G.auditAgents = [] | $G.briefFirst = true | $G.briefDir = \"$WS/briefs\" | $G.mapFirst = true | $G.researchFirst = true | $G.claimCheck = true"; skip=1; clear="$WS/briefs" ;;
    briefcheck) patch="$G.auditAgents = [] | $G.briefFirst = true | $G.briefDir = \"$WS/briefs\" | $G.claimCheck = true"; skip=1; clear="$WS/briefs" ;;
    brief101) patch="$G.auditAgents = [] | $G.briefFirst = true | $G.briefDir = \"$WS/briefs\" | $G.researchGuide = \"$HOME/.openclaw/harness-skills/research-101/SKILL.md\""; skip=1; clear="$WS/briefs" ;;
    noaudit101) patch="$G.auditAgents = [] | $G.researchGuide = \"$HOME/.openclaw/harness-skills/research-101/SKILL.md\""; skip=1 ;;
    baseline) patch="$G.gatesOnly = true"; skip=1; settle="${BASELINE_SETTLE:-30}" ;;
    *) echo "unknown arm: $arm"; exit 1 ;;
  esac
  # No auditor in the arm: nothing arrives late, so no settle wait before exporting (was 150 s per case).
  [[ -z "$settle" && "$skip" == 1 ]] && settle="${NOAUDIT_SETTLE:-0}"
  echo "[$(date +%H:%M)] arm $arm, $PACK x$REPEAT"
  jq "$patch | $extra" "$BK" > "$CFG.new" && mv "$CFG.new" "$CFG"
  openclaw config validate || { echo "config invalid for arm $arm; stopping"; exit 1; }
  openclaw gateway restart --wait 30
  # Unload models so every arm starts with the same GPU state (ignored if ollama is absent).
  command -v ollama >/dev/null && ollama ps 2>/dev/null | awk 'NR>1{print $1}' | xargs -r -n1 ollama stop >/dev/null 2>&1
  sleep 10
  # Sample GPU/CPU placement of loaded models every 2 min (is a model spilling to CPU?)
  OUTDIR="$RUNS/$TS-$PACK-$arm"; mkdir -p "$OUTDIR"
  ( while command -v ollama >/dev/null; do echo "--- $(date +%H:%M:%S)"; ollama ps 2>&1; sleep 120; done ) > "$OUTDIR/ollama-ps.log" 2>&1 &
  SAMPLER=$!
  env RUN_ID="$TS-$PACK-$arm" RUN_LABEL="$arm" SKIP_AUDITOR_SETUP="$skip" CLEAR_DIRS="$clear" \
    SPLIT="$split" ${answerfile:+ANSWER_FILE="$answerfile"} ${settle:+SETTLE="$settle"} ${pre:+PRE_CASE="$pre"} REPEAT="$REPEAT" bash "$REPO/engine/evals/run_evals.sh" "$PACK" "${FILTER:-}"
  kill "$SAMPLER" 2>/dev/null
  [[ "$PACK" == scbench ]] && "$SCBENCH_PY" "$REPO/engine/evals/scbench/grade.py" "$OUTDIR" --src "$SCBENCH_SRC" | tail -n +3
done

echo "[$(date +%H:%M)] done: $RUNS/$TS-$PACK-{${ARMS// /,}}"
