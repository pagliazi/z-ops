# Z-OPS Redis 升级与恢复演练报告 — task-c17 / 2026-10-13

## 升级结论

生产 Redis **6.0.16 → 7.2.10**（源码构建，二进制替换），P1 Streams 门槛（≥6.2）达标。
升级经用户授权（"1\2 都推进"），按隔离演练 → 备份 → 替换 → 验证的顺序执行。

## 过程记录

1. **隔离演练**：6.0 数据副本（RDB 20MB + AOF 38MB）载入 7.2.10 新实例（6400 端口）——
   838 键完整、AOF 自动迁移 multi-part（appendonlydir）、XPENDING IDLE/XAUTOCLAIM 可用。
2. **备份**（`backups/redis-upgrade-20261013/`）：dump.rdb + appendonly.aof + redis.conf；
   旧二进制保留为 `/usr/bin/redis-server-6.0.16.bak`；构建产物 `/usr/local/bin/redis-server-7.2.10`。
3. **替换**：systemd unit `/usr/bin/redis-server` 指向 7.2.10（distro unit 不变）。
4. **两个升级坑（已修复并记录）**：
   - 源码构建默认 **不含 systemd 通知支持** → `Type=notify` 超时循环；
     修复：drop-in `/etc/systemd/system/redis-server.service.d/10-type-simple.conf`（Type=simple）。
   - distro 配置里的 `user default on nopass ~* +@all` 在 7.x 解析后**通道权限为空**
     （`resetchannels`）→ 全部 SUBSCRIBE 报 NoPermissionError、agent 集体重启循环；
     修复：redis.conf 该行加 `&*`（`~* &* +@all`），Redis 重启后 agents 5 秒内全体自愈恢复
     （zops-agent@.service 双向自愈架构按设计工作，验证了 10-09 的投资）。
5. **升级后验证**：13/13 服务 active、10/10 agent 心跳在线、e2e 对话正常、
   task_runtime readiness 全绿（streams_allowed=True）、dbsize/键完整。

## 回退手册（如需回到 6.0.16）

```bash
systemctl stop redis-server.service
cp /usr/bin/redis-server-6.0.16.bak /usr/bin/redis-server
# 注意: 7.2 已把 AOF 迁移为 appendonlydir/ 多部分; 6.0 读不了多部分 AOF
#   → 删除 /var/lib/redis/appendonlydir, 还原备份的单文件 AOF:
cp backups/redis-upgrade-20261013/appendonly.aof /var/lib/redis/appendonly.aof
cp backups/redis-upgrade-20261013/dump.rdb /var/lib/redis/dump.rdb
# redis.conf 的 user 行含 &* 在 6.0 下不识别? 6.0 忽略 & 语法会报错 → 还原备份 conf:
cp backups/redis-upgrade-20261013/redis.conf.bak /etc/redis/redis.conf
rm -f /etc/systemd/system/redis-server.service.d/10-type-simple.conf
systemctl daemon-reload && systemctl start redis-server.service
systemctl restart zops-backend.service zops-feishu.service 'zops-agent@*'
# 回退会丢失升级后新写入的数据 (回退点之后), 属最后手段
```

## 运维注意事项

- `zops:tr:queue`（Streams）与任务/事件键无自动清理（SPEC），容量监测已内置
  （`capacity_check`: 内存 8G 线 / 活跃任务 1000 线, 超限受控拒收）。
- redis 包仍在 unattended-upgrades 黑名单（`/etc/apt/apt.conf.d/51-zops-redis-blacklist`）——
  现在 7.2 二进制是手动放置的，distro 包升级不会覆盖它，但 apt 重装 redis-server 会；保持黑名单。
- 故障边界不变：AOF everysec 存在 ~1s 丢失窗口，不承诺零丢失/exactly-once。
