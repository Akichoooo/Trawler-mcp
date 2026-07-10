from trawler.parser.quality import markdown_quality


def test_markdown_quality_counts_structure():
    markdown = """# Title

Text with [a link](https://example.com).

| A | B |
|---|---|
| 1 | 2 |

```python
print("hi")
```
"""

    result = markdown_quality(markdown)

    assert result["char_count"] == len(markdown)
    assert result["heading_count"] == 1
    assert result["link_count_markdown"] == 1
    assert result["table_count"] == 1
    assert result["code_block_count"] == 1
