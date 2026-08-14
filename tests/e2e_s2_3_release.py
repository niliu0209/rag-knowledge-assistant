"""S2-3 浏览器实测：Caddy HTTPS 反代 + 隐私说明页 + 页脚备案号。

真实浏览器（playwright chromium headless）驱动 docker compose 的 Streamlit UI。
S2-3 起对外入口为 Caddy（https://localhost，internal CA 本地证书——
ignore_https_errors 仅测试用；线上 Let's Encrypt 无需该开关）。

运行前提：
- .env 已配置 RAG_COOKIE_SECURE=true、RAG_ICP_NUMBER=测试备案号（模拟上线）；
- docker compose up -d --build 已启动（caddy 80/443 + api + ui 栈内）；
- 宿主机 chromium 依赖库已解包到 /tmp/debs（LD_LIBRARY_PATH 注入后启动）。

数据卷前提（真实卷）：admin 存在且 active（沿用 S2-1/S2-2 状态）。

流程：https 可达 → 未登录隐私说明（关键条款可见 + 返回登录）→ admin 登录
（Secure cookie）→ 导航含「隐私说明」且可达 → 页脚备案号展示（ICP 测试值 +
工信部链接）→ 截图存档。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_URL = "https://localhost"
SHOT_DIR = Path(os.environ.get("E2E_SHOT_DIR", "/tmp/e2e-screenshots"))
PREFIX = "s23"

ADMIN_USER, ADMIN_PASS = "admin", "AdminPass!@#2026"
TEST_ICP = "浙ICP备2026999999号-1"  # 测试值；上线时以真实备案号配置

results: list[tuple[str, bool, str]] = []
shot_index = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))


def shot(page: Page, name: str) -> None:
    global shot_index
    shot_index += 1
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{PREFIX}-{shot_index}-{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  📸 {path.name}")


def login(page: Page, username: str, password: str) -> None:
    page.get_by_role("tab", name="登录").click()
    page.wait_for_timeout(500)
    page.get_by_role("textbox", name="用户名").fill(username)
    page.get_by_role("textbox", name="密码", exact=True).fill(password)
    page.get_by_role("button", name="登录", exact=True).click()
    page.get_by_role("button", name="退出登录").wait_for(timeout=30000)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        # 1. HTTPS 可达（Caddy internal CA；标题出现即页面加载成功）
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        page.get_by_role("tab", name="登录").wait_for(timeout=30000)
        check("HTTPS 可达（认证门出现）", "登录" in page.get_by_role("tab", name="登录").inner_text())
        shot(page, "auth-gate-https")

        # 2. 未登录访问隐私说明（公开信息）→ 关键条款可见 → 返回登录
        page.get_by_role("button", name="隐私说明").click()
        page.get_by_role("heading", name="隐私说明").wait_for(timeout=15000)
        body_text = page.locator(".stApp").inner_text()
        check(
            "未登录隐私页可达且覆盖关键条款",
            "发送给" in body_text and "SiliconFlow" in body_text and "加密" in body_text
            and "邀请" in body_text and "删除" in body_text,
        )
        shot(page, "privacy-guest")
        page.get_by_role("button", name="返回登录").click()
        page.get_by_role("tab", name="登录").wait_for(timeout=15000)
        check("返回登录可用", True)

        # 3. admin 登录（Secure cookie 模拟上线）
        login(page, ADMIN_USER, ADMIN_PASS)
        page.get_by_text("退出登录").wait_for(timeout=30000)
        check("admin 登录成功（HTTPS + Secure cookie）", True)
        shot(page, "home-logged-in")

        # 4. 登录后导航含「隐私说明」且页面可达
        # （radio input 视觉隐藏，label 才是可点击目标——headless 下
        # 点 input 会被 stSidebarContent 层拦截，label 是语义可点击元素）
        page.locator("label", has_text="隐私说明").click()
        page.get_by_role("heading", name="隐私说明").wait_for(timeout=15000)
        body_text = page.locator(".stApp").inner_text()
        check(
            "登录后导航隐私页可达",
            "数据流向" in body_text and "API Key 存储" in body_text,
        )
        shot(page, "privacy-logged-in")

        # 5. 页脚备案号（ICP 测试值 + 工信部链接）
        page.locator("label", has_text="首页").click()
        # 轮询等 rerun 完成（最长 15s；不等固定时长，避免 rerun 中抓到旧内容）
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            footer_text = page.locator(".stApp").inner_text()
            if "ICP备案号" in footer_text:
                break
            page.wait_for_timeout(500)
        icp_link = page.locator('a[href="https://beian.miit.gov.cn/"]')
        check(
            "页脚备案号展示（ICP 测试值 + 工信部链接）",
            TEST_ICP in footer_text
            and "ICP备案号" in footer_text
            and icp_link.count() == 1,  # inner_text 不含 href，链接单独断言
        )
        shot(page, "footer-icp")

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== S2-3 浏览器实测 {passed}/{len(results)} PASS =====")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
