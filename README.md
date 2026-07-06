# code-quality

生成离线 HTML 代码质量报告的自用 CLI。

```bash
uv tool install jeseki-code-quality
```

```bash
uv run code-quality
uv run cq
uv run code-quality --root /path/to/project --output /path/to/report.html
uv run cq -r /path/to/project -o /path/to/report.html -t
```

默认扫描当前工作目录下的 `src/server` 和 `src/client`，并输出到
`<root>/tmp/code_quality_report.html`。
