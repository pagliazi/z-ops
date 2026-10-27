#!/bin/bash
# ═══ Z-OPS 运维智能体 — Nginx 配置生成 ═══

ZOPS_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cat > "$ZOPS_DIR/deploy/zops-nginx.conf" << 'NGINXEOF'
server {
    listen 80;
    server_name ops.your-domain.com;

    # Z-OPS Web Dashboard
    location / {
        proxy_pass http://127.0.0.1:7789;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;

        # SSE 支持
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
        proxy_cache off;
    }

    # n8n (可选)
    location /n8n/ {
        proxy_pass http://127.0.0.1:5678/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
NGINXEOF

echo "Nginx 配置已生成: $ZOPS_DIR/deploy/zops-nginx.conf"
echo "安装方法:"
echo "  sudo cp $ZOPS_DIR/deploy/zops-nginx.conf /etc/nginx/sites-available/zops"
echo "  sudo ln -sf /etc/nginx/sites-available/zops /etc/nginx/sites-enabled/"
echo "  sudo nginx -t && sudo systemctl reload nginx"
