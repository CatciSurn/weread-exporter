#!/usr/bin/env python3
"""
weread-exporter — 微信读书全本导出工具 (2026 修复版)

原版依赖的 i.weread.qq.com App 接口已失效（返回"用户不存在"），微信读书网页版
现已全部走 WASM 签名接口，外部裸请求拿不到数据。本版改为纯网页阅读页方案：

  1. 从书籍 URL 中解析出加密 book 标识（reader/bookDetail 链接均可）
  2. 打开网页阅读器，从顶栏读书名、从目录(DOM)读章节列表与锁定状态
  3. 连续翻页(ArrowRight)，用 Canvas fillText 钩子抓取正文，
     按页面顶部当前章节标题把文字归入对应章节
  4. 坐标重建文本 → 段落格式化 → 输出 Markdown

不再依赖任何已失效的接口；锁定(付费未购)的章节无法导出，会被跳过。

用法:
    python weread_export.py <reader或bookDetail链接 / 加密book标识>
"""
import argparse
import asyncio
import json
import os
import re
import sys
import urllib.parse

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


# ── 工具函数 ────────────────────────────────────────────────

def extract_enc_book(url_or_id: str) -> str:
    """
    从 reader / bookDetail 链接或裸标识中取出加密的 book 标识。
    reader 链接形如 .../reader/{enc}k{chapterHash}，enc 与章节用 'k' 分隔，
    enc 本身只含 0-9 a-f 与分隔符 g，绝不含 'k'，故按首个 'k' 切分即可。
    """
    s = url_or_id.strip()
    m = re.search(r'/(?:reader|bookDetail)/([0-9a-zA-Z]+)', s)
    token = m.group(1) if m else s
    return token.split("k")[0]


def wr_hash(s: str) -> str:
    """把数字 chapterUid 转成阅读页 URL 里的加密 hash（逆向自前端 JS）。"""
    import hashlib
    h = hashlib.md5(s.encode()).hexdigest()
    result = h[:3] + "32" + h[-2:]
    chunks = [("%x" % int(s[i:min(i + 9, len(s))])) for i in range(0, len(s), 9)]
    for i, chunk in enumerate(chunks):
        w = "%x" % len(chunk)
        if len(w) == 1:
            w = "0" + w
        result += w + chunk
        if i < len(chunks) - 1:
            result += "g"
    if len(result) < 20:
        result += h[:20 - len(result)]
    result += hashlib.md5(result.encode()).hexdigest()[:3]
    return result


def norm(t: str) -> str:
    """归一化标题（去掉所有空白），用于章节匹配。"""
    return re.sub(r"\s+", "", t or "")


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "未命名"


# ── Canvas 数据处理（沿用原版，已验证有效）──────────────────

def split_spread(chars):
    """微信读书 Canvas 同时渲染当前页和下一页，按 y 坐标重置点切分两页。"""
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
    """从单页 fillText 捕获数据重建可读文本。"""
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
    """合并 Canvas 断行为自然段落。"""
    lines = raw_text.split("\n")
    cleaned = []
    title_seen = False
    nt = norm(chapter_title)
    for line in lines:
        s = line.strip()
        if not title_seen and norm(s) == nt and nt:
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
    """按 wr_vid cookie 判断真实登录态（不再靠 URL 猜）。"""
    page = await context.new_page()
    print("  检查登录状态...")
    try:
        await page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(2)
    vid, _ = await read_identity(context)
    if vid and vid != "?":
        print("  ✅ 已登录")
        await page.close()
        return True

    print("\n  ⚠️ 未登录。请在弹出的浏览器窗口中点击右上角【登录】，用你自己的微信扫码")
    for _ in range(120):  # 最多等 10 分钟
        await asyncio.sleep(5)
        vid, _ = await read_identity(context)
        if vid and vid != "?":
            print("  ✅ 登录成功")
            await page.close()
            return True
    print("  ❌ 登录超时（10 分钟）")
    await page.close()
    return False


async def wait_stable(page, min_chars=10, timeout=8):
    for _ in range(int(timeout / 0.5)):
        c1 = await page.evaluate("() => window.__wr_count ? window.__wr_count() : 0")
        await asyncio.sleep(0.5)
        c2 = await page.evaluate("() => window.__wr_count ? window.__wr_count() : 0")
        if c2 == c1 and c1 >= min_chars:
            return c2
    return await page.evaluate("() => window.__wr_count ? window.__wr_count() : 0")


