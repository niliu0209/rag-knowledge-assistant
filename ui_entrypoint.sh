#!/bin/sh
# ui 容器入口：等待 api 就绪（有限重试 + 明确日志）后启动 Streamlit。
# 就绪探测只读 /api/health；重试上限 30 次（约 30s），避免无限等待。
set -eu

API_URL="${API_URL:-http://api:8000}"
COUNT=0
while [ "$COUNT" -lt 30 ]; do
    if python3 -c "import urllib.request,sys; urllib.request.urlopen('$API_URL/api/health', timeout=2); sys.exit(0)" 2>/dev/null; then
        echo "[ui] api ready at $API_URL"
        break
    fi
    COUNT=$((COUNT + 1))
    echo "[ui] waiting for api ($COUNT/30)..."
    sleep 1
done

if [ "$COUNT" -ge 30 ]; then
    echo "[ui] ERROR: api not ready after 30s at $API_URL" >&2
fi

exec streamlit run ui/main.py --server.port "${UI_PORT:-8501}" --server.address 0.0.0.0
