#!/usr/bin/env python3
"""真实链路验证: webchat → orchestrator → ?, 观察路由去向 + 完整回复耗时"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import redis

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

QUESTIONS = [
    "查一下ESXI-02上有什么机器",
    "ESXI-02主机状态",
]

for q in QUESTIONS:
    msg_id = f"verify-real-{int(time.time()*1000)}"
    reply_ch = f"zops:reply:{msg_id}"
    payload = {
        "msg_id": msg_id, "source": "webchat", "target": "orchestrator",
        "action": "", "params": {},
        "text": q, "timestamp": time.time(), "reply_to": "",
        "channel_source": "webchat", "session_id": f"verify-{msg_id}",
        "username": "verify_script",
    }
    ps = r.pubsub()
    ps.subscribe(reply_ch)
    print(f">>> [真实链路] 提问: {q}")
    t0 = time.time()
    r.publish("zops:orchestrator", json.dumps(payload, ensure_ascii=False))
    got_reply = False
    first_delta = None
    while time.time() - t0 < 180:
        m = ps.get_message(ignore_subscribe_messages=True)
        if not m or m.get("type") != "message":
            time.sleep(0.3); continue
        try:
            d = json.loads(m["data"])
        except Exception:
            continue
        if d.get("_type") == "progress":
            print(f"   [{time.time()-t0:5.1f}s][progress@{d.get('source','?')}] {d.get('text','')[:80]}")
        elif d.get("_type") == "delta":
            if first_delta is None:
                first_delta = time.time() - t0
                print(f"   ⚡ [{first_delta:5.1f}s] 首个流式增量 (来自 {d.get('source','?')})")
        elif "result" in d:
            got_reply = True
            print(f"   ✔ [{time.time()-t0:5.1f}s] 最终回复 来自={d.get('source','?')} error={d.get('error')}")
            text = ""
            res = d.get("result") or {}
            if isinstance(res, dict):
                text = res.get("text", "")
            print(f"   回复前500字: {text[:500]}")
            open("/opt/zops/scripts/prod_real_reply.txt", "w").write(text)
            break
    if not got_reply:
        print(f"   ✗ 180s 未收到最终回复")
    ps.close()
    print()
