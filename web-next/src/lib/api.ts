const API_BASE = ''; // 使用相对路径，通过 Next.js rewrite 代理

// 获取 token - 确保只在客户端环境访问 localStorage
export function getToken(): string {
  if (typeof window === 'undefined') {
    return ''; // SSR 环境不访问 localStorage
  }
  return localStorage.getItem('zops_token') || '';
}

// 获取请求头
function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

// 统一处理 401 响应：清除 token 并跳转登录
function handleUnauthorized(response: Response): void {
  if (response.status === 401) {
    if (typeof window !== 'undefined') {
      localStorage.removeItem('zops_token');
      localStorage.removeItem('zops_user');
      window.location.reload();
    }
    throw new Error('登录已过期，请重新登录');
  }
}

export interface Session {
  id: string;
  title: string;
  updated_at: string;
  message_count?: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: string;
  agent?: string;
  agents?: string[];     // 参与链: 处理过该消息的所有 agent
  kb_source?: boolean;   // 知识来源标记: 回答引用了内部知识库
  isStreaming?: boolean;
  thinking?: string;      // 推理模型思考链 (DeepSeek thinking mode)，前端折叠展示
  // 增强元数据 (参考 claude-code cost-tracker)
  data_source?: string;   // 数据来源 (如 opscore_mgt)
  data_count?: number;    // 数据条数
  usage?: { input_tokens?: number; output_tokens?: number };
  model?: string;         // 使用的 LLM 模型
  progress?: string;      // 进度文本
  createdAt?: number;     // 创建时间戳（用于相对时间显示）
  file_url?: string;      // 可下载文件 URL
  file_name?: string;     // 文件名
}

export interface ChatRequest {
  message: string;
  session_id?: string;
  target?: string;
  target_agent?: string;
}

export interface ChatResponse {
  message: ChatMessage;
  session_id: string;
}

// 登录
export async function loginApi(username: string, password: string, authType: string) {
  const response = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, auth_type: authType }),
  });
  return response.json();
}

// 检查 LDAP 状态
export async function getLdapStatus() {
  const response = await fetch(`${API_BASE}/api/auth/ldap-status`);
  return response.json();
}

// 获取会话列表
export async function getSessions(): Promise<Session[]> {
  const response = await fetch(`${API_BASE}/api/chat/sessions`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const error = await response.text();
    throw new Error(`HTTP error! status: ${response.status} - ${error}`);
  }
  const data = await response.json();
  
  console.log('Sessions API response:', data);
  
  // 适配后端返回格式：{sessions: [{session_id, title, msg_count, updated_at}]}
  const sessions = data.sessions || (Array.isArray(data) ? data : []);
  return sessions.map((s: any) => ({
    id: s.session_id || s.id,
    title: s.title || '新对话',
    updated_at: s.updated_at ? formatTimestamp(s.updated_at) : '',
    message_count: s.msg_count || s.message_count || 0,
  }));
}

// 时间戳格式化 (后端返回秒级Unix时间戳, 统一输出 UTC+8)
function formatTimestamp(ts: number | string): string {
  if (typeof ts === 'number') {
    return toCST(new Date(ts * 1000));
  }
  // 如果已经是 ISO 字符串，也转为 UTC+8
  if (typeof ts === 'string' && ts.includes('T')) {
    return toCST(new Date(ts));
  }
  return ts;
}

