"""
AI 渠道自动化搜索脚本
功能：打开指定渠道(元宝/豆包/千问) → 输入提示词 → 等待AI回复 → 追问生成HTML → 提取报告链接
用法：python ai_chat_automation.py --channel yuanbao --keyword "三角洲行动" --start "2026年07月16日" --end "2026年07月22日" --dim opinion
输出：JSON 格式结果（含 html_url / html_content / status）
"""

import asyncio
import json
import sys
import argparse
import time
import os
import re
from pathlib import Path

# ============================================================
# 渠道配置
# ============================================================
CHANNELS = {
    "yuanbao": {
        "name": "元宝",
        "url": "https://yuanbao.tencent.com/chat/naQivTmsDa",
        # 元宝聊天框选择器（根据实际DOM调整）
        "input_selectors": [
            "textarea[placeholder*='输入']",
            "textarea[placeholder*='消息']",
            "textarea[placeholder*='提问']",
            "[contenteditable='true']",
            "textarea",
            ".chat-input textarea",
            "#chatInput",
            "input[type='text']",
        ],
        # 发送按钮选择器
        "send_selectors": [
            "button[aria-label='发送']",
            "button:has-text('发送')",
            "button[class*='send']",
            "button[type='submit']",
            ".send-btn",
            "[class*='submit']",
        ],
        # AI 回复区域选择器
        "response_selectors": [
            ".message-item:last-child .message-content",
            ".chat-message:last-child .content",
            "[class*='message']:last-child [class*='content']",
            ".assistant-message:last-child",
            "[class*='assistant']:last-child [class*='text']",
            ".markdown-body:last-child",
            "[class*='reply']:last-child",
        ],
        # 文件卡片/链接选择器（AI返回HTML文件时）
        "file_link_selectors": [
            "a[href$='.html']",
            "[class*='file'] a",
            "[class*='attachment'] a",
            "a[class*='download']",
            "[class*='card'] a[href]",
        ],
        "wait_after_send": 8,      # 发送后等待AI回复的秒数
        "wait_after_followup": 15,  # 追问后等待HTML生成的秒数
    },
    "doubao": {
        "name": "豆包",
        "url": "https://www.doubao.com/chat/",
        "input_selectors": [
            "textarea[placeholder*='输入']",
            "textarea[placeholder*='发送']",
            "[contenteditable='true']",
            "textarea",
            "#search-box textarea",
            ".ProseMirror",
            "div[role='textbox']",
        ],
        "send_selectors": [
            "button[aria-label='发送']",
            "button:has-text('发送')",
            "button[class*='send']",
            "svg[class*='send']",
        ],
        "response_selectors": [
            ".message-bubble:last-child .bubble-content",
            "[class*='message']:last-child [class*='text']",
            ".assistant:last-child [class*='content']",
            ".markdown:last-child",
        ],
        "file_link_selectors": [
            "a[href$='.html']",
            "[class*='file'] a",
            "[class*='code-file'] a",
        ],
        "wait_after_send": 8,
        "wait_after_followup": 15,
    },
    "qianwen": {
        "name": "千问",
        "url": "https://www.qianwen.com/",
        "input_selectors": [
            "textarea[placeholder*='输入']",
            "textarea[placeholder*='向']",
            "[contenteditable='true']",
            "textarea",
            "#default-input-box textarea",
            "div[data-testid='input']",
        ],
        "send_selectors": [
            "button[aria-label='发送']",
            "button:has-text('发送')",
            "button[class*='send']",
        ],
        "response_selectors": [
            ".message-row:last-child .message-content",
            "[class*='assistant']:last-child [class*='text']",
            ".markdown-body:last-child",
        ],
        "file_link_selectors": [
            "a[href$='.html']",
            "[class*='file-card'] a",
        ],
        "wait_after_send": 8,
        "wait_after_followup": 15,
    },
}

# 监测类型映射
DIM_INFO = {
    "opinion": "游戏舆情和玩家核心讨论",
    "update": "游戏近期内容更新",
    "both": "舆情+内容更新",
}