async def open_catalog(page):
    """确保目录面板已展开。"""
    vis = await page.evaluate(
        "() => { const li=document.querySelector('li.readerCatalog_list_item');"
        " return !!(li && li.offsetParent !== null); }")
    if not vis:
        await page.evaluate(
            "() => { const b=document.querySelector('.readerControls_item.catalog')"
            " || document.querySelector('[class*=catalog]'); if(b) b.click(); }")
        await asyncio.sleep(1.3)


async def close_catalog(page):
    vis = await page.evaluate(
        "() => { const li=document.querySelector('li.readerCatalog_list_item');"
        " return !!(li && li.offsetParent !== null); }")
    if vis:
        await page.evaluate(
            "() => { const b=document.querySelector('.readerControls_item.catalog'); if(b) b.click(); }")
        await asyncio.sleep(0.8)


async def read_chapter_title(page):
    return await page.evaluate(
        "() => { const e=document.querySelector('.renderTargetPageInfo_header_chapterTitle');"
        " return e ? e.textContent.trim() : ''; }")


async def read_book_title(page):
    return await page.evaluate(
        "() => { const e=document.querySelector('.readerTopBar_title_link');"
        " return e ? e.textContent.trim() : ''; }")


async def read_catalog(page):
    """读取目录：每章标题 + 是否锁定。"""
    await open_catalog(page)
    items = await page.evaluate(r"""() => {
        const lis = document.querySelectorAll('li.readerCatalog_list_item');
        return Array.from(lis).map(li => {
            const tt = li.querySelector('.readerCatalog_list_item_title_text')
                    || li.querySelector('.readerCatalog_list_item_title');
            const title = ((tt ? tt.textContent : li.textContent) || '').trim();
            return { title, locked: /_lock|_disabled/.test(li.className) };
        });
    }""")
    return items


# ── 连续翻页抓取 ─────────────────────────────────────────────

async def goto_catalog_index(page, index):
    """跳转到目录第 index 章（点击目录项；点击前清空缓冲，确保首页被抓到）。"""
    await open_catalog(page)
    await page.evaluate("() => { window.__wr_chars=[]; window.__wr_page_marks=[0]; }")
    await page.evaluate(
        "(i)=>{const l=document.querySelectorAll('li.readerCatalog_list_item');"
        " if(l && l[i]) l[i].click();}", index)
    await asyncio.sleep(2.8)


async def read_identity(context):
    """从 cookie 读出当前登录账号的 vid 与昵称，用于核对身份。"""
    try:
        cookies = await context.cookies("https://weread.qq.com")
    except Exception:
        return "?", ""
    vid = next((c["value"] for c in cookies if c["name"] == "wr_vid"), "?")
    name = next((c["value"] for c in cookies if c["name"] == "wr_name"), "")
    try:
        name = urllib.parse.unquote(name)
    except Exception:
        pass
    return vid, name


async def read_sel_index(page):
    """读取当前阅读所在的目录索引（带 _selected 类的目录项）。返回 -1 表示未知。"""
    return await page.evaluate(
        "() => { let idx=-1; document.querySelectorAll('li.readerCatalog_list_item')"
        ".forEach((li,i)=>{ if(/_selected/.test(li.className)) idx=i; }); return idx; }")


async def goto_chapter(page, enc_book, uid):
    """用 uid 直接跳转到对应章节（可靠，已验证）。"""
    url = f"https://weread.qq.com/web/reader/{enc_book}k{wr_hash(str(uid))}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    except Exception:
        pass
    await asyncio.sleep(2.6)


async def derive_uid_offset(page, enc_book, probe_index):
    """
    标定 "目录索引 → chapterUid" 的偏移量：uid = 目录索引 + offset。
    做法：用某个猜测 uid 跳转，读出实际落在的目录索引，反推 offset。
    """
    for guess in (2, 3, 1, 4, 0, 5):
        uid = probe_index + guess
        await goto_chapter(page, enc_book, uid)
        sel = await read_sel_index(page)
        if sel is not None and sel >= 0:
            return uid - sel
    return 2  # 兜底


