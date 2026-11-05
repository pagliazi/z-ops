#!/bin/bash
# Z-OPS P0-P3 全量回归 (隔离 Redis 6399)
cd /opt/zops || exit 1
export ZOPS_TEST_RUN="$(date +%s%3N)"
redis-cli -p 6399 flushall >/dev/null 2>&1
rm -rf tests/p0/__pycache__ tests/p1/__pycache__ tests/p2/__pycache__ tests/p3/__pycache__
FAILSUITE=0
for f in tests/p0/test_p0_acceptance.py tests/p0/test_p0_def.py tests/p0/test_llm_config_mask.py \
         tests/p1/test_p1_task_runtime.py tests/p1/test_p1_feishu_runtime.py tests/p1/test_p1_n8n_entry.py \
         tests/p1/test_p1_diagnostics.py tests/p1/test_p1_archive_sweep.py \
         tests/p2/test_p2_lifecycle.py tests/p2/test_close_loop.py \
         tests/p3/test_mcp_rest_discovery.py tests/p3/test_model_gateway.py tests/p3/test_plugin_system.py \
         tests/p3/test_cu_bridge.py tests/p3/test_cu_login_guard.py tests/p3/mcp_sdk_validation.py; do
  [ -f "$f" ] || { echo "SKIP (missing) $f"; continue; }
  out=$(timeout 600 python3 -u "$f" 2>&1)
  code=$?
  line=$(echo "$out" | grep -E "通过 [0-9]+ ?/|PASS|全部通过|passed" | tail -1)
  if [ $code -ne 0 ]; then
    FAILSUITE=1
    echo "❌ $f (exit=$code) ${line}"
    echo "$out" | tail -25
  else
    echo "✅ $f ${line}"
  fi
  redis-cli -p 6399 flushall >/dev/null 2>&1
done
echo "════════════════════════════════════"
[ $FAILSUITE -eq 0 ] && echo "REGRESSION: ALL GREEN" || echo "REGRESSION: HAS FAILURE"
exit $FAILSUITE
