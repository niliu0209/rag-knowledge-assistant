"""S2-1 浏览器实测：认证门 + 邀请码注册 + 双账号隔离 + 停用即时失效。

真实浏览器（playwright chromium headless）驱动 docker compose 的 Streamlit UI。
断言全部基于 DOM 文本（真实语义元素）；截图存 Windows 截图目录。

运行前提：docker compose up -d 已启动（api + ui:8501）；
宿主机 chromium 依赖库已解包到 /tmp/debs（LD_LIBRARY_PATH 注入后启动）。

数据卷前提（真实卷，2026-08-12 容器 API 实测后）：
- admin 已存在（密码 AdminPass!@#2026），名下 2 份 ready 文档（已从 default 迁移）；
- friend 已存在且 active；邀请码 W6RXRU63 已被使用；
- 本脚本不再假设首启注册：admin 直接登录 → 生成新邀请码 → 注册新用户 friend2 →
  隔离验证（friend2 空列表 vs admin 2 份文档）→ admin 停用 friend2 → 登录被拒。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE_URL = "https://localhost"  # S2-3 起入口为 Caddy HTTPS（internal CA 本地证书）
SHOT_DIR = Path(os.environ.get("E2E_SHOT_DIR", "/tmp/e2e-screenshots"))
PREFIX = "s21"

ADMIN_USER, ADMIN_PASS = "admin", "AdminPass!@#2026"
FRIEND2_USER, FRIEND2_PASS = "friend2", "FriendPass!@#2026"

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


def login_or_register(page: Page, username: str, password: str, invite: str | None = None) -> None:
    """先试登录；等待「退出登录」出现判定成功（rerun 后旧按钮残留不可靠），
    超时则切注册 tab（首启管理员 / 邀请码注册）。"""
    page.get_by_role("tab", name="登录").click()
    page.wait_for_timeout(500)
    page.get_by_role("textbox", name="用户名").fill(username)
    page.get_by_role("textbox", name="密码").fill(password)
    page.get_by_role("button", name="登录").click()
    try:
        page.get_by_role("button", name="退出登录").wait_for(timeout=4000)
        return  # 登录成功（已进主界面）
    except Exception:
        pass
    # 登录失败 → 注册
    page.get_by_role("tab", name="注册").click()
    page.wait_for_timeout(500)
    page.get_by_role("textbox", name="用户名").fill(username)
    page.get_by_role("textbox", name="密码（至少 8 位）").fill(password)
    if invite:
        page.get_by_role("textbox", name="邀请码（可选）").fill(invite)
    page.get_by_role("button", name="注册").click()
    page.wait_for_timeout(1500)


def click_radio(page: Page, name: str) -> None:
    """Streamlit sidebar radio：点 label 文本触发（radio input 常被遮罩拦截）。"""
    page.get_by_text(name, exact=True).first.click()
    page.wait_for_timeout(1000)


def st_selectbox(page: Page, label: str, option: str) -> None:
    """Streamlit selectbox（baseweb Select：combobox + option，非原生 select）。

    新版 Streamlit 容器点击不再展开下拉——点击后检查 aria-expanded，
    未展开则用键盘 ArrowDown 兜底（实测有效）。
    """
    box = (
        page.locator('[data-testid="stSelectbox"]', has_text=label)
        .locator('[role="combobox"]')
        .first
    )
    box.click()
    page.wait_for_timeout(600)
    if box.get_attribute("aria-expanded") != "true":
        box.press("ArrowDown")
        page.wait_for_timeout(600)
    page.get_by_role("option", name=option).click()
    page.wait_for_timeout(600)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 1000})
        page = context.new_page()

        # 1. 未登录 → 认证门
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("未登录显示登录页", page.get_by_role("tab", name="登录").is_visible())
        check("登录页含注册 tab", page.get_by_role("tab", name="注册").is_visible())
        shot(page, "01-auth-gate")

        # 2. admin 登录（真实卷已有账号，非首启注册）
        login_or_register(page, ADMIN_USER, ADMIN_PASS)
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("admin 登录进入主界面", "本地知识库问答助手" in body)
        check("侧边栏显示管理员标识", "（管理员）" in body)
        shot(page, "02-admin-home")

        # 3. 管理页：若 friend2 处于停用状态（上次运行「停用即时失效」用例的
        # 遗留），先启用——保证脚本可重复运行（前置自修复）
        click_radio(page, "管理")
        page.wait_for_timeout(800)
        st_selectbox(page, "选择用户", FRIEND2_USER)
        enable_btn = page.get_by_role("button", name="启用")
        if enable_btn.is_enabled():
            enable_btn.click()
            page.wait_for_timeout(800)
            print("  ⟳ friend2 已重新启用（上次运行遗留的停用状态）")
        else:
            st_selectbox(page, "选择用户", FRIEND2_USER)  # 还原选择，避免影响后续 selectbox 状态
        page.get_by_role("button", name="生成邀请码").click()
        page.wait_for_timeout(800)
        code_text = page.locator("pre").first.inner_text().strip()
        check("生成邀请码成功", len(code_text) == 8 and code_text.isalnum(), code_text)
        check("邀请码列表显示", code_text in page.inner_text("body"))
        shot(page, "03-admin-invite-code")

        # 4. 登出 → friend2 凭邀请码注册
        page.get_by_text("退出登录", exact=True).first.click()
        page.wait_for_timeout(800)
        check("登出回到登录页", page.get_by_role("tab", name="登录").is_visible())
        login_or_register(page, FRIEND2_USER, FRIEND2_PASS, invite=code_text)
        body = page.inner_text("body")
        check("邀请码注册成功进入主界面", "本地知识库问答助手" in body)
        check("普通用户无管理员标识", "（管理员）" not in body)
        try:
            page.get_by_role("radio", name="管理").wait_for(timeout=1500)
            check("普通用户无管理菜单", False)
        except Exception:
            check("普通用户无管理菜单", True)
        shot(page, "04-friend2-home")

        # 5. friend2 文档列表为空（隔离：admin 的 2 份文档不可见）
        click_radio(page, "文档列表")
        page.wait_for_timeout(800)
        list_text = page.inner_text("body")
        check("friend2 列表为空（隔离合同）", "知识库还没有文档" in list_text)
        click_radio(page, "文档上传")
        page.wait_for_timeout(600)
        check("friend2 上传页可达", "选择文档" in page.inner_text("body"))
        shot(page, "05-friend2-isolated")

        # 6. 登出 → admin 重新登录：列表显示自己的 2 份文档（隔离反证）
        page.get_by_text("退出登录", exact=True).first.click()
        page.wait_for_timeout(600)
        login_or_register(page, ADMIN_USER, ADMIN_PASS)
        page.wait_for_timeout(1200)
        click_radio(page, "文档列表")
        page.wait_for_timeout(800)
        body = page.inner_text("body")
        check("admin 列表显示共 2 份文档", "共 2 份文档" in body)
        check("admin 列表含采购汇总报告", "低值办公物资采购汇总报告" in body)
        shot(page, "06-admin-two-docs")

        # 7. admin 停用 friend2 → 用户列表状态变「已停用」
        click_radio(page, "管理")
        page.wait_for_timeout(800)
        st_selectbox(page, "选择用户", FRIEND2_USER)
        page.get_by_role("button", name="停用", exact=True).click()
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        check("停用后列表显示已停用", "已停用" in body, "用户列表状态列更新")
        shot(page, "07-admin-disable-friend2")

        # 8. friend2 重新登录被拒（停用即失效；手动登录，不 fallback 注册）
        page.get_by_text("退出登录", exact=True).first.click()
        page.wait_for_timeout(600)
        page.get_by_role("tab", name="登录").click()
        page.wait_for_timeout(500)
        page.get_by_role("textbox", name="用户名").fill(FRIEND2_USER)
        page.get_by_role("textbox", name="密码").fill(FRIEND2_PASS)
        page.get_by_role("button", name="登录").click()
        page.wait_for_timeout(1500)
        err_text = page.inner_text("body")
        check(
            "被停用用户登录被拒",
            "账号已被停用" in err_text,
            [l for l in err_text.splitlines() if "停用" in l][:1][0] if "停用" in err_text else "",
        )
        shot(page, "08-disabled-login-rejected")

        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n=== S2-1 浏览器实测：{len(results) - len(failed)}/{len(results)} PASS ===")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
