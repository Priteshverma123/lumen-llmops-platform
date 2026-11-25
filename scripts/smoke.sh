#!/usr/bin/env bash
# End-to-end smoke test against a running stack: bootstrap → OTLP/JSON span → trace queryable.
set -euo pipefail
API="${API:-http://localhost:8000}"
TOKEN="${LUMEN_BOOTSTRAP_TOKEN:-change-me-bootstrap}"

KEY=$(curl -sf -X POST "$API/v1/admin/bootstrap" -H "x-bootstrap-token: $TOKEN" \
  -H 'content-type: application/json' -d "{\"project\":\"smoke-$RANDOM\"}" \
  | python -c 'import sys, json; print(json.load(sys.stdin)["api_key"])')
TRACE=$(python -c 'import os; print(os.urandom(16).hex())')
NOW=$(python -c 'import time; print(time.time_ns())')
END=$(python -c "print($NOW + 1200000000)")

BODY=$(python - "$TRACE" "$NOW" "$END" <<'PY'
import json, sys
trace, start, end = sys.argv[1:4]
attr = lambda k, v: {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
print(json.dumps({"resourceSpans": [{"resource": {"attributes": [attr("service.name", "smoke")]},
  "scopeSpans": [{"spans": [{"traceId": trace, "spanId": "0102030405060708", "name": "chat", "kind": 3,
    "startTimeUnixNano": start, "endTimeUnixNano": end, "status": {"code": 1},
    "attributes": [attr("gen_ai.provider.name", "anthropic"), attr("gen_ai.response.model", "claude-sonnet-5-5"),
                   attr("gen_ai.usage.input_tokens", 1200), attr("gen_ai.usage.output_tokens", 300)]}]}]}]}))
PY
)
curl -sf -X POST "$API/v1/traces" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d "$BODY"

for i in $(seq 1 60); do
  if curl -sf "$API/v1/traces/$TRACE" -H "Authorization: Bearer $KEY" -o /tmp/lumen-trace.json; then
    echo "trace queryable after ~${i}s"; head -c 400 /tmp/lumen-trace.json; echo
    exit 0
  fi
  sleep 1
done
echo "trace not queryable after 60s" >&2
exit 1
