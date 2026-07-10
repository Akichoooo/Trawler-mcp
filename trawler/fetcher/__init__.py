"""fetcher 包 — 自纠错阶梯。

两轴:
- Fetcher 阶梯 (怎么拿 HTML): patchright → Jina → HITL
- Parser 阶梯 (从 HTML 提正文): trafilatura → readability → html2text (在 parser/)

每档拿到响应立刻过 3 层主动检测后短路决策。
"""
