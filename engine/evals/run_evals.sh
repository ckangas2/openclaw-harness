#!/usr/bin/env bash
# Run every prompt in a pack's evals/cases/ through the worker agent, each in a fresh
# session, with its own fresh auditor session (agent:auditor:eval-<run>-<case>).
# The auditor's main session is never touched. Records everything under $RUNS_DIR/<RUN_ID>/.
#
# Usage (from the repo root):
#   engine/evals/run_evals.sh scrna-qc          # all cases in packs/scrna-qc/evals/cases
#   engine/evals/run_evals.sh scrna-qc 04       # only cases whose name contains "04"
#   REPEAT=3 engine/evals/run_evals.sh scrna-qc # each case three times
#
# Optional environment overrides:
#   AGENT, AUDITOR_AGENT, WORKSPACE, CASE_TIMEOUT (s), SETTLE (s), RUN_ID, RUNS_DIR, REPEAT,
#   SKIP_AUDITOR_SETUP (1 = no auditor session per case), CLEAR_DIRS (emptied before each case),
#   PRE_CASE (shell command run before each case, case name as $1)
set -uo pipefail

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$EVAL_DIR/../.." && pwd)"
[[ -f "$REPO/config.env" ]] && source "$REPO/config.env"
PACK="${1:-}"
if [[ -z "$PACK" || ! -d "$REPO/packs/$PACK/evals/cases" ]]; then
  echo "usage: $0 <pack> [case-filter]   (packs: $(ls "$REPO/packs" | tr '\n' ' '))" >&2
  exit 2
fi
CASES_DIR="$REPO/packs/$PACK/evals/cases"
RUNS_DIR="${RUNS_DIR:-${HARNESS_DATA:-$REPO/data}/agent-evals/runs}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M)-$PACK}"
OUT="$RUNS_DIR/$RUN_ID"
AGENT="${AGENT:-${WORKER_AGENT:-grounded-pig}}"
AUDITOR_AGENT="${AUDITOR_AGENT:-${AUDITOR_ID:-auditor}}"
WORKSPACE="${WORKSPACE:-${WORKER_WS:-$HOME/.openclaw/workspace/workspace-grounded}}"
G_LOG="$HOME/.openclaw/logs/grounded-audit.log"
A_LOG="$HOME/.openclaw/logs/audit-gate.log"
EXPORTS="$HOME/.openclaw/trajectory-exports"
CASE_TIMEOUT="${CASE_TIMEOUT:-2700}"   # 45 min per case
RESEARCH_TIMEOUT="${RESEARCH_TIMEOUT:-1200}"  # SPLIT=1: research call limit (20 min)
SETTLE="${SETTLE:-150}"                # wait for late agent-to-agent turns to finish before exporting
FILTER="${2:-}"
REPEAT="${REPEAT:-1}"

mkdir -p "$OUT"
echo "Run $RUN_ID → $OUT"
# Which engine + pack produced this run
printf '{"run_id":"%s","pack":"%s","label":"%s","commit":"%s","dirty":%s,"repeat":%s}\n' "$RUN_ID" "$PACK" "${RUN_LABEL:-}" \
  "$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  "$([[ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]] && echo true || echo false)" "$REPEAT" > "$OUT/run.json"

size_of() { stat -c %s "$1" 2>/dev/null || echo 0; }