async def sweep_from(page, max_turns=4000, reset=True):
    """
    从当前页连续翻页抓取，每页登记其所在目录索引(selIdx)。
    翻不动(到全书末尾)即停止。返回 (all_chars, marks, seg_idx)。
    """
    if reset:
        await page.evaluate("() => { window.__wr_chars=[]; window.__wr_page_marks=[0]; }")
        await asyncio.sleep(0.4)

    seg_idx = []
    last_count = await wait_stable(page, min_chars=3)
    last_url = page.url
    last_sel = await read_sel_index(page)
    stale = 0

    for _ in range(max_turns):
        cur_sel = await read_sel_index(page)
        if cur_sel < 0:
            cur_sel = last_sel
        await page.evaluate("() => window.__wr_mark()")
        seg_idx.append(cur_sel)
        last_sel = cur_sel

        await page.keyboard.press("ArrowRight")
        await asyncio.sleep(1.1)
        new_count = await wait_stable(page, min_chars=last_count)

        if new_count == last_count and page.url == last_url:
            stale += 1
            if stale >= 4:
                break
        else:
            stale = 0
            last_count = new_count
            last_url = page.url

    all_chars = await page.evaluate("() => window.__wr_chars")
    marks = await page.evaluate("() => window.__wr_page_marks")
    return all_chars, marks, seg_idx


def group_by_index(all_chars, marks, seg_idx, catalog):
    """
    按"连续相同目录索引"切分章节，用目录标题命名。
    本书章节名循环重复(亚历克斯/凯特…)，但目录索引唯一，故按索引切分绝不混淆。
    返回 [(title, text, idx)]。
    """
    runs = []          # [(idx, [texts])]
    cur_idx = None
    cur_texts = []
    n = min(len(seg_idx), len(marks) - 1)
    for k in range(n):
        seg = all_chars[marks[k]:marks[k + 1]]
        texts = []
        for spread in split_spread(seg):
            t = reconstruct_page(spread)
            if t:
                texts.append(t)
        if not texts:
            continue
        idx = seg_idx[k]
        if cur_idx is None:
            cur_idx = idx
        if idx != cur_idx:
            runs.append((cur_idx, cur_texts))
            cur_idx, cur_texts = idx, []
        cur_texts.extend(texts)
    if cur_texts:
        runs.append((cur_idx, cur_texts))

    result = []
    for idx, texts in runs:
        title = catalog[idx]["title"] if (catalog and 0 <= idx < len(catalog)) else f"章节{idx}"
        cleaned = clean_text("\n\n".join(texts), title)
        if cleaned.strip():
            result.append((title, cleaned, idx))
    return result


# ── 主流程 ───────────────────────────────────────────────────

