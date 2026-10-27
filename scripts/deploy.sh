#!/bin/bash
# Z-OPS 部署守门脚本 (2026-10-18: 修"源码改了没 build / build 了没重启 / 半成品悄悄上线"事故类)
# 用法: ./scripts/deploy.sh backend|frontend|all
set -e
cd "$(dirname "$0")/.."
TYPE="${1:-all}"

say() { echo -e "\033[1;36m[deploy]\033[0m $*"; }
fail() { echo -e "\033[1;31m[deploy] ✗ $*\033[0m"; exit 1; }

# ── 守门 1: 未提交改动可见化 (并行改动/半成品混入是 2026-10-18 三起事故根源) ──
DIRTY=$(git status --porcelain 2>/dev/null | wc -l)
if [ "$DIRTY" -gt 0 ] 2>/dev/null; then
  say "⚠ 工作区有 $DIRTY 个未提交文件 (将随本次部署一起上线):"
  git status --porcelain | head -15
  [ "$DIRTY" -gt 15 ] && echo "  ...等共 $DIRTY 个"
  read -p "  继续部署? [y/N] " -r -t 30 || true
  [[ "$REPLY" =~ ^[Yy]$ ]] || fail "已取消 (建议先 git add/commit 固化再部署)"
fi

# ── 守门 2: 前端源码比构建产物新则强制重建 ──
need_build() {
  [ ! -f web-next/.next/BUILD_ID ] && return 0
  NEWEST_SRC=$(find web-next/src web-next/public -type f -newer web-next/.next/BUILD_ID 2>/dev/null | head -1)
  [ -n "$NEWEST_SRC" ] && return 0
  return 1
}

deploy_backend() {
  say "后端: 语法检查..."
  python3 -m py_compile webchat_api.py config.py $(find api core -name '*.py' -not -path '*__pycache__*') \
    || fail "Python 语法错误, 中止"
  say "后端: 重启 zops-backend..."
  systemctl restart zops-backend.service
  for i in $(seq 1 15); do
    sleep 1
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7789/docs)" = "200" ] && { say "后端 ✅ (/docs 200)"; return 0; }
  done
  fail "后端 15s 内未就绪, 查 journalctl -u zops-backend -n 50"
}

deploy_frontend() {
  if need_build; then
    say "前端: 源码比构建新 (或无产物), 执行 npm run build..."
    (cd web-next && npm run build) || fail "构建失败, 中止 (线上仍为旧版本)"
  else
    say "前端: 构建产物已是最新, 跳过 build"
  fi
  say "前端: 重启 zops-frontend..."
  systemctl restart zops-frontend.service
  for i in $(seq 1 15); do
    sleep 1
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/)" = "200" ] && { say "前端 ✅ (/ 200)"; return 0; }
  done
  fail "前端 15s 内未就绪, 查 journalctl -u zops-frontend -n 50"
}

case "$TYPE" in
  backend)  deploy_backend ;;
  frontend) deploy_frontend ;;
  all)      deploy_backend; deploy_frontend ;;
  *)        echo "用法: $0 backend|frontend|all"; exit 1 ;;
esac
say "完成 $(date '+%H:%M:%S')"
