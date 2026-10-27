// ===== Z-OPS API 服务 =====

/**
 * API 服务类
 */
class APIService {
    constructor() {
        this.baseURL = '';
        this.token = null;
    }
    
    /**
     * 设置认证 Token
     * @param {string} token - JWT Token
     */
    setToken(token) {
        this.token = token;
    }
    
    /**
     * 获取 Token
     * @returns {string|null}
     */
    getToken() {
        return this.token || localStorage.getItem('zops_token');
    }
    
    /**
     * 通用请求方法
     * @param {string} endpoint - API 端点
     * @param {Object} options - 请求选项
     * @returns {Promise<any>}
     */
    async request(endpoint, options = {}) {
        const url = `${this.baseURL}${endpoint}`;
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers
        };
        
        // 添加认证头
        const token = this.getToken();
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }
        
        try {
            const response = await fetch(url, {
                ...options,
                headers
            });
            
            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: '请求失败' }));
                throw new Error(error.detail || `HTTP ${response.status}`);
            }
            
            return await response.json();
        } catch (error) {
            console.error('[Z-OPS] API 请求失败:', error);
            throw error;
        }
    }
    
    /**
     * GET 请求
     * @param {string} endpoint - API 端点
     * @param {Object} params - 查询参数
     * @returns {Promise<any>}
     */
    async get(endpoint, params = {}) {
        const queryString = new URLSearchParams(params).toString();
        const url = queryString ? `${endpoint}?${queryString}` : endpoint;
        return this.request(url, { method: 'GET' });
    }
    
    /**
     * POST 请求
     * @param {string} endpoint - API 端点
     * @param {Object} data - 请求数据
     * @returns {Promise<any>}
     */
    async post(endpoint, data = {}) {
        return this.request(endpoint, {
            method: 'POST',
            body: JSON.stringify(data)
        });
    }
    
    /**
     * 流式 POST 请求
     * @param {string} endpoint - API 端点
     * @param {Object} data - 请求数据
     * @param {Function} onChunk - 数据块回调
     * @returns {Promise<void>}
     */
    async postStream(endpoint, data = {}, onChunk) {
        const url = `${this.baseURL}${endpoint}`;
        const token = this.getToken();
        
        try {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...(token ? { 'Authorization': `Bearer ${token}` } : {})
                },
                body: JSON.stringify(data)
            });
            
            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: '请求失败' }));
                throw new Error(error.detail || `HTTP ${response.status}`);
            }
            
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                
                const chunk = decoder.decode(value);
                const lines = chunk.split('\n');
                
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.slice(6));
                            onChunk(data);
                        } catch (e) {
                            // 忽略解析错误
                        }
                    }
                }
            }
        } catch (error) {
            console.error('[Z-OPS] 流式请求失败:', error);
            throw error;
        }
    }
    
    // ===== 具体 API 方法 =====
    
    /**
     * 获取 LDAP 状态
     * @returns {Promise<Object>}
     */
    async getLdapStatus() {
        return this.get('/api/auth/ldap-status');
    }
    
    /**
     * LDAP 登录
     * @param {string} username - 用户名
     * @param {string} password - 密码
     * @returns {Promise<Object>}
     */
    async ldapLogin(username, password) {
        return this.post('/api/auth/ldap', { username, password });
    }
    
    /**
     * 本地登录
     * @param {string} username - 用户名
     * @param {string} password - 密码
     * @returns {Promise<Object>}
     */
    async localLogin(username, password) {
        return this.post('/api/auth/login', { 
            username, 
            password, 
            auth_type: 'local' 
        });
    }
    
    /**
     * 发送消息
     * @param {string} query - 查询内容
     * @param {Function} onChunk - 数据块回调
     * @returns {Promise<void>}
     */
    async sendMessage(query, onChunk) {
        return this.postStream('/api/chat', { query }, onChunk);
    }
    
    /**
     * 获取系统概览
     * @returns {Promise<Object>}
     */
    async getOverview() {
        return this.get('/api/overview');
    }
}

// 创建全局 API 实例
window.ZOPSAPI = new APIService();
