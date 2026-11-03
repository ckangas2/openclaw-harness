#!/usr/bin/env bash
# Unit tests for the engine plugins. Runs them against a stub OpenClaw SDK in a temp dir,
# with the scrna-qc pack as the active pack. Needs only node.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/node_modules/openclaw/plugin-sdk"
echo '{"name":"openclaw","type":"module","exports":{"./plugin-sdk/plugin-entry":"./plugin-sdk/plugin-entry.js"}}' > "$T/node_modules/openclaw/package.json"
echo 'export const definePluginEntry = (x) => x;' > "$T/node_modules/openclaw/plugin-sdk/plugin-entry.js"
echo '{"type":"module"}' > "$T/package.json"
cp "$REPO/engine/extensions/grounded/index.js" "$T/grounded.js"
cp "$REPO/engine/extensions/audit-gate/index.js" "$T/auditgate.js"
cp "$REPO/engine/extensions/grounded/links.js" "$T/links.js"
# Each plugin ships its own copy of links.js; the copies must not drift apart.
if cmp -s "$REPO/engine/extensions/grounded/links.js" "$REPO/engine/extensions/audit-gate/links.js"; then
  echo "PASS links.js copies identical"
else
  echo "FAIL links.js copies differ (grounded vs audit-gate)"; exit 1
fi
cp "$REPO/packs/scrna-qc/pack.json" "$T/pack.json"
cp "$HERE"/*.test.mjs "$T/"
cd "$T"
fail=0
for t in *.test.mjs; do
  echo "== $t"
  node "$t" | grep -E "FAIL|passed" || fail=1
  node "$t" >/dev/null || fail=1
done
echo "== scbench_answers.test.py"
python3 "$HERE/scbench_answers.test.py" | grep -E "FAIL|passed" || fail=1
python3 "$HERE/scbench_answers.test.py" >/dev/null || fail=1
exit $fail
