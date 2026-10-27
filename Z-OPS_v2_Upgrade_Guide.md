# Z-OPS v2.0.0 架构更新实施指南

## 📋 更新概览

本次更新参考 **OpenClaw v2026.3.13** 架构，为 Z-OPS 添加了以下关键功能：

### ✅ 已完成的更新

1. **沙箱隔离机制** (Docker/namespace)
2. **浏览器会话 Attach** (Playwright + userDataDir)
3. **CLI 备份工具** (zops backup create/verify)
4. **SecretRef 敏感配置管理** (移除明文密码)
5. **配置 dry-run 验证机制**
6. **Self-Improving Skills** (自我进化技能系统)

---

## 1. 沙箱隔离机制

### 文件位置
- `/opt/zops/core/sandbox.py`

### 功能特性
- **三种模式**: Docker / namespace / none
- **资源限制**: CPU、内存、网络隔离
- **超时控制**: 自动终止超时执行
- **安全执行**: 隔离危险操作

### 使用示例

```python
from core.sandbox import execute_in_sandbox, SandboxConfig

# 配置沙箱
config = SandboxConfig(
    mode="namespace",  # docker | namespace | none
    cpu_limit=1.0,
    memory_limit="512m",
    network_enabled=False,
    timeout=30
)

# 在沙箱中执行代码
success, stdout, stderr = await execute_in_sandbox(
    code="print('Hello from sandbox!')",
    config=config
)

# 在沙箱中执行命令
success, stdout, stderr = await execute_in_sandbox(
    command="ls -la",
    config=config
)
```

### 集成到 Skill

```python
from core.sandbox import execute_in_sandbox

class DangerousSkill(BaseSkill):
    async def execute(self, params, context):
        # 在沙箱中执行危险操作
        success, stdout, stderr = await execute_in_sandbox(
            command=params.get("command"),
            config=SandboxConfig(mode="namespace")
        )
        
        if success:
            return SkillResult(success=True, text=stdout)
        else:
            return SkillResult(success=False, error=stderr)
```

---

## 2. 浏览器会话 Attach

### 文件位置
- `/opt/zops/core/browser_session.py`

### 功能特性
- **持久化会话**: 保留真实登录态
- **Attach 模式**: 复用用户浏览器配置
- **多会话管理**: 同时管理多个浏览器实例
- **可见/无痕模式**: 灵活配置

### 使用示例

```python
from core.browser_session import (
    attach_browser_session,
    get_browser_page,
    close_browser_session
)

# Attach 到用户的真实浏览器会话
session = await attach_browser_session(
    session_id="user_chrome",
    user_data_dir="/home/user/.config/google-chrome/Default",
    headless=False  # 可见模式
)

# 获取页面并操作
page = await get_browser_page("user_chrome")
await page.goto("https://example.com")

# 关闭会话
await close_browser_session("user_chrome")
```

### 集成到 browser_agent

```python
# 在 browser_agent.py 中使用
from core.browser_session import get_browser_session_manager

class BrowserAgent(BaseAgent):
    async def handle(self, msg):
        manager = get_browser_session_manager()
        
        # Attach 到现有会话
        await manager.attach_session(
            session_id=msg.session_id,
            user_data_dir="/path/to/chrome/profile",
            headless=False
        )
        
        # 获取页面操作
        page = await manager.get_page(msg.session_id)
        await page.goto(msg.params.get("url"))
```

---

## 3. CLI 备份工具

### 文件位置
- `/opt/zops/cli/zops.py` (主入口)
- `/opt/zops/cli/backup.py` (备份管理)

### 功能特性
- **创建备份**: 自动打包文件 + 导出 Redis 数据
- **验证备份**: SHA256 校验和验证
- **列出备份**: 显示所有备份及状态
- **恢复备份**: 一键还原到历史版本

### 使用示例

