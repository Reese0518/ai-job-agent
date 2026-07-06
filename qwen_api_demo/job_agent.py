import json
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


sys.stdout.reconfigure(encoding="utf-8")

START_URL = "https://xiaoyuan.zhaopin.com/"
STATE_FILE = Path("zhaopin_state.json")
OUTPUT_FILE = Path("job_links.json")
MISSED_FILE = Path("missed_jobs.json")

MAX_SCROLL_ROUNDS = 20
MAX_JOBS = 80
SCROLL_STEP = 360

# Set to True only when you really want to remove internship listings.
EXCLUDE_INTERNSHIP = False
EXCLUDE_WORDS = ["实习", "实习生", "暑期实习", "实训", "兼职"]


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_title(card_text):
    lines = [line.strip() for line in card_text.splitlines() if line.strip()]
    return lines[0] if lines else "未知岗位"


def should_keep_card_text(text):
    if EXCLUDE_INTERNSHIP and any(word in text for word in EXCLUDE_WORDS):
        return False
    return True


def get_visible_job_cards(page):
    cards = page.evaluate(
        """
        () => {
            const els = Array.from(document.querySelectorAll("div"));
            const badWords = [
                "工作地点", "职位类型", "其他筛选", "已选条件",
                "更多城市", "更多专业", "首页", "职位推荐", "职位搜索",
                "求职服务", "求职工具"
            ];

            return els.map((el, index) => {
                const text = (el.innerText || "").trim();
                const rect = el.getBoundingClientRect();
                const lines = text.split("\\n").map(s => s.trim()).filter(Boolean);
                const title = lines[0] || "";

                let titleRect = null;
                const children = Array.from(el.querySelectorAll("div, span, p, a"));
                for (const child of children) {
                    const childText = (child.innerText || child.textContent || "").trim();
                    if (!childText || childText !== title) continue;
                    const childRect = child.getBoundingClientRect();
                    if (childRect.width < 20 || childRect.height < 10) continue;
                    titleRect = {
                        x: childRect.x,
                        y: childRect.y,
                        width: childRect.width,
                        height: childRect.height
                    };
                    break;
                }

                return {
                    index,
                    text,
                    title,
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height,
                    titleRect
                };
            }).filter(item => {
                if (!item.text || !item.title) return false;
                if (item.text.length < 20 || item.text.length > 460) return false;
                if (badWords.some(w => item.text.includes(w))) return false;
                if (!item.text.includes("投递")) return false;
                if (!item.titleRect) return false;
                if (item.width < 300 || item.height < 60) return false;
                if (item.y < 70 || item.y > window.innerHeight - 30) return false;
                return true;
            });
        }
        """
    )

    filtered = []
    seen_keys = set()
    for card in cards:
        if not should_keep_card_text(card["text"]):
            continue
        title = parse_title(card["text"])
        key = f"{title}|{card['text'][:120]}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        filtered.append(card)
    return filtered


def click_title_by_dom(page, context, title):
    try:
        with context.expect_page(timeout=5000) as new_page_info:
            page.evaluate(
                """
                (title) => {
                    const all = Array.from(document.querySelectorAll("div, span, p, a"));
                    const titleEl = all.find(el => {
                        const text = (el.innerText || el.textContent || "").trim();
                        const rect = el.getBoundingClientRect();
                        return text === title && rect.width > 20 && rect.height > 10;
                    });

                    if (!titleEl) throw new Error(`title element not found: ${title}`);
                    titleEl.scrollIntoView({block: "center", inline: "nearest"});
                    titleEl.click();
                }
                """,
                title,
            )

        detail_page = new_page_info.value
        detail_page.wait_for_load_state("domcontentloaded", timeout=60000)
        detail_page.wait_for_timeout(1200)
        detail_url = detail_page.url
        detail_page.close()
        return detail_url if "/job/" in detail_url else ""
    except Exception:
        return ""


