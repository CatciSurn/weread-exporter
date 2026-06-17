# weread-exporter

微信读书全本导出工具 — 通过 Playwright + Canvas Hook 提取完整书籍内容，导出为 Markdown。

## 原理

微信读书网页版使用 Canvas 渲染书籍文字（而非 DOM 文本节点），本工具通过以下技术栈实现内容提取：

1. **Playwright 自动化** — 启动 Chromium，持久化登录会话（扫码一次，后续自动复用）
2. **Canvas fillText Hook** — 在页面 JS 执行前注入钩子，拦截所有 `CanvasRenderingContext2D.fillText()` 调用
3. **双页拆分** — 微信读书在同一 Canvas 上同时渲染当前页和下一页（预渲染优化），本工具通过检测 y 坐标重置点将两页字符流正确分离
4. **文本重建** — 按 (x, y) 坐标将捕获的单字符重组为行和段落
5. **格式清理** — 合并 Canvas 渲染断行，还原自然段落

## 安装

```bash
pip install playwright
playwright install chromium
```

## 使用

```bash
# 末 N 章
C:\Users\CatciSurn\miniconda3\python.exe weread_export.py "<书链接>" --last 5
# 全本
C:\Users\CatciSurn\miniconda3\python.exe weread_export.py "<书链接>"
# 换账号
... --relogin
```

首次运行会弹出浏览器窗口要求扫码登录微信读书，登录后会话自动保存在 `cache/browser_profile/`。

## 输出

```
output/
├── <book_id>/
│   ├── meta.json          # 书籍元数据（标题、作者、章节列表）
│   ├── chapters/          # 每章独立 Markdown
│   │   ├── 001.md
│   │   ├── 002.md
│   │   └── ...
│   └── raw/               # 原始 Canvas 坐标数据（可选，用于调试/重处理）
│       ├── 2.json
│       └── ...
└── 书名.md                # 合并后的全本文件
```

## 限制

- 需要有效的微信读书账号，且对目标书籍有阅读权限（无限卡会员或已购买）
- 部分出版社限制网页端阅读（显示"去 App 阅读"），此类书籍无法导出
- 导出速度受翻页等待时间限制，约 20-30 秒/章

## 工作流程

```
浏览器登录 → 获取书籍元数据 → 逐章打开 reader 页面
    → 注入 Canvas Hook → 翻页收集 fillText 数据
    → 双页拆分 → 坐标重建文本 → 段落格式化 → 输出 Markdown
```

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播，请尊重著作权。
