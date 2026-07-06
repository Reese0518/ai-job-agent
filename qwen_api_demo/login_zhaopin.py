from pathlib import Path

from playwright.sync_api import sync_playwright


STATE_FILE = Path("zhaopin_state.json")
LOGIN_URL = "https://xiaoyuan.zhaopin.com/"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        print("浏览器已打开，请手动完成登录。")
        input("登录完成后，回到终端按回车保存登录状态...")

        context.storage_state(path=str(STATE_FILE))
        browser.close()

    print(f"登录状态已保存到 {STATE_FILE}")


if __name__ == "__main__":
    main()
