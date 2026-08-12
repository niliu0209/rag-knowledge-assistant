"""S2-2 浏览器实测：平台共享预设 Key 全链路（新用户无 Key 免费问答）。

真实浏览器（playwright chromium headless）驱动 docker compose 的 Streamlit UI。
断言基于 DOM 文本（真实语义元素）；截图存 Windows 截图目录。

运行前提：
- .env 已配置 RAG_SHARED_PRESET_KEY（平台共享免费预设 Key，复用现有 Key）；
- docker compose up -d --build 已启动（api + ui:8501）；
- 宿主机 chromium 依赖库已解包到 /tmp/debs（LD_LIBRARY_PATH 注入后启动）。

数据卷前提（真实卷，S2-1 后）：
- admin 存在且 active，自有加密 SiliconFlow Key（key_source=own）；
- friend2 被停用（S2-1 测试产物）；本脚本注册新用户 friend3。

流程：admin 生成邀请码 → 注册 friend3 → friend3 提供商页断言「平台免费预设
（无需 Key）」且无 Key 输入框 → 上传中文 docx → 真实问答（共享额度调用，断言
引用来源）→ admin 提供商页断言「已使用你自己的 Key」（BYOK 覆盖共享）。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fixture_docs import make_docx  # noqa: E402

BASE_URL = "http://localhost:8501"
SHOT_DIR = Path("/tmp/e2e-screenshots")
PREFIX = "s22"

ADMIN_USER, ADMIN_PASS = "admin", "AdminPass!@#2026"
FRIEND3_USER, FRIEND3_PASS = "friend3", "Friend3Pass!@#2026"

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
    """先试登录；等待「退出登录」出现判定成功，超时则切注册 tab。"""
    page.get_by_role("tab", name="登录").click()
    page.wait_for_timeout(500)
    page.get_by_role("textbox", name="用户名").fill(username)
    page.get_by_role("textbox", name="密码").fill(password)
    page.get_by_role("button", name="登录").click()
    try:
        page.get_by_role("button", name="退出登录").wait_for(timeout=4000)
        return
    except Exception:
        pass
    page.get_by_role("tab", name="注册").click()
    page.wait_for_timeout(500)
    page.get_by_role("textbox", name="用户名").fill(username)
    page.get_by_role("textbox", name="密码（至少 8 位）").fill(password)
    if invite:
        page.get_by_role("textbox", name="邀请码（可选）").fill(invite)
    page.get_by_role("button", name="注册").click()
    page.wait_for_timeout(1500)


def click_radio(page: Page, name: str) -> None:
    """Streamlit sidebar radio：点 label 文本触发。"""
    page.get_by_text(name, exact=True).first.click()
    page.wait_for_timeout(1000)


def main() -> int:
    # 唯一文件名：同名文档会被服务端拒绝（409 duplicate_document，防覆盖设计），
    # 多次跑本脚本时避免与残留文档冲突
    docx_path = Path(f"/tmp/s22-friend3-{uuid.uuid4().hex[:8]}.docx")
    make_docx(docx_path, None)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        # 1. admin 登录 → 管理页生成邀请码
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(1200)
        login_or_register(page, ADMIN_USER, ADMIN_PASS)
        page.wait_for_timeout(1500)
        check("admin 登录进入主界面", "本地知识库问答助手" in page.inner_text("body"))
        click_radio(page, "管理")
        page.wait_for_timeout(800)
        page.get_by_role("button", name="生成邀请码").click()
        page.wait_for_timeout(800)
        code_text = page.locator("pre").first.inner_text().strip()
        check("生成邀请码成功", len(code_text) == 8 and code_text.isalnum(), code_text)

        # 2. 注册新用户 friend3（无任何 provider_settings 记录）
        page.get_by_text("退出登录", exact=True).first.click()
        page.wait_for_timeout(800)
        login_or_register(page, FRIEND3_USER, FRIEND3_PASS, invite=code_text)
        body = page.inner_text("body")
        check("friend3 邀请码注册成功", "本地知识库问答助手" in body)

        # 3. friend3 提供商页：preset 无 Key 展示共享额度状态，无 Key 输入框
        click_radio(page, "提供商配置")
        page.wait_for_timeout(800)
        body = page.inner_text("body")
        check(
            "预设模式显示共享额度状态",
            "平台免费预设（无需 Key）：当前使用平台共享额度" in body,
        )
        key_inputs = page.get_by_role("textbox", name="API Key")
        check("预设模式无 Key 输入框", key_inputs.count() == 0)
        check("共享 Key 掩码不回显", "sk-****" not in page.inner_text("body"))
        shot(page, "01-preset-shared-status")

        # 3.5 模式切换即时生效（用户实测暴露：form 内 radio 提交前不更新脚本值，
        # 切 BYOK 无 API Key 输入框；radio 移出 form 后修复）
        page.get_by_text("自带 Key（BYOK）", exact=True).first.click()
        page.wait_for_timeout(1000)
        check(
            "切 BYOK 显示 API Key 输入框",
            page.get_by_role("textbox", name="API Key").count() == 1,
        )
        check("BYOK 分支显示提供商下拉", "提供商" in page.inner_text("body"))
        page.get_by_text("免费预设", exact=True).first.click()
        page.wait_for_timeout(1000)
        check(
            "切回预设 API Key 输入框消失",
            page.get_by_role("textbox", name="API Key").count() == 0,
        )
        shot(page, "01b-mode-switch-instant")

        # 4. friend3 上传中文 docx（真实入库，随后问答才有检索命中）
        click_radio(page, "文档上传")
        page.wait_for_timeout(600)
        page.set_input_files('input[type="file"]', str(docx_path))
        page.wait_for_timeout(600)
        page.get_by_role("button", name="上传并入库").click()
        page.get_by_text("入库成功", exact=False).wait_for(timeout=120000)
        body = page.inner_text("body")
        check("friend3 上传入库成功", "入库成功" in body)
        shot(page, "02-upload-ok")

        # 5. friend3 真实问答（平台共享额度真实调用；Qwen3-14B 生成需等待）
        click_radio(page, "问答")
        page.wait_for_timeout(800)
        page.get_by_placeholder("输入问题…").fill("行政部本周完成了哪些工作？")
        page.keyboard.press("Enter")
        page.get_by_text("引用来源", exact=False).wait_for(timeout=150000)
        body = page.inner_text("body")
        check("共享额度真实问答返回", "引用来源" in body)
        check("回答展示提供商来源", "提供商：" in body)
        shot(page, "03-shared-qa-answer")

        # 6. admin 提供商页：BYOK 自有 Key 优先于共享额度
        page.get_by_text("退出登录", exact=True).first.click()
        page.wait_for_timeout(600)
        login_or_register(page, ADMIN_USER, ADMIN_PASS)
        page.wait_for_timeout(1200)
        click_radio(page, "提供商配置")
        page.wait_for_timeout(800)
        body = page.inner_text("body")
        check("admin 显示自有 Key 状态", "已使用你自己的 Key" in body)
        shot(page, "04-admin-own-key")

        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n=== S2-2 浏览器实测：{len(results) - len(failed)}/{len(results)} PASS ===")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