// 转换为 UTC+8 (中国标准时间) 格式
function toCST(date: Date): string {
  return date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

// 获取 UTC+8 当前时间字符串
function nowCST(): string {
  return toCST(new Date());
}

// 获取 UTC+8 当前日期 (YYYY-MM-DD)
export function todayCST(): string {
  const d = new Date();
  // 加8小时偏移
  const utc8 = new Date(d.getTime() + 8 * 3600 * 1000);
  return utc8.toISOString().split('T')[0];
}

// 创建会话
export async function createSession(): Promise<Session> {
  const response = await fetch(`${API_BASE}/api/chat/sessions`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  const data = await response.json();
  // 适配后端返回格式 {session_id, title} → 前端 Session 接口 {id, title, ...}
  return {
    id: data.session_id || data.id,
    title: data.title || '新对话',
    updated_at: data.updated_at ? formatTimestamp(data.updated_at) : nowCST(),
    message_count: 0,
  };
}

// 删除会话
export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/chat/sessions/${sessionId}`, {
    method: 'DELETE',
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
}

// 重命名会话
export async function renameSession(sessionId: string, title: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/chat/sessions/${sessionId}`, {
    method: 'PUT',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify({ title }),
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
}

// 会话待回复判定 (权威): 末条用户消息对应任务的实时状态
export async function getSessionPending(sessionId: string): Promise<{ state: string; error?: string; task_id?: string }> {
  const response = await fetch(`${API_BASE}/api/chat/sessions/${sessionId}/pending`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

// 获取会话消息
export async function getSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  const response = await fetch(`${API_BASE}/api/chat/messages/${sessionId}`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const error = await response.text();
    throw new Error(`HTTP error! status: ${response.status} - ${error}`);
  }
  const data = await response.json();
  
  console.log('Messages API response:', data);
  
  // 后端返回：{session_id, title, messages: [{role, content, timestamp, source}]}
  const messages = data.messages || (Array.isArray(data) ? data : []);
  return messages.map((m: any, idx: number) => ({
    id: m.id || `${m.role}-${m.timestamp || Date.now()}-${idx}`,
    role: m.role || 'assistant',
    content: m.content || '',
    timestamp: m.timestamp ? formatTimestamp(m.timestamp) : nowCST(),
    agent: m.source || m.agent || 'system',
    file_url: m.file_url,
    file_name: m.file_name,
    // 过程数据透传 (10-19 后端落库): 历史消息重开时 工具/子代理 tab 数据源
    agents: m.agents,
    steps: m.steps,
  }));
}

// 发送聊天消息
export async function sendMessage(request: ChatRequest): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(request),
  });

  handleUnauthorized(response);
  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }

  return response.json();
}

// 发送流式聊天消息
export interface StreamChunk {
  type: string;
  text?: string;
  session_id?: string;
  source?: string;
  kind?: string;           // delta 类型: content=正文 / thinking=思考链
  agents?: string[];       // 参与链: 处理过该消息的所有 agent
  kb_source?: boolean;     // 知识来源标记: 回答引用了内部知识库
  data_source?: string;
  data_count?: number;
  usage?: { input_tokens?: number; output_tokens?: number };
  model?: string;
  file_url?: string;
  file_name?: string;
  task_id?: string;        // P1: 任务 id (断线补读)
  seq?: number;            // P1: 事件游标
  event_id?: string;
  idempotency_key?: string;
  replay?: boolean;
  skills?: string[];       // P2 可见性: 本轮注入的已发布技能
  memory_used?: boolean;   // P2 可见性: 是否参考了用户跨会话记忆
}

