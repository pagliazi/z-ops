#!/bin/bash
# ═══ Z-OPS 开机自检 ═══
# 由 zops-postboot-check.service 在每次系统启动后自动调用。
# 等待服务充分启动 → 逐项检查 Redis/agent 群/前后端/飞书/n8n/端到端对话
# → 结果追加 logs/post-reboot-check.log 并推送飞书通知 (尽力而为)。

ZOPS=/opt/zops
LOG="$ZOPS/logs/post-reboot-check.log"
BOOT=$(uptime -s)

# 开机后 150 秒再开始检查, 给 agent 群/LLM 网关留启动时间
WAIT_UNTIL=$(( $(date -d "$BOOT" +%s) + 150 ))
while [ "$(date +%s)" -lt "$WAIT_UNTIL" ]; do sleep 5; done

FAIL=0
UNITS="redis-server nginx docker zops-backend zops-frontend zops-feishu"
# nms_agent 除外: 2026-10-09 起预期停用 (NMS 密码待人工更新, 已加防锁号熔断)
for a in orchestrator general_agent ops_agent network_agent vm_agent dev_agent browser_agent opscore_agent kb_agent; do
    UNITS="$UNITS zops-agent@$a"
done

REPORT=""
REPORT+="═══ Z-OPS 开机自检 $(date '+%F %T') (系统启动于 $BOOT, 内核 $(uname -r)) ═══\n"
for u in $UNITS; do
    s=$(systemctl is-active "$u" 2>/dev/null)
    [ "$s" = active ] || FAIL=$((FAIL+1))
    REPORT+="$(printf '  %-38s %s' "$u" "$s")\n"
done

# docker 容器 n8n
NS=$(docker inspect n8n --format '{{.State.Status}}' 2>/dev/null)
[ "$NS" = running ] || FAIL=$((FAIL+1))
REPORT+="$(printf '  %-38s %s' "docker/n8n" "${NS:-absent}")\n"

# nms_agent: 预期 inactive (停用待更新 NMS_PASSWORD), 不计入异常
REPORT+="$(printf '  %-38s %s' "zops-agent@nms_agent" "$(systemctl is-active zops-agent@nms_agent 2>/dev/null)/$(systemctl is-enabled zops-agent@nms_agent 2>/dev/null) 预期停用-待NMS密码")\n"

# 端到端对话 (铸临时 token → 前端 3000 → 后端 → orchestrator → agent)
# 测完自删会话, 避免自检记录污染用户会话列表 (2026-10-18)
CHAT=OK
TOKEN=$(python3 -c "import secrets;print(secrets.token_hex(16))" 2>/dev/null)
if [ -n "$TOKEN" ]; then
    redis-cli SET "zops:token:$TOKEN" "healthcheck" EX 300 >/dev/null 2>&1
    STREAM=$(curl -sN --max-time 60 -X POST http://127.0.0.1:3000/api/chat \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d '{"message":"开机自检：请只回复两个字：正常"}' 2>/dev/null)
    N=$(printf '%s' "$STREAM" | grep -c '"type": "done"')
    [ "$N" -ge 1 ] || { CHAT=FAIL; FAIL=$((FAIL+1)); }
    SID=$(printf '%s' "$STREAM" | grep -m1 -o '"session_id": "[^"]*"' | cut -d'"' -f4)
    [ -n "$SID" ] && curl -s --max-time 8 -X DELETE "http://127.0.0.1:7789/api/chat/sessions/$SID" \
        -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1
else
    CHAT=SKIP; FAIL=$((FAIL+1))
fi
REPORT+="$(printf '  %-38s %s' "端到端对话(webchat)" "$CHAT")\n"

if [ "$FAIL" -eq 0 ]; then VERDICT="✅ 全部正常"; else VERDICT="❌ ${FAIL} 项异常, 需人工检查"; fi
REPORT+="═══ 结论: $VERDICT ═══\n"

printf "$REPORT" >> "$LOG"

# 飞书通知 (尽力而为, 失败不影响自检结果)
MSG=$(python3 -c "
import json,sys
print(json.dumps({'text': '🖥️ Z-OPS 服务器重启自检完成 (${BOOT##* }启动, 内核 $(uname -r)) 结论: $VERDICT', 'topic': 'ops', 'source': 'post-reboot-check'}, ensure_ascii=False))" 2>/dev/null)
[ -n "$MSG" ] && redis-cli PUBLISH zops:notify:feishu "$MSG" >/dev/null 2>&1

exit 0
