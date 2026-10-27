'use client';

import { useState, useEffect } from 'react';
import { Shield, User, Loader2, Lock } from 'lucide-react';

interface LoginPageProps {
  onLogin: (user: { username: string; display_name: string; role: string; token: string }) => void;
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  // 默认 AD 域登录；本地登录仅作 LDAP 停用时的兜底，前端不展示切换入口
  const [authType, setAuthType] = useState<'ldap' | 'local'>('ldap');

  useEffect(() => {
    fetch('/api/auth/ldap-status')
      .then(r => r.json())
      .then(d => { if (!d.ldap_enabled) setAuthType('local'); })
      .catch(() => {});
  }, []);

  const isLdap = authType === 'ldap';

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    const doLogin = (type: 'ldap' | 'local') =>
      fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, auth_type: type })
      });

    try {
      let r: Response;
      if (authType === 'ldap') {
        // 优先 AD 域认证
        r = await doLogin('ldap');
        // AD 账号密码错误(401) 时自动回退本地认证，兼容本地管理员账号（本地登录入口已隐藏）
        if (r.status === 401) {
          r = await doLogin('local');
        }
      } else {
        r = await doLogin('local');
      }

      const d = await r.json();

      if (r.ok && d.token) {
        localStorage.setItem('zops_token', d.token);
        localStorage.setItem('zops_user', JSON.stringify({
          username: d.username,
          display_name: d.display_name,
          role: d.role
        }));
        onLogin({ username: d.username, display_name: d.display_name, role: d.role, token: d.token });
      } else if (r.status === 403) {
        setError('您的账号尚未被授权访问智OC，请联系管理员审批');
      } else {
        setError(d.detail || '登录失败');
      }
    } catch (e) {
      setError('网络错误');
    }
    setLoading(false);
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6" style={{ background: 'var(--bg-secondary)' }}>
      <div className="w-full max-w-[380px]">
        {/* 品牌标识 */}
        <div className="flex items-center justify-center gap-2.5 mb-7">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-base"
               style={{ background: 'var(--cf-orange)' }}>
            智
          </div>
          <div className="text-left">
            <div className="font-bold leading-tight" style={{ color: 'var(--text-primary)' }}>智OC</div>
            <div className="text-[11px]" style={{ color: 'var(--text-tertiary)' }}>Operation Claw · 智能运维平台</div>
          </div>
        </div>

        {/* 登录卡片 */}
        <div className="bg-white rounded-xl border p-7"
             style={{ borderColor: 'var(--border-light)', boxShadow: 'var(--shadow-sm)' }}>
          <h1 className="text-lg font-semibold mb-1" style={{ color: 'var(--text-primary)' }}>登录</h1>
          <p className="text-[13px] mb-5" style={{ color: 'var(--text-tertiary)' }}>
            {isLdap ? '使用公司 AD 域账号登录' : '使用账号密码登录'}
          </p>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-[13px] font-medium mb-1.5" style={{ color: 'var(--text-secondary)' }}>
                {isLdap ? 'AD 域账号' : '用户名'}
              </label>
              <div className="relative">
                <User size={15} className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: 'var(--text-tertiary)' }} />
                <input
                  type="text"
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  autoFocus
                  autoComplete="username"
                  className="w-full bg-white border border-[#D9D9E3] rounded-lg pl-9 pr-3 py-2.5 text-sm text-[#0D0D0D] placeholder:text-[#8E8EA0] transition-all focus:outline-none focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/20"
                  placeholder={isLdap ? '输入 AD 域账号 (如 zhangsan)' : '输入用户名'}
                />
              </div>
            </div>

            <div>
              <label className="block text-[13px] font-medium mb-1.5" style={{ color: 'var(--text-secondary)' }}>密码</label>
              <div className="relative">
                <Lock size={15} className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: 'var(--text-tertiary)' }} />
                <input
                  type="password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  autoComplete="current-password"
                  className="w-full bg-white border border-[#D9D9E3] rounded-lg pl-9 pr-3 py-2.5 text-sm text-[#0D0D0D] placeholder:text-[#8E8EA0] transition-all focus:outline-none focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/20"
                  placeholder={isLdap ? '输入 AD 域密码' : '输入密码'}
                />
              </div>
            </div>

            {error && (
              <div className={`text-[13px] flex items-start gap-2 p-3 rounded-lg border ${
                error.includes('未授权')
                  ? 'bg-yellow-50 text-yellow-700 border-yellow-200'
                  : 'bg-red-50 text-red-700 border-red-200'
              }`}>
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !username || !password}
              className="w-full py-2.5 rounded-lg font-medium text-sm text-white transition-colors flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ background: 'var(--cf-orange)' }}
            >
              {loading ? (
                <>
                  <Loader2 className="animate-spin" size={15} />
                  登录中...
                </>
              ) : (
                <>
                  <Shield size={14} />
                  {isLdap ? 'AD 域登录' : '登录'}
                </>
              )}
            </button>
          </form>
        </div>

        {/* 底部说明 */}
        {isLdap && (
          <p className="text-xs text-center mt-4" style={{ color: 'var(--text-tertiary)' }}>
            使用公司 AD 域账号登录，需管理员预先授权
          </p>
        )}
      </div>
    </div>
  );
}