for rep in $(seq 1 "$REPEAT"); do
for case in "$CASES_DIR"/*.md; do
  id="$(basename "$case" .md)"
  if [[ "$REPEAT" -gt 1 ]]; then id="r$rep-$id"; fi
  # FILTER: one substring, or several separated by | (e.g. "r05|r07").
  if [[ -n "$FILTER" ]]; then
    hit=0; IFS='|' read -ra _f <<< "$FILTER"
    for _p in "${_f[@]}"; do [[ -n "$_p" && "$(basename "$case" .md)" == *"$_p"* ]] && hit=1; done
    (( hit )) || continue
  fi
  dir="$OUT/$id"
  mkdir -p "$dir/files"
  cp "$case" "$dir/prompt.md"
  echo "=== $id  ($(date +%H:%M:%S))"

  tag="$RUN_ID-$id"
  key="agent:$AGENT:eval-$tag"
  akey="agent:$AUDITOR_AGENT:eval-$tag"

  # Create this case's own auditor session (one short turn), so the agent's
  # sessions_send has a fresh, empty auditor session to talk to. Arms without the
  # auditor skip this, so the auditor model is not loaded onto the GPU at all.
  if [[ "${SKIP_AUDITOR_SETUP:-0}" != 1 ]]; then
    openclaw agent --agent "$AUDITOR_AGENT" --session-key "$akey" \
      --message "New session for eval case $id. Reply with OK." > "$dir/auditor-setup.txt" 2>&1 || true
  fi
  # Folders that must start empty for every case (e.g. research briefs), so one case
  # cannot read what an earlier case wrote.
  for d in ${CLEAR_DIRS:-}; do [[ -n "$d" && "$d" != "/" ]] && rm -rf "$d"; done
  # Optional per-case reset command; gets the case name as $1 (e.g. clear a benchmark task's old outputs).
  [[ -n "${PRE_CASE:-}" ]] && bash -c "$PRE_CASE" _ "$(basename "$case" .md)"

  g0=$(size_of "$G_LOG"); a0=$(size_of "$A_LOG")
  touch "$dir/.start"
  start=$(date +%s)

  if [[ "${SPLIT:-0}" == 1 ]]; then
    # Two calls: a research-only session writes the brief; a fresh session does the task from it.
    brief="$WORKSPACE/briefs/split-$id.md"; mkdir -p "$WORKSPACE/briefs"; rm -f "$brief"
    { sed "s|__BRIEF__|$brief|g" "$EVAL_DIR/split/research.md"; cat "$case"; } > "$dir/research-prompt.md"
    timeout $((RESEARCH_TIMEOUT + 120)) openclaw agent --agent "$AGENT" --session-key "$key-research" \
      --message-file "$dir/research-prompt.md" --timeout "$RESEARCH_TIMEOUT" --json > "$dir/research-reply.json" 2> "$dir/research-stderr.txt"
    rrc=$?; rend=$(date +%s)
    cp "$brief" "$dir/brief.md" 2>/dev/null || echo "(no brief written)" > "$dir/brief.md"
    # An answer file written during research is set aside, so the analysis step has to produce its own.
    if [[ -n "${ANSWER_FILE:-}" ]]; then
      find "$WORKSPACE" -name "$ANSWER_FILE" -newer "$dir/.start" -not -path '*/data/*' 2>/dev/null | while read -r f; do
        mv "$f" "$dir/research-phase-$ANSWER_FILE"; echo "    research phase wrote $ANSWER_FILE (set aside)"; done
    fi
    echo "    research exit=$rrc  $((rend - start))s  brief=$(wc -c < "$dir/brief.md")B"
    { sed "s|__BRIEF__|$brief|g" "$EVAL_DIR/split/analysis.md" | python3 -c "import sys;t=sys.stdin.read();print(t.replace('__BRIEF_TEXT__',open(sys.argv[1]).read()),end='')" "$dir/brief.md"; cat "$case"; } > "$dir/analysis-prompt.md"
    timeout $((CASE_TIMEOUT + 120)) openclaw agent --agent "$AGENT" --session-key "$key" \
      --message-file "$dir/analysis-prompt.md" --timeout "$CASE_TIMEOUT" --json > "$dir/reply.json" 2> "$dir/stderr.txt"
    rc=$?
  else
    timeout $((CASE_TIMEOUT + 120)) openclaw agent --agent "$AGENT" --session-key "$key" \
      --message-file "$case" --timeout "$CASE_TIMEOUT" --json > "$dir/reply.json" 2> "$dir/stderr.txt"
    rc=$?
  fi
  end=$(date +%s)
  echo "    exit=$rc  $((end - start))s"

  sleep "$SETTLE"

  # Plugin log lines written during this case
  tail -c +$((g0 + 1)) "$G_LOG" > "$dir/grounded.log" 2>/dev/null || true
  tail -c +$((a0 + 1)) "$A_LOG" > "$dir/audit-gate.log" 2>/dev/null || true

  # Full transcripts of both agents
  if openclaw sessions export-trajectory --agent "$AGENT" --session-key "$key" --workspace "$HOME" \
       --output "eval-$tag-agent" > "$dir/export-agent.txt" 2>&1; then
    mv "$EXPORTS/eval-$tag-agent" "$dir/agent" 2>/dev/null || true
  fi
  if [[ "${SPLIT:-0}" == 1 ]] && openclaw sessions export-trajectory --agent "$AGENT" --session-key "$key-research" --workspace "$HOME" \
       --output "eval-$tag-research" > "$dir/export-research.txt" 2>&1; then
    mv "$EXPORTS/eval-$tag-research" "$dir/agent-research" 2>/dev/null || true
  fi
  if openclaw sessions export-trajectory --agent "$AUDITOR_AGENT" --session-key "$akey" --workspace "$HOME" \
       --output "eval-$tag-auditor" > "$dir/export-auditor.txt" 2>&1; then
    mv "$EXPORTS/eval-$tag-auditor" "$dir/auditor" 2>/dev/null || true
  fi

  # Files the agents created or changed during the case (workspace + /tmp scripts)
  find "$WORKSPACE" -type f -newer "$dir/.start" -not -path '*/memory/*' -size -2M \
    -exec cp --parents {} "$dir/files" \; 2>/dev/null || true
  find /tmp -maxdepth 3 -type f -newer "$dir/.start" \( -name '*.py' -o -name '*.R' -o -name '*.md' \) -size -2M \
    -exec cp --parents {} "$dir/files" \; 2>/dev/null || true

  printf '{"id":"%s","exit":%d,"seconds":%d,"session":"%s","auditor_session":"%s"}\n' \
    "$id" "$rc" $((end - start)) "$key" "$akey" > "$dir/meta.json"
done
done

python3 "$EVAL_DIR/summarize.py" "$OUT"
python3 "$EVAL_DIR/score.py" "$OUT" --workspace "$WORKSPACE" > /dev/null || true
tar czf "$RUNS_DIR/$RUN_ID.tgz" -C "$RUNS_DIR" "$RUN_ID"
echo
echo "Done. Summary: $OUT/summary.md"
echo "Scorecard: $OUT/scorecard.md"
echo "Bundle:  $RUNS_DIR/$RUN_ID.tgz"
