"""
Bootstrap: 把本会话捞出来的 29 条 Z-OPS 相关 Qoder memory 沉到 wiki/concepts/

这是一次性脚本，首轮导入后可保留用于全量重建。后续新增的 memory 应通过
core.memory.qoder_memory_bridge.import_qoder_memories() 增量导入。
"""
from core.memory.qoder_memory_bridge import import_qoder_memories

MEMORIES = [
    # ---- Z-OPS 系统运维与部署管理 ----
    {
        "title": "Z-OPS开发环境启动脚本",
        "category": "history_task_reference_files",
        "keywords": ["start-dev.sh", "Z-OPS", "启动脚本"],
        "content": "- /zops/deploy/start-dev.sh",
    },
    {
        "title": "Z-OPS服务重启需用pkill -9 -f强制清理残留进程",
        "category": "common_pitfalls_experience",
        "keywords": ["Z-OPS", "pkill -9", "干净重启", "进程清理"],
        "content": "在Z-OPS项目中执行干净重启时，仅用`pkill -f`无法彻底终止残留Agent进程（如 multiple general_agent 实例），必须使用`pkill -9 -f`强制终止，否则新进程启动会因端口/资源冲突失败。",
    },
    {
        "title": "Z-OPS服务干净重启标准流程",
        "category": "task_flow_experience",
        "keywords": ["Z-OPS", "干净重启", "进程清理"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[检测运行中的Z-OPS进程] --> B[执行pkill -f匹配正则]\n"
            "    B --> C[检查残留进程]\n"
            "    C --> D{是否仍有残留？}\n"
            "    D -->|是| E[执行pkill -9 -f强制终止]\n"
            "    D -->|否| F[启动新服务]\n"
            "    E --> G[二次验证pgrep无输出]\n"
            "    G --> F\n"
            "```"
        ),
    },
    {
        "title": "项目版本号",
        "category": "project_environment_configuration",
        "keywords": ["智OC", "v2.0.0", "版本"],
        "content": "项目当前版本为智OC v2.0.0，该版本号用于构建标识、升级管理和问题追踪。",
    },
    {
        "title": "Web服务端口",
        "category": "project_environment_configuration",
        "keywords": ["Web端口", "7789"],
        "content": "项目Web服务默认监听端口为7789，所有HTTP/HTTPS外部访问均通过此端口。",
    },
    {
        "title": "Redis缓存前缀",
        "category": "project_environment_configuration",
        "keywords": ["Redis", "zops:", "缓存前缀"],
        "content": "项目Redis缓存统一使用前缀 'zops:'，所有键名均以此开头，用于多租户/多实例隔离。",
    },
    {
        "title": "默认LLM模型",
        "category": "project_environment_configuration",
        "keywords": ["deepseek", "deepseek-chat", "LLM模型"],
        "content": "当前默认使用的LLM模型为 deepseek/deepseek-chat，该配置影响所有未显式指定模型的推理请求。",
    },
    {
        "title": "zops.env token配置生效机制",
        "category": "project_environment_configuration",
        "keywords": ["zops.env", "token", "服务重启", "配置生效"],
        "content": "修改 zops.env 中的 token 配置后，必须重启Z-OPS服务才能使新 token 生效。",
    },
    {
        "title": "Z-OPS系统升级依据文档",
        "category": "project_configuration",
        "keywords": ["Z-OPS", "升级计划", "Z-OPS-UpgradePlan.md"],
        "content": "Z-OPS系统升级工作必须严格依据文档 Z-OPS-UpgradePlan.md 中定义的步骤、范围和约束执行。",
    },

    # ---- OneAPI 集成 ----
    {
        "title": "接入OneAPI作为LLM Provider的完整实现流程",
        "category": "task_flow_experience",
        "keywords": ["OneAPI", "LLM Provider", "webchat_api.py", "base.py"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[需求:接入OneAPI作为新LLM Provider] --> B[在webchat_api.py中注册OneAPI provider配置块]\n"
            "    B --> C[在base.py中扩展_call_openai_compatible方法, 新增provider==\"oneapi\"分支]\n"
            "    C --> D[在webchat_frontend.html中同步扩展表单state与初始化逻辑, 增加oneapi_key/oneapi_base_url字段]\n"
            "    D --> E[更新CLI doctor检查逻辑以兼容OneAPI配置项]\n"
            "```"
        ),
    },
    {
        "title": "OneAPI接入功能相关文件",
        "category": "history_task_reference_files",
        "keywords": ["OneAPI", "LLM Provider"],
        "content": "- /zops/webchat_api.py\n- /zops/agents/base.py\n- /zops/webchat_frontend.html",
    },
    {
        "title": "OneAPI Keys token支持",
        "category": "project_introduction",
        "keywords": ["OneAPI", "token认证", "API鉴权"],
        "content": "项目需支持 OneAPI Keys token 认证机制，作为新增的API访问凭证管理能力，用于统一接入和鉴权外部AI服务调用。",
    },
    {
        "title": "Z-OPS系统集成OneAPI兼容网关的任务流程",
        "category": "task_flow_experience",
        "keywords": ["OneAPI", "LLM配置", "Z-OPS", "OpenAI兼容"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[需求:为Z-OPS接入OneAPI兼容网关] --> B[新增ONEAPI_API_KEY/ONEAPI_BASE_URL环境变量]\n"
            "    B --> C[在zops.env中添加OneAPI配置段]\n"
            "    C --> D[扩展webchat_api.py中LLM_PROVIDERS枚举, 预置6个OneAPI模型]\n"
            "    D --> E[修改agents/base.py, 使_oneapi_call路径独立使用base_url+token]\n"
            "    E --> F[更新webchat_frontend.html, 在AdminLLM面板新增OneAPI配置Tab]\n"
            "    F --> G[增强cli/main.py doctor命令, 校验ONEAPI_API_KEY是否设置]\n"
            "```"
        ),
    },
    {
        "title": "OneAPI集成相关修改文件",
        "category": "history_task_reference_files",
        "keywords": ["OneAPI", "Z-OPS", "LLM配置"],
        "content": (
            "- /zops/config.py\n"
            "- /zops/zops.env\n"
            "- /zops/webchat_api.py\n"
            "- /zops/agents/base.py\n"
            "- /zops/webchat_frontend.html\n"
            "- /zops/cli/main.py"
        ),
    },
    {
        "title": "OneAPI Keys token命名与生成规则",
        "category": "project_introduction",
        "keywords": ["OneAPI", "token命名", "oneapi:*"],
        "content": "OneAPI Keys token 由平台自动创建，固定命名为 'oneapi:*' 格式，专用于外部 Chatbox/OpenWebUI 等直接调用场景。",
    },
    {
        "title": "OneAPI Token校验需适配无前缀纯key",
        "category": "common_pitfalls_experience",
        "keywords": ["OneAPI", "token校验", "无前缀", "/v1/chat/completions"],
        "content": "OneAPI Key 的 token 校验逻辑已移除 `oneapi:` 前缀要求，后端所有 token 验证处（如 `/v1/chat/completions`）必须直接比对纯随机 key 字符串，不得再做前缀解析或截断。当前 `webchat_api.py` 中未见该路径校验实现，需额外检查其他文件（如中间件、认证模块）。",
    },
    {
        "title": "OneAPI Key机制重构任务流程",
        "category": "task_flow_experience",
        "keywords": ["OneAPI", "Key重构", "webchat_api.py"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[需求:重构OneAPI Key机制] --> B[定位/webchat_api.py中oneapi_keys_list/create]\n"
            "    B --> C[删除_old_token生成与_mask_token]\n"
            "    C --> D[新增_gen_api_key/_mask_key函数]\n"
            "    D --> E[更新列表接口: name->label, token->key, 新增id字段]\n"
            "    E --> F[更新创建接口: label=oneapi:{name}, id=uuid.hex[:8]]\n"
            "    F --> G[修正Redis遍历变量名: name->_hkey避免覆盖]\n"
            "```"
        ),
    },
    {
        "title": "OneAPI运行时环境配置与数据脱敏机制",
        "category": "project_environment_configuration",
        "keywords": ["OneAPI Base URL", "OpenAI兼容网关", "数据脱敏"],
        "content": "OneAPI Base URL 配置为 https://aigw-noc.example.com/，明确支持 new-api / one-api / chat-api 等 OpenAI 兼容网关路径；系统在 LLM 调用前自动执行数据脱敏与敏感信息掩码。",
    },
    {
        "title": "修复系统信息页当前模型未同步Redis动态配置",
        "category": "task_flow_experience",
        "keywords": ["当前模型", "Redis配置", "LLM_PROVIDER", "overview接口"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[前端页面显示当前模型字段] --> B[定位webchat_frontend.html第1346行]\n"
            "    B --> C[查找后端对应数据源: grep llm_provider webchat_api.py]\n"
            "    C --> D[定位overview接口返回逻辑(line 809)]\n"
            "    D --> E[发现直接使用config.LLM_PROVIDER/MODEL静态值]\n"
            "    E --> F[分析应优先读取Redis中KEY_LLM_PREF动态配置]\n"
            "    F --> G[在overview接口中插入pref_raw读取与fallback逻辑]\n"
            "    G --> H[替换返回字段为cur_provider/cur_model]\n"
            "    H --> I[重启服务验证生效]\n"
            "```"
        ),
    },

    # ---- LDAP 鉴权 / Per-Agent 日志 ----
    {
        "title": "Per-Agent日志需显式清理_file_handler避免重复写入",
        "category": "common_pitfalls_experience",
        "keywords": ["Per-Agent日志", "日志重复", "file_handler", "base.py"],
        "content": "在实现 Per-Agent 日志时，必须显式管理 `_file_handler` 并确保每次调用 `_setup_logging()` 前清除本 Agent 已注册的 `_file_handler`，否则会导致日志重复写入（同一日志行被多次记录）。原代码仅移除控制台 handler，遗漏文件 handler 清理。",
    },
    {
        "title": "LDAP用户未授权时自动发邮件并拒绝登录",
        "category": "common_pitfalls_experience",
        "keywords": ["LDAP认证", "Redis授权检查", "管理员通知邮件"],
        "content": "LDAP 用户首次登录时，若其 username 未存在于 Redis 哈希表 zops:users 中（即未被管理员授权），系统会自动发送 HTML 格式通知邮件给管理员，并抛出 HTTP 403 异常。该逻辑位于 /webchat_api.py 第 276–295 行，是 Z-OPS 系统用户准入控制的关键检查点。",
    },
    {
        "title": "Z-OPS系统LDAP用户API Key生成与鉴权验证流程",
        "category": "task_flow_experience",
        "keywords": ["API Key生成", "OneAPI集成", "LDAP用户鉴权"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[LDAP用户登录请求] --> B[检查Redis zops:users中是否存在username]\n"
            "    B -->|不存在| C[生成HTML邮件通知管理员]\n"
            "    C --> D[抛出HTTPException 403]\n"
            "    B -->|存在| E[生成临时Bearer Token存入Redis zops:token:{token}]\n"
            "    E --> F[调用OneAPI /api/oneapi/keys创建key]\n"
            "    F --> G[验证key对/v1/models的鉴权能力]\n"
            "    G --> H[确认key格式: label=oneapi:{username}, key=32位随机+遮掩, id=8位短UUID]\n"
            "```"
        ),
    },
    {
        "title": "增强log_daily调用以记录model和username参数",
        "category": "task_flow_experience",
        "keywords": ["log_daily", "参数增强", "llm_chat", "base.py"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[定位llm_chat中log_daily调用] --> B[分析日志需记录model和username]\n"
            "    B --> C[修改base.py中log_daily调用]\n"
            "    C --> D[添加model=f\"{provider}/{model}\"参数]\n"
            "    D --> E[添加username=username参数]\n"
            "    E --> F[验证diff无语法错误和意外变更]\n"
            "```"
        ),
    },

    # ---- HA 集成 ----
    {
        "title": "HA列表接口状态说明(测试中)",
        "category": "project_configuration",
        "keywords": ["HA列表", "接口测试", "生产禁用", "数据不准"],
        "content": "项目 API 文档部署在 http://198.18.18.166:8088/docs ，其中 HA 列表接口当前数据不准确，尚未完成调试，仅限内部测试使用，严禁用于生产环境或任何真实业务逻辑依赖。",
    },
    {
        "title": "HA列表接口测试状态说明",
        "category": "project_configuration",
        "keywords": ["HA列表", "API文档", "测试接口", "数据不准确"],
        "content": "http://198.18.18.166:8088/docs 中的 HA 列表接口当前数据不准确，尚未完成调试，仅限测试使用，不可用于生产环境或真实数据依赖。",
    },
    {
        "title": "ha_api.py中动态添加sys.path导入config",
        "category": "common_pitfalls_experience",
        "keywords": ["ha_api.py", "sys.path", "config导入"],
        "content": "在 ha_api.py 中需手动将项目根路径加入 sys.path 才能成功导入 config 模块，否则会因模块找不到而报错。具体操作：`import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`。",
    },
    {
        "title": "HA VM API集成任务流程",
        "category": "task_flow_experience",
        "keywords": ["HA API集成", "zops.env配置", "HA_INTEGRATION.md"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[需求:集成HA VM API] --> B[修改ha_api.py: 替换硬编码URL/TOKEN为config读取]\n"
            "    B --> C[更新zops.env: 在KB_RAG_ENABLED后新增HA配置块]\n"
            "    C --> D[创建HA_INTEGRATION.md: 包含配置说明、调用示例、端点列表]\n"
            "    D --> E[注册gcoc_api端点并验证调用路径]\n"
            "```"
        ),
    },
    {
        "title": "HA VM API集成相关文件",
        "category": "history_task_reference_files",
        "keywords": ["ha_api.py", "zops.env", "HA_INTEGRATION.md"],
        "content": "- /zops/agents/ha_api.py\n- /zops/zops.env\n- /zops/agents/HA_INTEGRATION.md",
    },

    # ---- 项目骨架 ----
    {
        "title": "项目功能树",
        "category": "project_introduction",
        "keywords": ["功能树", "AI应用平台", "智能运维", "基础设施监控"],
        "content": (
            "```mermaid\n"
            "graph TD\n"
            "    A[智能运维与AI应用平台] --> B[AI应用平台Dify]\n"
            "    A --> C[智能运维中枢Z-OPS]\n"
            "    A --> D[基础设施监控体系]\n"
            "    A --> E[统一认证与集成]\n"
            "    C --> C1[智能体管理]\n"
            "    C --> C2[自动化工作流]\n"
            "    C --> C3[技能中心]\n"
            "    C --> C4[多通道交互]\n"
            "    C1 --> C1a[浏览器智能体]\n"
            "    C1 --> C1b[虚拟机智能体]\n"
            "    C1 --> C1c[网络智能体]\n"
            "    E --> E1[OneAPI密钥管理]\n"
            "    E --> E2[Token自动命名]\n"
            "```"
        ),
    },
]


if __name__ == "__main__":
    import json
    result = import_qoder_memories(MEMORIES)
    print(json.dumps(result, indent=2))
    print(f"Imported {len(MEMORIES)} memories")