def build_prompt(channel_name, start_date, end_date, keyword, dim_key):
    """构造发给AI的提示词"""
    dim_label = DIM_INFO.get(dim_key, DIM_INFO["opinion"])
    return (
        f"你好{channel_name}，请帮我搜索{start_date}到{end_date}期间，"
        f"{keyword}内容的{dim_label}，需要客观公正真实的内容，"
        f"内容选取抖音、快手、B站、各个游戏APP论坛，然后生成html报告并将链接转发给我。"
    )


FOLLOWUP_PROMPT = "请将报告生成可交互的网页链接"


async def find_element(page, selectors, timeout=5000):
    """尝试多个选择器找到元素"""
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout // len(selectors))
            if el:
                return el
        except Exception:
            continue
    return None


async def get_ai_response_text(page, config):
    """获取AI最新回复的文本内容"""
    for sel in config["response_selectors"]:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 10:
                    return text.strip()
        except Exception:
            continue
    # 兜底：获取页面所有文本，找最后一段较长的
    try:
        all_text = await page.inner_text("body")
        lines = [l.strip() for l in all_text.split("\n") if len(l.strip()) > 20]
        if lines:
            return "\n".join(lines[-5:])  # 最后5行
    except Exception:
        pass
    return ""


async def extract_html_links(page, config):
    """从页面中提取HTML文件链接"""
    links = []
    for sel in config["file_link_selectors"]:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                href = await el.get_attribute("href")
                text = await el.inner_text()
                if href and (href.endswith(".html") or "html" in href.lower()):
                    links.append({"url": href, "text": text.strip()})
        except Exception:
            continue
    # 兜底：扫描所有 <a> 标签
    if not links:
        try:
            all_links = await page.query_selector_all("a[href]")
            for al in all_links:
                href = await al.get_attribute("href")
                if href and ("html" in href.lower() or "report" in href.lower()):
                    text = await al.inner_text()
                    links.append({"url": href, "text": text.strip()})
        except Exception:
            pass
    return links


