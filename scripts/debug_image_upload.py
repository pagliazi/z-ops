"""调试：飞书 tenant_token 获取 + 图片上传 im/v1/images 完整响应。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/opt/zops")

import httpx
import config


async def main():
    # 1) 读取凭据（同 feishu_bot 逻辑：Redis dyn 优先）
    app_id = config.FEISHU_APP_ID
    app_secret = config.FEISHU_APP_SECRET
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        raw = await r.get(config.KEY_NMS_DYN_CFG)
        dyn = json.loads(raw) if raw else {}
        app_id = dyn.get("feishu_app_id") or app_id
        app_secret = dyn.get("feishu_app_secret") or app_secret
        print(f"[cfg] app_id={app_id!r} app_secret={'***' + app_secret[-4:] if app_secret else None!r}")
        await r.aclose()
    except Exception as e:
        print(f"[cfg] redis 读取失败: {e}")

    # 2) 获取 tenant_access_token
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret})
        print(f"[token] HTTP {resp.status_code}")
        data = resp.json()
        print(f"[token] resp: {json.dumps(data, ensure_ascii=False)[:500]}")
        token = data.get("tenant_access_token") or ""

        if not token:
            print("FAIL: 未拿到 tenant_access_token，终止")
            return

        # 3) 上传图片
        shot_dir = config.BASE_DIR / "webchat_frontend" / "nms_shots"
        files = sorted(shot_dir.glob("*.png"), key=os.path.getmtime, reverse=True)
        if not files:
            print("FAIL: 无截图文件")
            return
        f = files[0]
        print(f"[upload] 文件: {f.name} ({f.stat().st_size} bytes)")
        with open(f, "rb") as fh:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("shot.png", fh, "image/png")})
        print(f"[upload] HTTP {resp.status_code}")
        print(f"[upload] resp: {json.dumps(resp.json(), ensure_ascii=False)[:800]}")


asyncio.run(main())