export async function* sendMessageStream(request: ChatRequest): AsyncGenerator<StreamChunk> {
  // P1 (task-c17): 幂等键 — 网络层重试/断线补读不重复提交任务
  const idemKey = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID()
    : `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: { ...getHeaders(), 'Idempotency-Key': idemKey },
    credentials: 'include',
    body: JSON.stringify(request),
  });

  handleUnauthorized(response);
  if (!response.ok) {
    const errorText = await response.text();
    // 优先取 FastAPI 的 {"detail": "..."} 让 503 快速失败等错误直达用户
    let detail = errorText;
    try {
      const j = JSON.parse(errorText);
      detail = j.detail || j.message || errorText;
    } catch { /* 非 JSON 原样返回 */ }
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('No response body');
  }

  const decoder = new TextDecoder();
  let buffer = '';
  
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      const text = decoder.decode(value, { stream: true });
      console.log('Received chunk:', text);
      
      buffer += text;
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data: ')) continue;
        const data = trimmed.slice(6);
        if (data === '[DONE]') {
          console.log('Stream completed');
          return;
        }
        try {
          const parsed = JSON.parse(data);
          console.log('Parsed chunk:', parsed);
          yield parsed;
        } catch (e) {
          console.warn('Failed to parse SSE data:', e, 'data:', data);
          // 忽略解析错误
        }
      }
    }
  } catch (error) {
    console.error('Stream error:', error);
    throw error;
  } finally {
    // 取消底层读取并关闭连接，避免组件卸载/提前退出时连接泄漏
    try { await reader.cancel(); } catch { /* 已关闭则忽略 */ }
    reader.releaseLock();
  }
}

// 获取会话历史
export async function getChatHistory(sessionId: string): Promise<ChatMessage[]> {
  const response = await fetch(`${API_BASE}/api/chat/history?session_id=${sessionId}`, {
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }

  return response.json();
}

// P1 (task-c17): 任务事件断线补读 — 流中断后按游标拉取直至终态, 不重新提交任务。
// 只回放关键事件 (reply/error/done); delta/progress 由实时流负责, 避免重复渲染。
export async function* catchUpTaskEvents(taskId: string): AsyncGenerator<StreamChunk> {
  let cursor = 0;
  for (let i = 0; i < 90; i++) {
    const resp = await fetch(
      `${API_BASE}/api/chat/tasks/${encodeURIComponent(taskId)}/events?after=${cursor}`,
      { headers: getHeaders(), credentials: 'include' },
    );
    if (!resp.ok) throw new Error(`catch-up HTTP ${resp.status}`);
    const d = await resp.json();
    for (const ev of d.events || []) {
      cursor = Math.max(cursor, ev.seq || 0);
      if (ev.type === 'reply') {
        yield { type: 'reply', text: ev.data?.text, source: ev.data?.source, agents: ev.data?.agents, event_id: ev.event_id };
      } else if (ev.type === 'task_terminal') {
        if (ev.data?.error) yield { type: 'error', text: ev.data.error, event_id: ev.event_id };
        yield { type: 'done' };
      }
    }
    if (d.terminal) return;
    await new Promise(r => setTimeout(r, 2000));
  }
  throw new Error('断线补读超时');
}

// 获取 Agent 列表
export async function getAgents(): Promise<{ name: string; description: string; status: string }[]> {
  const response = await fetch(`${API_BASE}/api/agents/detail`, {
    headers: getHeaders(),
    credentials: 'include',
  });

  handleUnauthorized(response);
  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }

  return response.json();
}

// 用户登录
export async function login(username: string, password: string): Promise<{ success: boolean; token?: string }> {
  const response = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ username, password }),
  });

  return response.json();
}

// 检查登录状态
export async function checkAuth(): Promise<{ authenticated: boolean; user?: { username: string; role: string } }> {
  const response = await fetch(`${API_BASE}/api/auth/check`, {
    credentials: 'include',
  });

  return response.json();
}

// ═══ 工具 & 用量 API (参考 claude-code cost-tracker) ═══

export interface ToolInfo {
  name: string;
  description: string;
  category: string;
  agent_id: string;
  permission: string;
  params: string[];
}

export interface UsageStats {
  total_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_tool_calls: number;
  total_elapsed_seconds: number;
  compact_stats: Record<string, any>;
  recent: any[];
}

// 获取已注册工具列表
export async function getTools(): Promise<{ count: number; tools: ToolInfo[] }> {
  const response = await fetch(`${API_BASE}/api/tools`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

// 获取 token 用量统计
export async function getUsage(sessionId?: string): Promise<UsageStats> {
  const qs = sessionId ? `?session_id=${sessionId}` : '';
  const response = await fetch(`${API_BASE}/api/usage/current${qs}`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

// ═══ v4.0 审批门 / 会话恢复 / 全链路追踪 API ═══

export interface ApprovalRecord {
  approval_id: string;
  tool_name: string;
  params: string;           // JSON 字符串
  agent?: string;
  session_id?: string;
  username?: string;
  risk_note?: string;
  status: 'pending' | 'approved' | 'rejected' | 'cancelled' | 'timeout';
  created_ts: string;       // Redis 原始字符串秒级时间戳
  decided_ts?: string;
  approver?: string;
  category?: string;        // 审批分类: daily=日常配置 / maintenance=维护
  citp?: string;            // CITP 快照 (JSON 字符串, 维护审批)
}

// 列出待流程决策的审批单 (仅管理员; destructive 已拦截, 审批仅流程留痕, 与飞书卡片双路径)
export async function getPendingApprovals(): Promise<{ pending: ApprovalRecord[]; count: number }> {
  const response = await fetch(`${API_BASE}/api/approvals`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

// 审批决策 (approve / reject; 决策永久写入审计日志)
// 审批只走流程: 决策仅为流程留痕, 系统不自动执行 destructive 操作
export async function decideApproval(approvalId: string, decision: 'approve' | 'reject'): Promise<{ status: string; decision: string; approver: string; staged?: boolean; level_done?: string; next_level?: string; note?: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/decide`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify({ decision }),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`HTTP ${response.status} - ${detail}`);
  }
  return response.json();
}

