// ===== Z-OPS 工具函数 =====

/**
 * 渲染 Markdown 文本
 * @param {string} text - 要渲染的 Markdown 文本
 * @returns {string} - 渲染后的 HTML
 */
function renderMarkdown(text) {
    if (!text) return '';
    try {
        // 提取 <think>...</think> 思考过程
        let thinkHtml = '';
        let mainText = text;
        const thinkRegex = /<think>([\s\S]*?)<\/think>/gi;
        const thinks = [];
        let match;
        while ((match = thinkRegex.exec(text)) !== null) {
            thinks.push(match[1].trim());
        }
        if (thinks.length > 0) {
            mainText = text.replace(thinkRegex, '').trim();
            const thinkContent = thinks.join('\n\n');
            thinkHtml = '<details class="think-block"><summary><i class="ri-lightbulb-line"></i> 思考过程</summary><div class="think-content">' + thinkContent.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>') + '</div></details>';
        }
        const bodyHtml = marked.parse(mainText);
        return thinkHtml + bodyHtml;
    } catch(e) {
        console.error('[Z-OPS] markdown parse error:', e);
        return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
    }
}

/**
 * 格式化时间戳
 * @param {string} timestamp - ISO 时间戳
 * @returns {string} - 格式化后的时间字符串
 */
function formatTimestamp(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

/**
 * 截断文本
 * @param {string} text - 要截断的文本
 * @param {number} maxLength - 最大长度
 * @returns {string} - 截断后的文本
 */
function truncateText(text, maxLength) {
    if (!text) return '';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}

/**
 * 复制到剪贴板
 * @param {string} text - 要复制的文本
 * @returns {Promise<boolean>} - 是否成功
 */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (err) {
        console.error('[Z-OPS] 复制失败:', err);
        return false;
    }
}

/**
 * 本地存储工具
 */
const storage = {
    /**
     * 获取存储项
     * @param {string} key - 键
     * @returns {any} - 值
     */
    get(key) {
        try {
            const item = localStorage.getItem(key);
            return item ? JSON.parse(item) : null;
        } catch (e) {
            console.error('[Z-OPS] 读取存储失败:', e);
            return null;
        }
    },
    
    /**
     * 设置存储项
     * @param {string} key - 键
     * @param {any} value - 值
     */
    set(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
            return true;
        } catch (e) {
            console.error('[Z-OPS] 写入存储失败:', e);
            return false;
        }
    },
    
    /**
     * 移除存储项
     * @param {string} key - 键
     */
    remove(key) {
        try {
            localStorage.removeItem(key);
            return true;
        } catch (e) {
            console.error('[Z-OPS] 删除存储失败:', e);
            return false;
        }
    }
};

// 导出工具函数
window.ZOPSUtils = {
    renderMarkdown,
    formatTimestamp,
    truncateText,
    copyToClipboard,
    storage
};
