#!/bin/bash
# ═══ Z-OPS Agents — systemd 包装器 ═══
# 2026-10-09: agent 群与 feishu_bot 已迁入 systemd 托管
#   (zops-agent@<name>.service / zops-feishu.service, Restart=always)
# 本脚本保留原有 start|stop|restart|status 用法, 内部转发 systemctl,
# 防止误用旧 nohup 方式造成双实例。历史实现备份于 start-agents.sh.bak-legacy。

UNITS=(orchestrator general_agent ops_agent network_agent vm_agent dev_agent browser_agent nms_agent opscore_agent kb_agent computer_agent)

agent_units() { for a in "${UNITS[@]}"; do echo "zops-agent@$a.service"; done; }

case "${1:-status}" in
    start)   exec systemctl start $(agent_units) zops-feishu.service ;;
    stop)    exec systemctl stop $(agent_units) zops-feishu.service ;;
    restart) exec systemctl restart $(agent_units) zops-feishu.service ;;
    status)
        echo "═══ Agent 状态 (systemd 托管) ═══"
        for a in "${UNITS[@]}"; do
            state=$(systemctl is-active "zops-agent@$a.service" 2>/dev/null || true)
            if [ "$state" = "active" ]; then
                pid=$(systemctl show -p MainPID --value "zops-agent@$a.service")
                printf "  ✓ %-18s RUNNING  PID=%s\n" "$a" "$pid"
            else
                printf "  ✗ %-18s %s\n" "$a" "$(echo "${state:-unknown}" | tr 'a-z' 'A-Z')"
            fi
        done
        fs=$(systemctl is-active zops-feishu.service 2>/dev/null || true)
        echo ""
        echo "  feishu_bot: ${fs:-unknown}  (zops-feishu.service)"
        echo ""
        echo "单 agent 操作: systemctl restart zops-agent@opscore_agent  |  日志: tail -f logs/opscore_agent.log"
        ;;
    *) echo "用法: $0 {start|stop|restart|status}"; exit 1 ;;
esac