// 审批申请业务字段 (用户主动申请; summary/site/case_no 必填)
export interface ApprovalApplyFields {
  tool_name: string;
  summary: string;
  params: Record<string, string>;
  risk_note?: string;
  site: string;
  service_type?: string;
  case_no: string;
  priority?: string;
  planned_ts?: string;
  impact_scope?: string;
  rollback_plan?: string;
  change_window?: string;
  customer?: string;
  customer_gid?: string;         // OPS-CORE CRM 客户编号 (表单选客户自动带出)
  customer_site_ids?: string;    // OPS-CORE-MGT CRM 资源池 Site ID (逗号分隔, 选客户自动带出)
  customer_order_no?: string;    // OPS-CORE-MGT CRM 订单号 (选客户自动带出)
  original_approval_id?: string; // 回退申请关联的原审批单号
  approvers?: string;
  approver_ds?: string;      // 分级审批: DS 配置复核 (必要级, 不能跳过)
  approver_tech?: string;    // 分级审批: 技术审批
  approver_admin?: string;   // 分级审批: 行政审批
  target_host?: string;      // 目标主机/服务器 (逗号分隔, 与 NMS 告警联动)
  related_alerts?: RelatedAlert[];  // 勾选关联的 NMS 活动告警快照
  category?: string;         // 审批分类: daily=日常配置(默认) / maintenance=维护
  citp?: CitpSnapshot;       // CITP 快照 (维护审批按故障类型模板生成, 随单入库)
}

// CITP 快照 (维护审批; 按真实 CITP 附件格式: Plan 步骤表 + Check_list 验证表)
export interface CitpSnapshot {
  citp_no?: string;
  reference_number?: string;    // CITP-{HOST}{故障类型}#{CASE}
  fault_type?: string;          // 故障类型名称 (如 板载网卡更换)
  fault_type_key?: string;
  template_source?: string;
  platform?: string;            // ceph / esxi / generic (决定 checklist 变体)
  window?: string;              // 时间窗 (前端手选 + 时区)
  prepared_by?: string;
  in_charge_by?: string;
  approved_by?: string;
  device?: CitpDevice;          // 主设备 (资源管理系统匹配)
  devices?: CitpDevice[];       // 全部设备 (主 + 备选)
  customers?: { name: string; gid?: string; vms?: number }[];  // 受影响客户列表
  vms_count?: number;
  alerts_matched?: number;
  plan?: CitpPlanStep[] | Record<string, string>;  // 新: 步骤表; 旧: 四段文本
  checklist?: CitpCheckItem[];
  matched_count?: number;
  total_targets?: number;
  mgt_available?: boolean;
  note?: string;
  generated_ts?: number;
  [k: string]: unknown;   // 后端附加字段透传
}

// CITP Plan 步骤行 (Change Management 表)
export interface CitpPlanStep {
  step: number;
  time?: string;
  desc?: string;
  operator?: string;
  command?: string;
  verify?: string;
  fallback?: string;
}

// CITP Check_list 验证行
export interface CitpCheckItem {
  step: number | string;
  host?: string;
  ip?: string;
  sub_step?: string;
  item?: string;
  expected?: string;
}

// CITP 设备匹配项 (资源管理系统 OPS-CORE-MGT/CMDB 识别结果)
export interface CitpDevice {
  target: string;         // 前端手选填写的目标 (主机名/IP/SN)
  matched: boolean;       // 是否匹配到资源管理系统设备
  name?: string;
  hostname?: string;
  ip?: string;
  sn?: string;
  vendor?: string;
  model?: string;
  firmware?: string;
  site?: string;
  rack?: string;          // 机柜位置 (CMDB 补充)
  platform?: string;      // ceph / esxi / storage / generic
  maintenance_mode?: boolean;   // 主机是否已在维护模式 (vCenter 实时)
  power_state?: string;
  connection_state?: string;
  reason?: string;        // 未匹配原因
}