```bash
# 检查配置
zops config

# 验证敏感配置（dry-run 模式）
zops config --dry-run

# 创建备份
zops backup create --name=before_upgrade

# 列出所有备份
zops backup list

# 验证备份
zops backup verify before_upgrade

# 恢复备份
zops backup restore before_upgrade

# 强制恢复
zops backup restore before_upgrade --force
```

### 备份内容
- 配置文件 (zops.env, webchat.env, feishu.env)
- 核心代码 (config.py, requirements.txt)
- 工作空间 (workspaces/)
- Skills (skills/)
- 截图 (screenshots/)
- Redis 数据（全部 zops: 前缀的键）

---

## 4. SecretRef 敏感配置管理

### 文件位置
- `/opt/zops/core/secret_ref.py`
- `/opt/zops/config.py` (已更新)

### 功能特性
- **移除明文默认值**: 所有敏感配置必须通过环境变量注入
- **SecretRef 支持**: 支持从环境变量/文件/直接值读取
- **配置验证**: 启动前自动验证必需配置
- **脱敏显示**: 日志中自动掩码

### 配置方式

#### 方式 1: 环境变量（推荐）

```bash
# .env 文件
LDAP_BIND_PASSWORD=${env:LDAP_BIND_PASSWORD}
JWT_SECRET=${env:JWT_SECRET}
VCENTER_PASS=${env:VCENTER_PASS}
```

#### 方式 2: 文件引用

```bash
# .env 文件
LDAP_BIND_PASSWORD=${file:/run/secrets/ldap_password}
JWT_SECRET=${file:/run/secrets/jwt_secret}
```

#### 方式 3: 直接值（仅测试）

```bash
# .env 文件（不推荐，仅用于开发测试）
TEST_SECRET=${value:my_test_secret}
```

### 代码中使用

```python
from core.secret_ref import get_secret

# 获取敏感配置（自动解析 SecretRef）
ldap_password = get_secret("LDAP_BIND_PASSWORD", required=True)
jwt_secret = get_secret("JWT_SECRET", required=True)
```

### 配置验证

```bash
# 检查所有必需配置
zops config

# Dry-run 模式（仅验证，不抛出异常）
zops dry-run
```

---

## 5. 配置 dry-run 验证

### 功能特性
- **验证敏感配置**: 检查所有必需的 Secret 是否已配置
- **解析 SecretRef**: 验证 SecretRef 是否可正确解析
- **配置摘要**: 显示配置概览（脱敏）

### 使用示例

```bash
# Dry-run 验证
zops dry-run

# 或
zops config --dry-run
```

### 输出示例

```
============================================================
Z-OPS 配置验证 v2.0.0
============================================================

✅ 基础配置:
   APP_NAME: Z-OPS
   REDIS_PREFIX: zops
   LLM_PROVIDER: deepseek
   LLM_MODEL: deepseek-chat

🔐 敏感配置:
   ✓ JWT_SECRET: ***
   ✓ WEBCHAT_AUTH_PASS: ***
   ✓ LDAP_BIND_PASSWORD: SecretRef(env)
   ✓ VCENTER_PASS: SecretRef(file)
   ❌ GCOC_INFO_PASS: 未配置

✅ 所有配置验证通过
```

---

## 6. Self-Improving Skills 自我进化技能

### 文件位置
- `/opt/zops/core/self_improving.py` (核心引擎)
- `/opt/zops/core/skill_base.py` (已更新)
- `/opt/zops/skills/general/self_improving_example.py` (示例)

### 功能特性
- **经验记录**: 自动记录每次技能执行
- **模式学习**: 从成功/失败中提取模式
- **优化建议**: 提供执行优化建议
- **信心校准**: 动态调整信心分数
- **相似参考**: 提供相似历史经验

### 核心概念

