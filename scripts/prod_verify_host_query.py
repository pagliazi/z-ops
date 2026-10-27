#!/usr/bin/env python3
"""最终生产验证: 订阅 zops:reply:{msg_id} 拿完整回复 + 监听 delta 首字时间"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import redis

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

msg_id = f"verify-{int(time.time()*1000)}"
reply_ch = f"zops:reply:{msg_id}"
payload = {
    "msg_id": msg_id, "source": "verify_script", "target": "gcoc_agent",
    "action": "smartops_vms", "params": {"search": "ESXI-02"},
    "text": "查询 ESXI-02 主机上的虚拟机列表",
    "timestamp": time.time(), "reply_to": "",
}
ps = r.pubsub()
ps.subscribe(reply_ch)
print(f">>> 发送查询 (msg_id={msg_id}), 订阅 {reply_ch}")
t0 = time.time()
first_delta_at = None
delta_bytes = 0
r.publish("zops:gcoc_agent", json.dumps(payload))

deadline = time.time() + 180
while time.time() < deadline:
    m = ps.get_message(timeout=1.0)
    if not (m and m.get("type") == "message"):
        continue
    try:
        d = json.loads(m["data"])
    except Exception:
        continue
    if d.get("_type") == "progress":
        print(f"   [progress {time.time()-t0:.1f}s] {d.get('text','')[:60]}")
    elif d.get("_type") == "delta":
        if first_delta_at is None:
            first_delta_at = time.time() - t0
            print(f"⚡ 首个流式增量到达: {first_delta_at:.1f}s")
        delta_bytes += len(d.get("text", ""))
    elif "result" in d and d.get("reply_to") == msg_id:
        total = time.time() - t0
        text = str(d.get("result", {}).get("text", ""))
        print(f"✅ 完整回复到达: 总耗时 {total:.1f}s, 回复 {len(text)} 字符, 流式增量累计 {delta_bytes} 字符")
        Path(__file__).parent.joinpath("prod_final_reply.txt").write_text(text)
        # 质检
        vmnames = ["Deploy-Server", "ERPDB-uX5F", "FR_node2-q7A8", "ceph-node-01",
                   "T100-hw3H", "tw1616-fMMR", "tw1626-Styk", "JustPowerAGGR-LRy5"]
        present = [n for n in vmnames if n in text]
        print(f"质检: 抽样8台VM在回复中出现 {len(present)}/8 | 台数'18台'或18在回复中: {'18' in text} | 有表格: {'|' in text and '---' in text}")
        print(f"完整回复已存 scripts/prod_final_reply.txt")
        break
else:
    print("?? 超时未收到")
ps.close()