// NMS 联动告警 (审批表单勾选关联; 快照摘要, 提交随单入库)
export interface RelatedAlert {
  alarm_id: string;
  severity: string;
  node: string;
  summary: string;
  time?: string;
  case_id?: string;
  matched?: string[];  // 命中维度: case/host/site/customer
}

// 用户主动发起审批申请 (审批只走流程: 仅留痕, 批准后由运维人员线下执行)
// 申请表单元数据: 操作类型/服务类型/站点清单/审批人候选 (下拉数据源)
export interface ApprovalMeta {
  apply_types: { value: string; label: string }[];
  service_types: string[];
  sites: string[];
  site_items?: { value: string; label: string; kw?: string }[];  // OPS-CORE 结构化站点 (kw=搜索域: 城市码+机楼码+中文名)
  operation_groups?: { stype: string; ops: { value: string; label: string }[] }[];  // 操作类型按服务类型(平台)分组联动 (合并视图)
  operation_groups_by_category?: Record<string, { product: string; services: { service: string; ops: { value: string; label: string }[] }[] }[]>;  // 三级目录: 产品→服务→操作 (按分类)
  approvers: { username: string; display_name: string }[];
  approvers_ds?: { username: string; display_name: string }[];  // DS 配置复核候选 (持 approval.ds_review)
  categories?: { value: string; label: string }[];               // 审批分类 (日常配置/维护)
  category_hints?: Record<string, string>;                       // 操作类型 → 建议分类 (联动预选)
  citp_fault_types?: { value: string; label: string; source?: string }[];  // CITP 故障类型模板目录
}
export async function getApprovalMeta(): Promise<ApprovalMeta> {
  const response = await fetch(`${API_BASE}/api/approvals/meta`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { apply_types: [], service_types: [], sites: [], approvers: [] };
  return response.json();
}

export async function createApproval(fields: ApprovalApplyFields): Promise<{ status: string; approval_id: string; tool_name: string; note: string }> {
  const response = await fetch(`${API_BASE}/api/approvals`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(fields),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`HTTP ${response.status} - ${detail}`);
  }
  return response.json();
}

// 申请草稿 (服务端存储, 跨设备; 每用户一份, 7 天过期; 表单 JSON 快照)
export async function getApprovalDraft(): Promise<{ draft: Record<string, unknown> | null; saved_ts?: number }> {
  const response = await fetch(`${API_BASE}/api/approvals/draft`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { draft: null };
  return response.json();
}

export async function saveApprovalDraft(draft: Record<string, unknown>): Promise<{ status: string; saved_ts?: number }> {
  const response = await fetch(`${API_BASE}/api/approvals/draft`, {
    method: 'PUT',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(draft),
  });
  handleUnauthorized(response);
  if (!response.ok) throw Error(`HTTP ${response.status}`);
  return response.json();
}

export async function clearApprovalDraft(): Promise<{ status: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/draft`, {
    method: 'DELETE',
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw Error(`HTTP ${response.status}`);
  return response.json();
}

// 申请人编辑待审批单 (仅本人 + pending; 编辑后分级审批流重置从头审)
export async function updateApproval(approvalId: string, fields: ApprovalApplyFields):
  Promise<{ status: string; approval_id: string; edit_count?: number; note?: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/${approvalId}`, {
    method: 'PUT',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(fields),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail.slice(0, 200));
  }
  return response.json();
}

// 审批单完整流程轨迹 (近 7 天审计事件: 创建/编辑/逐级决策/终审/撤销/CITP, 全员可读)
export interface ApprovalHistoryEvent { ts?: number | string; action?: string; username?: string; summary?: string }
export async function getApprovalHistory(approvalId: string):
  Promise<{ approval_id: string; events: ApprovalHistoryEvent[]; count: number }> {
  const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/history`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { approval_id: approvalId, events: [], count: 0 };
  return response.json();
}

// 申请内容自动分析 → 表单字段自动填充 (规则提取 case/设备/站点 + AI 分类产品/操作/客户)
export async function autoFillApproval(body: { summary: string; category?: string }):
  Promise<{ fills: Record<string, string>; filled: string[]; source: Record<string, string>; note?: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/autofill`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(body),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    return { fills: {}, filled: [], source: {} };
  }
  return response.json();
}

