import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path 以便导入 src
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))
from trawler.account_vault import profile_dir, save_storage_state  # noqa: E402


async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run scripts/login.py <domain> [url]")
        print("Example: uv run scripts/login.py zhihu.com https://www.zhihu.com")
        sys.exit(1)

    domain = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else f"https://{domain}"

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("Error: patchright or playwright not installed")
            sys.exit(1)

    p_dir = profile_dir(domain)
    print(f"\n[INFO] 正在启动独立浏览器环境 (Domain: {domain})")
    print(f"[INFO] Profile 目录: {p_dir}")

    async with async_playwright() as p:
        # 弹出一个有界面的浏览器
        context = await p.chromium.launch_persistent_context(
            user_data_dir=p_dir,
            headless=False,
            channel="chrome",
        )
        page = await context.new_page()
        print(f"\n[INFO] 正在打开 {url} ...")
        await page.goto(url)

        print("\n" + "="*50)
        print("请在弹出的浏览器中手动登录您的账号。")
        print("如果您遇到图形验证码，请手动完成。")
        print("登录成功后，请在此终端按回车键 (Enter) 提取 Cookie...")
        print("="*50)

        # 阻塞等待用户按回车
        await asyncio.to_thread(input, "")

        print("\n[INFO] 正在提取 storage_state...")
        state = await context.storage_state()
        
        save_storage_state(domain, state)
        print("[SUCCESS] 账号 Cookie 已安全保存！")
        print(f"[SUCCESS] Agent 下次抓取 {domain} 将自动使用该身份。")

        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
