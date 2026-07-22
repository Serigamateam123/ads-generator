#!/bin/bash
# Run every safety check against a temp COPY of the app — never touches live data.
# Usage:  ./tests/run_all.sh
cd "$(dirname "$0")/.." || exit 1
PY="./venv/bin/python"
fail=0
for t in tests/test_*.py; do
  echo "──────── $t"
  "$PY" "$t" 2>&1 | grep -E "^(  (PASS|FAIL)|=== |ALL CHECKS|[0-9]+ FAILED)" || true
  [ "${PIPESTATUS[0]}" -ne 0 ] && fail=1
done
echo
[ "$fail" -eq 0 ] && echo "✅ ALL SUITES PASSED" || echo "❌ SOME SUITES FAILED"
exit $fail