def click_card_by_coordinates(page, context, card):
    title_rect = card.get("titleRect") or {}
    click_points = []

    if title_rect:
        click_points.append(
            (
                title_rect["x"] + min(80, max(10, title_rect["width"] / 2)),
                title_rect["y"] + title_rect["height"] / 2,
            )
        )

    click_points.extend(
        [
            (card["x"] + 80, card["y"] + 25),
            (card["x"] + 160, card["y"] + 25),
            (card["x"] + card["width"] - 120, card["y"] + 25),
            (card["x"] + 120, card["y"] + card["height"] / 2),
            (card["x"] + card["width"] / 2, card["y"] + card["height"] / 2),
        ]
    )

    for click_x, click_y in click_points:
        try:
            with context.expect_page(timeout=5000) as new_page_info:
                page.mouse.click(click_x, click_y)

            detail_page = new_page_info.value
            detail_page.wait_for_load_state("domcontentloaded", timeout=60000)
            detail_page.wait_for_timeout(1200)
            detail_url = detail_page.url
            detail_page.close()

            if "/job/" in detail_url:
                return detail_url
        except PlaywrightTimeoutError:
            if "/job/" in page.url:
                detail_url = page.url
                page.go_back(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1200)
                return detail_url
        except Exception:
            pass
    return ""


def open_card_and_get_url(page, context, card):
    title = parse_title(card["text"])
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    detail_url = click_title_by_dom(page, context, title)
    if detail_url:
        return detail_url

    return click_card_by_coordinates(page, context, card)


def page_metrics(page):
    return page.evaluate(
        """
        () => ({
            scrollY: window.scrollY,
            innerHeight: window.innerHeight,
            scrollHeight: document.documentElement.scrollHeight
        })
        """
    )


def main():
    job_links = []
    missed_jobs = []
    seen_urls = set()
    attempted_keys = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        if STATE_FILE.exists():
            context = browser.new_context(storage_state=str(STATE_FILE))
            print(f"已加载登录状态: {STATE_FILE}")
        else:
            context = browser.new_context()
            print(f"没有找到登录状态文件: {STATE_FILE}")

        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)

        print("浏览器已打开。")
        print("请手动搜索目标岗位，并确认结果列表已经加载。")
        print(f"是否排除实习岗位: {EXCLUDE_INTERNSHIP}")
        input("搜索结果页加载完成后，回到这里按回车开始抓取链接...")

        print("当前页面:", page.url)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)

        scroll_y = 0
        no_new_rounds = 0

        for scroll_round in range(1, MAX_SCROLL_ROUNDS + 1):
            if len(job_links) >= MAX_JOBS:
                break

            before_count = len(job_links)
            print(f"扫描第 {scroll_round} 屏，当前位置 {scroll_y}...")
            page.wait_for_timeout(1200)
            cards = get_visible_job_cards(page)
            print(f"当前屏岗位卡片: {len(cards)}")

            for card in cards:
                if len(job_links) >= MAX_JOBS:
                    break

                title = parse_title(card["text"])
                card_key = f"{title}|{card['text'][:120]}"
                if card_key in attempted_keys:
                    continue
                attempted_keys.add(card_key)

                print(f"获取岗位链接: {title}")
                url = open_card_and_get_url(page, context, card)

                if not url:
                    print("未获取到详情页链接，记录到 missed_jobs.json")
                    missed_jobs.append(
                        {
                            "title": title,
                            "card_text": card["text"],
                            "source_page": page.url,
                            "scroll_y": scroll_y,
                        }
                    )
                    save_json(MISSED_FILE, missed_jobs)
                    continue

                if url in seen_urls:
                    print("重复链接，跳过")
                    continue

                seen_urls.add(url)
                job_links.append(
                    {
                        "title": title,
                        "url": url,
                        "source_page": page.url,
                    }
                )
                save_json(OUTPUT_FILE, job_links)
                print(f"已保存: {title}")
                page.wait_for_timeout(600)

            if len(job_links) == before_count:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

            metrics = page_metrics(page)
            at_bottom = metrics["scrollY"] + metrics["innerHeight"] >= metrics["scrollHeight"] - 20
            if at_bottom:
                print("已经到页面底部，停止滚动。")
                break
            if no_new_rounds >= 5:
                print("连续多轮没有新增岗位，停止滚动。")
                break

            scroll_y += SCROLL_STEP
            page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
            page.wait_for_timeout(1000)

        browser.close()

    save_json(OUTPUT_FILE, job_links)
    save_json(MISSED_FILE, missed_jobs)
    print(f"完成，已保存 {len(job_links)} 条岗位链接到 {OUTPUT_FILE}")
    print(f"漏抓 {len(missed_jobs)} 条，已保存到 {MISSED_FILE}")


if __name__ == "__main__":
    main()