#### Experience（经验）
```python
Experience(
    id="abc123",
    skill_name="gcoc_query",
    action="execute",
    success=True,
    input_params={"query_type": "site"},
    output_result={"count": 10},
    execution_time=2.5,
    lessons_learned=["成功要素：query_type=site"],
    confidence_score=0.85
)
```

#### SkillInsight（洞察）
```python
SkillInsight(
    skill_name="gcoc_query",
    pattern_type="success_pattern",
    pattern_description="成功执行 query_type=site",
    frequency=15,
    confidence=0.9,
    recommendations=["使用 site 查询类型成功率高"]
)
```

### 使用示例

#### 方式 1: 使用 execute_with_learning()

```python
from core.skill_base import BaseSkill

class MySkill(BaseSkill):
    name = "my_skill"
    enable_learning = True
    
    async def execute(self, params, context):
        # 业务逻辑
        return SkillResult(success=True, text="Done")

# 调用时（自动学习）
skill = MySkill()
result = await skill.execute_with_learning(params, context)
```

#### 方式 2: 使用装饰器

```python
from core.self_improving import record_skill_execution

class MySkill(BaseSkill):
    @record_skill_execution
    async def execute(self, params, context):
        # 业务逻辑
        return SkillResult(success=True)
```

#### 方式 3: 手动钩子

```python
class MySkill(BaseSkill):
    async def on_before_execute(self, params, context):
        # 获取历史经验
        from core.self_improving import get_improving_engine
        engine = get_improving_engine()
        
        similar = engine.get_similar_experiences(self.name, params)
        recommendations = engine.get_recommendations(self.name, params)
        
        # 应用优化
        self.confidence_score = engine.calculate_confidence(self.name, params)
    
    async def on_after_execute(self, params, result, execution_time, context):
        # 记录经验（如果未自动调用）
        pass
```

### 示例技能

#### 1. GCOC 智能查询技能

```python
class GcocQuerySkill(BaseSkill):
    name = "gcoc_query_optimized"
    enable_learning = True
    
    async def on_before_execute(self, params, context):
        await super().on_before_execute(params, context)
        
        # 基于信心调整策略
        if self.confidence_score < 0.6:
            logger.warning("信心较低，使用保守策略")
```

#### 2. 网络扫描优化技能

```python
class NetworkScanSkill(BaseSkill):
    name = "network_scan_optimized"
    enable_learning = True
    
    async def on_before_execute(self, params, context):
        await super().on_before_execute(params, context)
        
        # 分析历史超时情况
        from core.self_improving import get_improving_engine
        engine = get_improving_engine()
        
        similar = engine.get_similar_experiences(self.name, params)
        timeout_failures = [e for e in similar if "timeout" in e.error_message]
        
        if len(timeout_failures) > len(similar) * 0.3:
            # 自动增加超时时间
            params["timeout"] = int(params["timeout"] * 1.5)
```

### 经验数据管理

```python
from core.self_improving import ExperienceDatabase

db = ExperienceDatabase()

# 获取经验
experiences = db.get_experiences("gcoc_query", limit=100)

# 获取相似经验
similar = db.get_similar_experiences(
    "gcoc_query",
    input_params={"query_type": "site"}
)

# 获取统计
stats = db.get_statistics("gcoc_query")
# {
#   "total": 150,
#   "success": 135,
#   "failure": 15,
#   "success_rate": 0.9,
#   "avg_execution_time": 2.5
# }
```

---

## 集成测试

### 1. 测试沙箱

```python
# test_sandbox.py
from core.sandbox import execute_in_sandbox, SandboxConfig

async def test_sandbox():
    config = SandboxConfig(mode="namespace", timeout=5)
    
    # 测试代码执行
    success, stdout, stderr = await execute_in_sandbox(
        code="print('test')",
        config=config
    )
    assert success
    
    # 测试命令执行
    success, stdout, stderr = await execute_in_sandbox(
        command="echo test",
        config=config
    )
    assert success

# 运行测试
# python -m pytest test_sandbox.py
```