async def main(enc_book: str, output_dir: str = "output", save_raw: bool = True,
               relogin: bool = False, last_n: int = None):
    print("=" * 60)
    print("  weread-exporter — 微信读书导出 (2026 修复版)")
    print("=" * 60)
    print(f"  book 标识: {enc_book}")

    os.makedirs(USER_DATA_DIR, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1200, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        if relogin:
            print("\n  🔄 已请求重新登录：清除旧登录态...")
            try:
                await context.clear_cookies()
            except Exception as e:
                print(f"    clear_cookies 提示: {repr(e)[:60]}")
            tmp = await context.new_page()
            try:
                await tmp.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000)
                await tmp.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch(e) {} }")
            except Exception:
                pass
            await tmp.close()
            print("  ➡️ 接下来请在弹出的浏览器窗口里，用【你自己的】微信扫码登录")

        if not await ensure_login(context):
            await context.close()
            return

        vid, name = await read_identity(context)
        print(f"  👤 当前登录账号：vid={vid}  昵称={name or '(未读到)'}")

        page = await context.new_page()
        await page.add_init_script(CANVAS_HOOK)
        reader_url = f"https://weread.qq.com/web/reader/{enc_book}"
        print(f"\n  打开阅读器: {reader_url}")
        try:
            await page.goto(reader_url, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print(f"  ⚠️ 页面加载提示: {repr(e)[:80]}")
        await asyncio.sleep(5)

        title = await read_book_title(page) or enc_book
        catalog = await read_catalog(page)
        if not catalog:
            print("  ❌ 没读到目录，可能未登录或该书不可读")
            await context.close()
            return
        locked = [c for c in catalog if c["locked"]]
        readable = [c for c in catalog if not c["locked"]]
        locked_titles = set(norm(c["title"]) for c in locked)

        print(f"\n  📖 {title}")
        print(f"  📚 目录共 {len(catalog)} 条，可读 {len(readable)} 条，锁定 {len(locked)} 条")
        print("  —— 目录 ——")
        for i, c in enumerate(catalog):
            print(f"    [{i:2d}] {'🔒' if c['locked'] else '  '} {c['title']}")
        if locked:
            print(f"  ⚠️ 有 {len(locked)} 条被锁定（付费未购/无权限），无法导出，将跳过")

        # 确定要抓的目录索引范围
        if last_n:
            start_index = max(0, len(catalog) - last_n)
        else:
            start_index = 0
        target_set = set(range(start_index, len(catalog)))

        print("\n  标定 章节uid 偏移...")
        offset = await derive_uid_offset(page, enc_book, start_index)
        start_uid = start_index + offset
        print(f"  offset={offset}  起始 uid={start_uid}（目录[{start_index}] {catalog[start_index]['title']}）")
        print(f"  🎯 目标：[{start_index}]{catalog[start_index]['title']} → "
              f"[{len(catalog)-1}]{catalog[-1]['title']}  共 {len(target_set)} 章")

        await goto_chapter(page, enc_book, start_uid)
        landed = await read_sel_index(page)
        if landed != start_index:
            print(f"  ⚠️ 落点目录索引为 {landed}，期望 {start_index}，继续抓取（按实际索引归章）")

        print("\n  开始连续翻页抓取...（约 1 秒/页，请勿操作弹出的浏览器窗口）")
        all_chars, marks, seg_idx = await sweep_from(page, reset=True)
        print(f"  抓取完成：{len(all_chars)} 个字符片段，{len(marks) - 1} 页")

        book_dir = os.path.join(output_dir, safe_filename(enc_book))
        md_dir = os.path.join(book_dir, "chapters")
        os.makedirs(md_dir, exist_ok=True)
        if save_raw:
            with open(os.path.join(book_dir, "raw.json"), "w", encoding="utf-8") as f:
                json.dump({"chars": all_chars, "marks": marks, "seg_idx": seg_idx},
                          f, ensure_ascii=False)

        all_grouped = group_by_index(all_chars, marks, seg_idx, catalog)
        # 只保留目标范围内的章节
        chapters = [(t, txt, idx) for (t, txt, idx) in all_grouped if idx in target_set]
        chapters.sort(key=lambda x: x[2])

        with open(os.path.join(book_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"title": title, "enc_book": enc_book, "catalog": catalog,
                       "exported": [{"idx": idx, "title": t} for t, _, idx in chapters]},
                      f, ensure_ascii=False, indent=2)

        for ch_title, text, idx in chapters:
            out_file = os.path.join(md_dir, f"{idx:03d}.md")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(f"# {ch_title}\n\n{text}\n")
            print(f"  [{idx:2d}] {ch_title}  ({len(text)} 字)")

        print(f"\n{'=' * 60}")
        print(f"  Done!  导出 {len(chapters)} 章")
        print(f"{'=' * 60}")

        if chapters:
            suffix = f"_末{last_n}章" if last_n else ""
            merged_file = os.path.join(output_dir, safe_filename(title) + suffix + ".md")
            with open(merged_file, "w", encoding="utf-8") as out:
                out.write(f"# {title}\n\n---\n\n")
                for ch_title, text, idx in chapters:
                    out.write(f"# {ch_title}\n\n{text}\n\n---\n\n")
            size = os.path.getsize(merged_file)
            print(f"\n  ✅ 合并文件: {merged_file} ({size:,} bytes)")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="微信读书导出工具 (2026 修复版) — 网页阅读页 + Canvas Hook")
    parser.add_argument("book", help="微信读书 reader/bookDetail 链接 或 加密 book 标识")
    parser.add_argument("-o", "--output", default="output", help="输出目录 (default: output)")
    parser.add_argument("--no-raw", action="store_true", help="不保存原始 Canvas 数据")
    parser.add_argument("--relogin", action="store_true", help="清除旧登录态，强制重新扫码登录")
    parser.add_argument("--last", type=int, default=None, metavar="N", help="只爬取末 N 章")
    args = parser.parse_args()

    enc = extract_enc_book(args.book)
    asyncio.run(main(enc, args.output, save_raw=not args.no_raw,
                     relogin=args.relogin, last_n=args.last))
