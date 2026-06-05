#!/usr/bin/env python3
"""
weread-exporter — 微信读书全本导出工具

通过 Playwright 自动化浏览器 + Canvas fillText Hook，
从微信读书网页版提取完整书籍内容并导出为 Markdown。

支持付费书籍（需有效的无限卡或购买记录）。

用法:
    python weread_export.py <book_url_or_id>

示例:
    python weread_export.py https://weread.qq.com/web/bookDetail/dd6324f0813ab9f97g019a24
    python weread_export.py dd6324f0813ab9f97g019a24
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys

from playwright.async_api import async_playwright

USER_DATA_DIR = os.path.join("cache", "browser_profile")

CANVAS_HOOK = """
(function() {
    window.__wr_chars = [];
    window.__wr_page_marks = [0];

    var orig = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim()) {
            window.__wr_chars.push({
                t: text,
                x: Math.round(x * 10) / 10,
                y: Math.round(y * 10) / 10
            });
        }
        return orig.apply(this, arguments);
    };

    window.__wr_mark = function() {
        window.__wr_page_marks.push(window.__wr_chars.length);
    };

    window.__wr_count = function() {
        return window.__wr_chars.length;
    };
})();
"""

MEASURE_RE = re.compile(
    r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$'
)

SENTENCE_END = set("。！？；：」）】》…—")


# ── WeRead URL hash ──────────────────────────────────────────

def wr_hash(s: str) -> str:
    """生成微信读书章节 reader URL 后缀 hash（逆向自前端 JS）"""
    h = hashlib.md5(s.encode()).hexdigest()
    result = h[:3] + "32" + h[-2:]
    chunks = []
    for i in range(0, len(s), 9):
        chunks.append("%x" % int(s[i : min(i + 9, len(s))]))
    for i, chunk in enumerate(chunks):
        width = "%x" % len(chunk)
        if len(width) == 1:
            width = "0" + width
        result += width + chunk
        if i < len(chunks) - 1:
            result += "g"
    if len(result) < 20:
        result += h[: 20 - len(result)]
    result += hashlib.md5(result.encode()).hexdigest()[:3]
    return result


def parse_book_id(url_or_id: str) -> str:
    """从 URL 或直接 ID 中提取 book_id"""
    m = re.search(r'([0-9a-f]{20,})', url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


# ── Canvas 数据处理 ──────────────────────────────────────────

def split_spread(chars):
    """
    微信读书的 Canvas 同时渲染当前页和下一页（双页并排）。
    两轮渲染的字符按绘制顺序排列，在第一轮结束时 y 坐标会从高位重置到低位。
    此函数在重置点切分，返回两页的字符列表。
    """
    if len(chars) < 20:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]
    for j in range(1, len(singles)):
        prev_y = singles[j - 1][1]["y"]
        curr_y = singles[j][1]["y"]
        if prev_y > 400 and curr_y < 200:
            cut_idx = singles[j][0]
            return [chars[:cut_idx], chars[cut_idx:]]
    return [chars]


def reconstruct_page(chars):
    """从单页 fillText 捕获数据重建可读文本"""
    if not chars:
        return ""
    real = [c for c in chars
            if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return ""
    rows = {}
    for c in real:
        y_key = round(c["y"] / 3) * 3
        if y_key not in rows:
            rows[y_key] = []
        rows[y_key].append(c)
    lines = []
    for y_key in sorted(rows.keys()):
        sorted_chars = sorted(rows[y_key], key=lambda c: c["x"])
        line = "".join(c["t"] for c in sorted_chars)
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def clean_text(raw_text, chapter_title):
    """合并 Canvas 断行为自然段落"""
    lines = raw_text.split("\n")
    cleaned = []
    title_seen = False
    for line in lines:
        s = line.strip()
        if not title_seen and s == chapter_title:
            title_seen = True
            continue
        cleaned.append(s)

    paragraphs = []
    current = []
    for line in cleaned:
        if not line:
            if current:
                paragraphs.append("".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("".join(current))

    merged = []
    for para in paragraphs:
        if not para.strip():
            continue
        if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
            merged[-1] += para
        else:
            merged.append(para)
    return "\n\n".join(merged)


# ── Playwright 交互 ─────────────────────────────────────────

async def ensure_login(context):
    page = await context.new_page()
    print("  检查登录状态...")
    await page.goto("https://weread.qq.com/web/shelf", timeout=30000)
    await asyncio.sleep(3)
    if "login" in page.url.lower():
        print("\n  ⚠️  需要扫码登录微信读书")
        print("  请在弹出的浏览器窗口中用微信扫描二维码")
        await page.goto("https://weread.qq.com/#login", timeout=30000)
        for _ in range(120):
            await asyncio.sleep(5)
            if "login" not in page.url.lower():
                print("  ✅ 登录成功")
                break
        else:
            print("  ❌ 登录超时（10 分钟）")
            await page.close()
            return False
    else:
        print("  ✅ 已登录")
    await page.close()
    return True


async def fetch_book_meta(context, book_id):
    """从微信读书 API 获取书籍元数据"""
    page = await context.new_page()
    try:
        meta = await page.evaluate("""
            async (bookId) => {
                const r1 = await fetch('https://i.weread.qq.com/book/info?bookId=' + bookId,
                    {credentials: 'include'});
                const info = await r1.json();

                const r2 = await fetch('https://i.weread.qq.com/book/chapterInfos', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({bookIds: [bookId], synckeys: [0], teenmode: 0}),
                    credentials: 'include',
                });
                const chapData = await r2.json();

                let chapters = [];
                for (const item of (chapData.data || [])) {
                    chapters = item.updated || [];
                }

                return {
                    title: info.title || '',
                    author: info.author || '',
                    chapters: chapters.map(c => ({
                        id: c.chapterUid,
                        title: c.title,
                        words: c.wordCount || 0,
                    })),
                };
            }
        """, book_id)
        return meta
    except Exception as e:
        print(f"  ❌ 获取书籍信息失败: {e}")
        return None
    finally:
        await page.close()


async def wait_stable(page, min_chars=10, timeout=8):
    for _ in range(int(timeout / 0.5)):
        c1 = await page.evaluate("() => window.__wr_count()")
        await asyncio.sleep(0.5)
        c2 = await page.evaluate("() => window.__wr_count()")
        if c2 == c1 and c1 >= min_chars:
            return c2
    return await page.evaluate("() => window.__wr_count()")


async def fetch_chapter(context, book_id, chapter_id, raw_dir=None):
    reader_url = f"https://weread.qq.com/web/reader/{book_id}k{wr_hash(str(chapter_id))}"
    page = await context.new_page()
    try:
        await page.add_init_script(CANVAS_HOOK)
        await page.goto(reader_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        count = await wait_stable(page)
        if count < 5:
            return {"ok": False, "text": "", "pages": 0, "reason": "no_content"}

        stale = 0
        page_num = 0
        while page_num < 500:
            await page.evaluate("() => window.__wr_mark()")
            await page.keyboard.press("ArrowRight")
            await asyncio.sleep(1.2)
            new_count = await wait_stable(page, min_chars=count)
            if wr_hash(str(chapter_id)) not in page.url:
                break
            if new_count == count:
                stale += 1
                if stale >= 3:
                    break
            else:
                stale = 0
                count = new_count
            page_num += 1

        await page.evaluate("() => window.__wr_mark()")
        all_chars = await page.evaluate("() => window.__wr_chars")
        marks = await page.evaluate("() => window.__wr_page_marks")

        if raw_dir:
            os.makedirs(raw_dir, exist_ok=True)
            with open(os.path.join(raw_dir, f"{chapter_id}.json"), "w") as f:
                json.dump({"chars": all_chars, "marks": marks}, f, ensure_ascii=False)

        page_texts = []
        for i in range(len(marks) - 1):
            page_chars = all_chars[marks[i]:marks[i + 1]]
            for spread in split_spread(page_chars):
                text = reconstruct_page(spread)
                if text:
                    page_texts.append(text)

        full_text = "\n\n".join(page_texts)
        return {"ok": bool(full_text), "text": full_text, "pages": len(page_texts),
                "reason": "" if full_text else "empty"}

    except Exception as e:
        return {"ok": False, "text": "", "pages": 0, "reason": str(e)}
    finally:
        await page.close()


# ── 主流程 ───────────────────────────────────────────────────

async def main(book_id: str, output_dir: str = "output", save_raw: bool = True):
    print("=" * 60)
    print("  weread-exporter — 微信读书全本导出")
    print("=" * 60)

    os.makedirs(USER_DATA_DIR, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1200, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        if not await ensure_login(context):
            await context.close()
            return

        print("\n  获取书籍信息...")
        meta = await fetch_book_meta(context, book_id)
        if not meta or not meta.get("chapters"):
            print("  ❌ 无法获取章节列表，请检查 book_id 是否正确")
            await context.close()
            return

        title = meta["title"]
        author = meta["author"]
        chapters = meta["chapters"]
        total = len(chapters)

        book_dir = os.path.join(output_dir, book_id)
        md_dir = os.path.join(book_dir, "chapters")
        raw_dir = os.path.join(book_dir, "raw") if save_raw else None
        merged_file = os.path.join(output_dir, f"{title}.md")

        os.makedirs(md_dir, exist_ok=True)

        meta_file = os.path.join(book_dir, "meta.json")
        with open(meta_file, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        print(f"\n  📖 {title} — {author}")
        print(f"  📚 共 {total} 章\n")

        ok = fail = skip = 0
        for i, ch in enumerate(chapters):
            ch_id = ch["id"]
            ch_title = ch["title"]
            out_file = os.path.join(md_dir, f"{i + 1:03d}.md")

            if os.path.exists(out_file) and os.path.getsize(out_file) > 100:
                with open(out_file) as f:
                    if "[FAILED]" not in f.read():
                        print(f"  [{i + 1:3d}/{total}] skip  {ch_title}")
                        skip += 1
                        continue

            print(f"  [{i + 1:3d}/{total}] fetch {ch_title}...", end=" ", flush=True)
            result = await fetch_chapter(context, book_id, ch_id, raw_dir)

            if result["ok"]:
                cleaned = clean_text(result["text"], ch_title)
                with open(out_file, "w") as f:
                    f.write(f"# {ch_title}\n\n{cleaned}\n")
                print(f"ok  {len(cleaned)} chars ({result['pages']} pages)")
                ok += 1
            else:
                with open(out_file, "w") as f:
                    f.write(f"# {ch_title}\n\n[FAILED: {result['reason']}]\n")
                print(f"FAIL  {result['reason']}")
                fail += 1

            await asyncio.sleep(1.5)

        print(f"\n{'=' * 60}")
        print(f"  Done!  ok={ok}  skip={skip}  fail={fail}  total={total}")
        print(f"{'=' * 60}")

        if ok + skip > 0:
            print(f"\n  Merging → {merged_file}")
            md_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
            with open(merged_file, "w") as out:
                out.write(f"# {title}\n\n**{author}**\n\n---\n\n")
                for fname in md_files:
                    with open(os.path.join(md_dir, fname)) as inf:
                        out.write(inf.read())
                    out.write("\n\n---\n\n")
            size = os.path.getsize(merged_file)
            print(f"  ✅ {merged_file} ({size:,} bytes)")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="微信读书全本导出工具 — 通过 Canvas Hook 提取完整书籍内容")
    parser.add_argument("book", help="微信读书 book URL 或 book_id")
    parser.add_argument("-o", "--output", default="output", help="输出目录 (default: output)")
    parser.add_argument("--no-raw", action="store_true", help="不保存原始 Canvas 数据")
    args = parser.parse_args()

    book_id = parse_book_id(args.book)
    print(f"  Book ID: {book_id}")
    asyncio.run(main(book_id, args.output, save_raw=not args.no_raw))