// 客户字典 (OPS-CORE 管理平台 CRM 同步: GID + 客户名称; 首次拉取数秒, 缓存 10min)
export async function getApprovalCustomers(): Promise<{ customers: { gid: string; name: string; site_ids?: string[]; order_no?: string }[]; count: number; source: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/customers`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { customers: [], count: 0, source: 'manual' };
  return response.json();
}

// case 号联动校验 (OPS-CORE 信息管理系统同步; 平台不可用时 available=false, 不阻断提交)
export interface CaseLookupResult {
  available: boolean;
  found: boolean;
  cases: { case_no: string; title: string; status: string; customer: string }[];
  note?: string;
}
export async function lookupCaseNo(caseNo: string): Promise<CaseLookupResult> {
  const response = await fetch(`${API_BASE}/api/approvals/case-lookup?case_no=${encodeURIComponent(caseNo)}`,
    { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { available: false, found: false, cases: [], note: `HTTP ${response.status}` };
  return response.json();
}

// 申请人撤销待审批单 (仅 pending 且本人; 撤销写审计日志永久留痕)
// LLM 辅助影响分析: 依据操作内容生成 影响范围/风险/回退建议 (回填表单可编辑)
// 审批表单联动: 按站点/客户/case/主机匹配 NMS 当前活动告警 (可勾选关联)
export async function getRelatedAlerts(params: { site?: string; customer?: string; case_no?: string; host?: string }):
  Promise<{ available: boolean; alerts: RelatedAlert[]; matched?: number; total_active?: number; note?: string }> {
  const qs = new URLSearchParams();
  if (params.site) qs.set('site', params.site);
  if (params.customer) qs.set('customer', params.customer);
  if (params.case_no) qs.set('case_no', params.case_no);
  if (params.host) qs.set('host', params.host);
  const response = await fetch(`${API_BASE}/api/approvals/related-alerts?${qs}`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { available: false, alerts: [], note: `请求失败 (${response.status})` };
  return response.json();
}

export async function analyzeApprovalImpact(body: {
  tool_name?: string; summary?: string; site?: string; customer?: string;
  customer_gid?: string; case_no?: string; service_type?: string;
  params?: Record<string, string>;
}): Promise<{ available: boolean; impact_scope?: string; risk_note?: string; rollback_plan?: string; error?: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/analyze`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(body),
  });
  handleUnauthorized(response);
  return response.json();
}

// 执行确认 (已批准单线下执行完成; approved → executed)
export async function confirmExecution(approvalId: string):
  Promise<{ status: string; approval_id: string; decision: string; note?: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/execute`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw Error(detail.slice(0, 160));
  }
  return response.json();
}

// 审批统计概览 (管理员全局/普通用户本组)
export interface ApprovalStats {
  pending?: number; today?: number; week?: number;
  maintenance?: number; daily?: number; total?: number;
  avg_decide_hours?: number; scope?: string;
  slow_pending?: { approval_id?: string; tool_name?: string; username?: string; site?: string; age_hours?: number }[];
}
export async function getApprovalStats(): Promise<ApprovalStats> {
  const response = await fetch(`${API_BASE}/api/approvals/stats`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return {};
  return response.json();
}

// CITP 设备候选搜索 (OPS-CORE-MGT 宿主机 + 存储阵列; 短名/IP/SN 包含匹配, 前 10 条)
export interface CitpDeviceCandidate { name: string; kind: 'host' | 'array'; vendor: string; model: string; ip: string }
export async function searchCitpDevices(q: string):
  Promise<{ candidates: CitpDeviceCandidate[]; count: number; note?: string }> {
  const qs = new URLSearchParams({ q });
  const response = await fetch(`${API_BASE}/api/approvals/citp/devices?${qs}`, { headers: getHeaders(), credentials: 'include' });
  handleUnauthorized(response);
  if (!response.ok) return { candidates: [], count: 0 };
  return response.json();
}

// CITP 设备手动选择 → 资源管理系统 (OPS-CORE-MGT) 匹配: SN/IP/型号/机柜 + 受影响客户列表
export async function matchApprovalDevice(target: string):
  Promise<{ device: CitpDevice; customers: { name: string; gid?: string; vms?: number }[]; vms_count: number }> {
  const response = await fetch(`${API_BASE}/api/approvals/citp/match`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify({ target }),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw Error(detail.slice(0, 120) || `HTTP ${response.status}`);
  }
  return response.json();
}

// CITP 自动生成 (维护审批): 前端手选故障类型/设备/时间窗 → 资源管理系统匹配设备
// → 按真实 CITP 附件格式模板填充 Plan/Check_list (占位符替换 SN/IP/机柜/客户等)
export async function generateApprovalCitp(body: {
  fault_type?: string; target?: string; window?: string; tz?: string; platform?: string;
  case_no?: string; site?: string; customer?: string; summary?: string;
  tool_name?: string; target_host?: string; approved_by?: string;
  params?: Record<string, string>;
}): Promise<CitpSnapshot & { available: boolean; error?: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/citp`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify(body),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    return { available: false, error: `HTTP ${response.status}` };
  }
  return response.json();
}