async def run_automation(channel, keyword, start_date, end_date, dim_key, headless=True, output_dir=None):
    """
    执行完整自动化流程
    返回: dict {status, channel, prompt, response_text, followup_response, html_links, html_content, error}
    """
    from playwright.async_api import async_playwright

    result = {
        "status": "unknown",
        "channel": channel,
        "prompt": "",
        "response_text": "",
        "followup_response": "",
        "html_links": [],
        "html_content": None,
        "html_url": None,
        "error": None,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    config = CHANNELS.get(channel)
    if not config:
        result["error"] = f"未知渠道: {channel}"
        result["status"] = "error"
        return result

    prompt = build_prompt(config["name"], start_date, end_date, keyword, dim_key)
    result["prompt"] = prompt

    # 输出目录
    out_dir = Path(output_dir) if output_dir else Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # ===== Step 1: 打开渠道页面 =====
            print(f"[1/6] 正在打开 {config['name']} ({config['url']})...")
            await page.goto(config["url"], wait_until="networkidle", timeout=30000)
            await asyncio.sleep(3)  # 等待JS渲染完成

            # 截图记录初始状态
            screenshot_path = out_dir / f"{channel}_step1_initial.png"
            await page.screenshot(path=str(screenshot_path))
            print(f"      初始状态截图: {screenshot_path.name}")

            # ===== Step 2: 找到输入框并输入提示词 =====
            print(f"[2/6] 正在查找输入框并输入提示词...")
            input_el = await find_element(page, config["input_selectors"], timeout=8000)
            if not input_el:
                # 尝试点击页面激活输入区域
                await page.click("body")
                await asyncio.sleep(1)
                input_el = await find_element(page, config["input_selectors"], timeout=5000)

            if input_el:
                tag_name = await input_el.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "textarea":
                    await input_el.fill(prompt)
                else:
                    await input_el.click()
                    await page.keyboard.type(prompt, delay=20)
                print(f"      ✓ 提示词已输入 ({len(prompt)} 字符)")
            else:
                raise Exception(f"未找到输入框 (已尝试 {len(config['input_selectors'])} 个选择器)")

            # 截图记录输入后状态
            screenshot_path = out_dir / f"{channel}_step2_input.png"
            await page.screenshot(path=str(screenshot_path))

            # ===== Step 3: 点击发送按钮 =====
            print(f"[3/6] 正在点击发送...")
            send_btn = await find_element(page, config["send_selectors"], timeout=5000)
            if send_btn:
                await send_btn.click()
                print(f"      ✓ 已点击发送按钮")
            else:
                # 尝试按回车键发送
                print(f"      未找到发送按钮，尝试按 Enter 发送...")
                await page.keyboard.press("Enter")

            # ===== Step 4: 等待 AI 首次回复 =====
            wait_time = config["wait_after_send"]
            print(f"[4/6] 等待 AI 回复 ({wait_time}s)...")
            await asyncio.sleep(wait_time)

            # 检查是否有加载指示器（转圈等），如果有则继续等
            for _ in range(5):
                loading = await page.query_selector("[class*='loading'], [class*='spinner'], [class*='thinking']")
                if not loading:
                    break
                print(f"      AI 仍在思考中，继续等待...")
                await asyncio.sleep(3)

            response_text = await get_ai_response_text(page, config)
            result["response_text"] = response_text[:2000] if response_text else ""
            print(f"      ✓ 收到 AI 回复 ({len(response_text)} 字符)")
            print(f"      回复预览: {response_text[:150]}...")

            # 截图记录首次回复
            screenshot_path = out_dir / f"{channel}_step3_first_reply.png"
            await page.screenshot(path=str(screenshot_path))

            # ===== Step 5: 追问生成 HTML =====
            print(f"[5/6] 正在追问「{FOLLOWUP_PROMPT}」...")
            input_el2 = await find_element(page, config["input_selectors"], timeout=5000)
            if input_el2:
                tag_name = await input_el2.evaluate("el => el.tagName.lower()")
                if tag_name == "textarea":
                    await input_el2.fill(FOLLOWUP_PROMPT)
                else:
                    await input_el2.click()
                    await page.keyboard.type(FOLLOWUP_PROMPT, delay=15)

                send_btn2 = await find_element(page, config["send_selectors"], timeout=3000)
                if send_btn2:
                    await send_btn2.click()
                else:
                    await page.keyboard.press("Enter")
                print(f"      ✓ 追问已发送")

            # ===== Step 6: 等待 HTML 报告生成 =====
            wait_time2 = config["wait_after_followup"]
            print(f"[6/6] 等待 HTML 报告生成 ({wait_time2}s)...")

            # 轮询检查是否出现 HTML 链接
            html_links = []
            for i in range(wait_time2 // 3 + 1):
                await asyncio.sleep(3)
                html_links = await extract_html_links(page, config)
                if html_links:
                    print(f"      ✓ 发现 {len(html_links)} 个 HTML 链接!")
                    break
                # 也检查是否有新消息出现
                if i % 3 == 0:
                    print(f"      ... 等待中 ({i*3}/{wait_time2}s)")

            result["html_links"] = html_links

            # 获取追问后的回复
            followup_text = await get_ai_response_text(page, config)
            result["followup_response"] = followup_text[:2000] if followup_text else ""

            # 截图记录最终状态
            screenshot_path = out_dir / f"{channel}_step6_final.png"
            await page.screenshot(path=str(screenshot_path))

            # 如果找到了 HTML 链接，尝试访问并下载内容
            if html_links:
                primary_link = html_links[0]
                result["html_url"] = primary_link["url"]

                # 尝试获取 HTML 内容
                try:
                    # 处理相对URL
                    link_url = primary_link["url"]
                    if link_url.startswith("/"):
                        from urllib.parse import urlparse
                        base = urlparse(config["url"])
                        link_url = f"{base.scheme}://{base.netloc}{link_url}"

                    print(f"      正在获取 HTML 报告内容: {link_url}")
                    resp_page = await context.new_page()
                    await resp_page.goto(link_url, wait_until="domcontentloaded", timeout=20000)
                    html_content = await resp_page.content()
                    result["html_content"] = html_content

                    # 保存到本地
                    safe_keyword = re.sub(r'[\\/:*?"<>|]', '_', keyword)
                    local_filename = out_dir / f"舆情报告_{safe_keyword}_{channel}.html"
                    with open(local_filename, "w", encoding="utf-8") as f:
                        f.write(html_content)
                    result["local_file"] = str(local_filename)
                    print(f"      ✓ HTML 报告已保存: {local_filename.name}")

                    await resp_page.close()
                except Exception as e:
                    print(f"      ⚠ 获取 HTML 内容失败: {e}")
                    result["error"] = f"HTML下载失败: {str(e)}"

            result["status"] = "success" if html_links else "partial"

        except Exception as e:
            result["error"] = str(e)
            result["status"] = "error"
            print(f"      ✗ 错误: {e}")

            # 错误时也截图
            try:
                err_screenshot = out_dir / f"{channel}_error.png"
                await page.screenshot(path=str(err_screenshot))
            except Exception:
                pass

        finally:
            await browser.close()

    return result


# ============================================================
# CLI 入口 & HTTP API（供前端调用）
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="AI渠道自动化搜索 - 输入提示词并获取HTML报告")
    parser.add_argument("--channel", "-c", required=True, choices=["yuanbao", "doubao", "qianwen"],
                        help="AI渠道: yuanbao(元宝) / doubao(豆包) / qianwen(千问)")
    parser.add_argument("--keyword", "-k", required=True, help="搜索关键词")
    parser.add_argument("--start", "-s", required=True, help="起始日期 (如: 2026年07月16日)")
    parser.add_argument("--end", "-e", required=True, help="结束日期 (如: 2026年07月22日)")
    parser.add_argument("--dim", "-d", default="opinion", choices=["opinion", "update", "both"],
                        help="监测类型: opinion/update/both (默认opinion)")
    parser.add_argument("--headless", action="store_true", default=True, help="无头模式运行浏览器")
    parser.add_argument("--output-dir", "-o", default=None, help="输出目录")
    parser.add_argument("--serve", action="store_true", help="启动HTTP服务模式（供前端调用）")
    parser.add_argument("--port", type=int, default=8765, help="HTTP服务端口")
    args = parser.parse_args()

    if args.serve:
        # HTTP 服务模式
        run_server(args.port, args.output_dir)
    else:
        # 单次执行模式
        result = asyncio.run(run_automation(
            channel=args.channel,
            keyword=args.keyword,
            start_date=args.start,
            end_date=args.end,
            dim_key=args.dim,
            headless=args.headless,
            output_dir=args.output_dir,
        ))
        # 输出JSON结果到stdout（供前端解析）
        print("\n=== RESULT_JSON_START ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("=== RESULT_JSON_END ===")


def run_server(port, output_dir):
    """启动简单的HTTP API服务器，供前端调用"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse

    class RequestHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if self.path == "/api/search":
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                params = json.loads(body.decode())

                # 异步执行自动化
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(run_automation(
                        channel=params.get("channel", "yuanbao"),
                        keyword=params.get("keyword", ""),
                        start_date=params.get("start", ""),
                        end_date=params.get("end", ""),
                        dim_key=params.get("dim", "opinion"),
                        headless=True,
                        output_dir=output_dir,
                    ))
                except Exception as e:
                    result = {"status": "error", "error": str(e)}
                finally:
                    loop.close()

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            print(f"[HTTP] {args[0]}")  # 简化日志

    server = HTTPServer(("127.0.0.1", port), RequestHandler)
    print(f"\n🚀 AI 自动化搜索服务已启动: http://127.0.0.1:{port}")
    print(f"   POST /api/search  (参数: channel, keyword, start, end, dim)")
    print(f"   按 Ctrl+C 停止\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