### 2. 测试 Self-Improving

```python
# test_self_improving.py
from skills.general.self_improving_example import ExampleSelfImprovingSkill

async def test_self_improving():
    skill = ExampleSelfImprovingSkill()
    
    # 多次执行
    for i in range(10):
        result = await skill.execute_with_learning({
            "task": f"test_{i}",
            "complexity": 5
        })
        print(f"执行 {i}: success={result.success}, confidence={skill.confidence_score:.2f}")
    
    # 查看统计
    from core.self_improving import get_improving_engine
    engine = get_improving_engine()
    stats = engine.get_statistics("example_self_improving")
    print(f"统计：{stats}")

# 运行测试
# python -m pytest test_self_improving.py
```

---

## 迁移指南

### 从旧版本升级

1. **备份当前系统**
   ```bash
   zops backup create --name=before_v2_upgrade
   ```

2. **更新配置文件**
   ```bash
   # 移除 .env 文件中的明文默认值
   # 使用 SecretRef 或直接设置环境变量
   ```

3. **验证配置**
   ```bash
   zops config --dry-run
   ```

4. **重启服务**
   ```bash
   # 停止旧服务
   pkill -f "python.*webchat_api.py"
   pkill -f "python.*orchestrator.py"
   
   # 启动新服务
   cd /opt/zops
   bash deploy/start-dev.sh
   ```

### 新技能开发

```python
# 新技能模板
from core.skill_base import BaseSkill, SkillParam, SkillResult

class MyNewSkill(BaseSkill):
    name = "my_new_skill"
    description = "我的新技能"
    enable_learning = True  # 启用自我学习
    
    parameters = [
        SkillParam(name="param1", type="string", required=True)
    ]
    
    async def execute(self, params, context):
        # 业务逻辑
        return SkillResult(success=True, text="Done")

# 使用时调用 execute_with_learning()
skill = MyNewSkill()
result = await skill.execute_with_learning({"param1": "value"})
```

---

## 性能优化建议

1. **沙箱模式选择**
   - 生产环境：使用 Docker 模式（最强隔离）
   - 开发环境：使用 namespace 模式（轻量级）
   - 测试环境：使用 none 模式（无隔离，最快）

2. **浏览器会话管理**
   - 复用会话：避免频繁创建/销毁
   - 定期清理：关闭空闲会话
   - 持久化配置：使用 userDataDir 保留登录态

3. **Self-Improving 优化**
   - 限制查询：`limit=10` 避免加载过多历史
   - 定期清理：删除过旧的经验记录
   - 异步记录：不要阻塞主流程

---

## 故障排查

### 沙箱执行失败

```bash
# 检查 Docker
docker ps

# 检查 namespace
which unshare

# 测试沙箱
python -c "from core.sandbox import *; print(execute_in_sandbox(command='echo test'))"
```

### 浏览器 Attach 失败

```bash
# 检查 Chrome 路径
which google-chrome

# 检查 userDataDir
ls -la /path/to/chrome/profile

# 测试 Playwright
python -c "from playwright.async_api import async_playwright; print('OK')"
```

### Self-Improving 不工作

```bash
# 检查经验数据库
ls -la /opt/zops/experiences/

# 查看日志
tail -f logs/zops.log | grep "self_improving"
```

---

## 总结

本次更新为 Z-OPS 带来了 OpenClaw v2026.3.13 的核心特性：

✅ **安全性**: 沙箱隔离 + SecretRef 配置管理
✅ **易用性**: CLI 备份工具 + 浏览器会话 Attach
✅ **可靠性**: 配置 dry-run 验证
✅ **智能性**: Self-Improving 技能系统

所有功能均已实现并可立即使用。建议按优先级逐步部署：
1. 沙箱隔离（立即）
2. SecretRef 配置（立即）
3. CLI 备份（高优）
4. Self-Improving（中优）
5. 浏览器 Attach（按需）