// 维护验收: 主机网卡测试 (test-nic) → 结果写入该单 CITP 验收记录
export interface AcceptanceNic { name?: string; vswitch?: string; link_up?: boolean; summary?: string; issues?: string[] }
export interface AcceptanceResult {
  type?: string; tested_at?: string; overall_status?: 'green' | 'warn' | 'red';
  vcenter?: string; cluster?: string; nics?: AcceptanceNic[];
  pass_n?: number; warn_n?: number; fail_n?: number; by?: string; ts?: number;
}
export async function runAcceptanceTest(approvalId: string):
  Promise<{ status: string; approval_id: string; acceptance: AcceptanceResult }> {
  const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/acceptance`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw Error(detail.slice(0, 160));
  }
  return response.json();
}

// CITP xlsx 导出地址 (按真实模板格式: Plan + Check_list + 验收结果 + Rack Layout + 现场照片)
export function citpExportUrl(approvalId: string): string {
  return `${API_BASE}/api/approvals/${approvalId}/citp.xlsx`;
}

export async function cancelApproval(approvalId: string, reason: string): Promise<{ status: string; approval_id: string; decision: string; note: string }> {
  const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/cancel`, {
    method: 'POST',
    headers: getHeaders(),
    credentials: 'include',
    body: JSON.stringify({ reason }),
  });
  handleUnauthorized(response);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail.slice(0, 160));
  }
  return response.json();
}

// 当前用户发起的审批申请列表 (含已决策; 单据 Redis TTL 24h, 完整历史在审计日志)
export async function getMyApprovals(): Promise<{ mine: ApprovalRecord[]; count: number }> {
  const response = await fetch(`${API_BASE}/api/approvals/mine`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

// 业务权限点定义 (与系统角色分离; 目录由后端 BIZ_PERMS 下发, 新增业务域前端自动渲染)
export interface BizPermDef {
  key: string;      // 权限点标识 (存入用户 biz_perms)
  label: string;    // 展示名
  desc: string;     // 功能说明
  group: string;    // 业务分组 (变更治理/自动化/知识运营/可观测...)
}

// 业务权限点目录 (登录用户可读; 授予操作走用户管理 PUT)
export async function getBizPermCatalog(): Promise<{ perms: BizPermDef[]; count: number }> {
  const response = await fetch(`${API_BASE}/api/auth/biz-perms`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

export interface ResumeContext {
  source: 'redis' | 'jsonl' | 'empty';
  messages: any[];
  stats?: Record<string, any>;
}

// 会话消息加载失败 (Redis 会话过期 404) 时降级: 从持久化 transcript (logs/sessions/{id}.jsonl) 重建上下文
export async function resumeSessionContext(sessionId: string): Promise<ResumeContext> {
  const response = await fetch(`${API_BASE}/api/sessions/${sessionId}/resume_context`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}

// 按 trace_id 精确查询全链执行记录 (路由 → Agent → 工具)
export async function getExecutionsByTrace(traceId: string): Promise<{ trace_id: string; count: number; records: any[] }> {
  const response = await fetch(`${API_BASE}/api/observability/executions?trace_id=${encodeURIComponent(traceId)}&limit=50`, {
    headers: getHeaders(),
    credentials: 'include',
  });
  handleUnauthorized(response);
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  return response.json();
}
