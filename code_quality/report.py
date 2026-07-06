#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate an offline HTML code quality report based on effective LOC."""

from __future__ import annotations

import ast
import html
import io
import json
import tokenize
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal


GroupName = Literal["backend", "frontend"]
ScoreStatus = Literal["healthy", "warning", "danger"]

FRONTEND_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".css"}
IGNORED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "data",
    "dist",
    "dist-ssr",
    "logs",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class FileScore:
    path: Path
    relative_path: str
    group: GroupName
    effective_lines: int
    score: float
    status: ScoreStatus
    included_in_score: bool
    is_test: bool
    warning: str | None = None


@dataclass(frozen=True)
class GroupScore:
    name: GroupName | Literal["system"]
    label: str
    score: float | None
    file_count: int
    included_count: int
    healthy_count: int
    warning_count: int
    danger_count: int
    total_effective_lines: int


@dataclass(frozen=True)
class QualityReport:
    root: Path
    generated_at: datetime
    include_tests: bool
    files: list[FileScore]
    backend: GroupScore
    frontend: GroupScore
    system: GroupScore


def has_ignored_parent(path: Path) -> bool:
    return any(part in IGNORED_DIR_NAMES or part.startswith("tmp") for part in path.parts)


def is_test_file(path: Path) -> bool:
    name = path.name
    return (
        "tests" in path.parts
        or "__tests__" in path.parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def python_docstring_lines(source: str) -> set[int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    spans: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        end_lineno = getattr(first, "end_lineno", first.lineno)
        spans.update(range(first.lineno, end_lineno + 1))

    return spans


def count_python_effective_lines(path: Path) -> tuple[int, str | None]:
    source = path.read_text(encoding="utf-8")
    docstring_lines = python_docstring_lines(source)
    lines: set[int] = set()
    warning = None

    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type in {
                tokenize.COMMENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.INDENT,
                tokenize.NL,
                tokenize.NEWLINE,
            }:
                continue
            start_line, _ = token.start
            end_line, _ = token.end
            for line_number in range(start_line, end_line + 1):
                if line_number not in docstring_lines:
                    lines.add(line_number)
    except (SyntaxError, tokenize.TokenError, UnicodeDecodeError) as exc:
        warning = f"Python tokenization fallback: {exc}"
        return count_simple_effective_lines(source.splitlines()), warning

    return len(lines), warning


def count_simple_effective_lines(lines: Iterable[str]) -> int:
    return sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))


def count_web_effective_lines(path: Path) -> tuple[int, str | None]:
    source = path.read_text(encoding="utf-8")
    line_has_code = [False for _ in source.splitlines()]
    if source and source.endswith("\n"):
        # splitlines() intentionally omits the final empty line.
        pass

    line_index = 0
    in_block_comment = False
    in_string: str | None = None
    escaped = False
    line_comment_enabled = path.suffix != ".css"
    i = 0

    while i < len(source):
        char = source[i]
        next_char = source[i + 1] if i + 1 < len(source) else ""

        if char == "\n":
            line_index += 1
            if in_string != "`":
                in_string = None
                escaped = False
            i += 1
            continue

        if line_index >= len(line_has_code):
            line_has_code.append(False)

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if in_string:
            if not char.isspace():
                line_has_code[line_index] = True
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            i += 1
            continue

        if char == "/" and next_char == "*":
            in_block_comment = True
            i += 2
            continue

        if line_comment_enabled and char == "/" and next_char == "/":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue

        if char in {"'", '"', "`"}:
            in_string = char
            line_has_code[line_index] = True
            i += 1
            continue

        if not char.isspace():
            line_has_code[line_index] = True

        i += 1

    return sum(1 for has_code in line_has_code if has_code), None


def linear_score(effective_lines: int, healthy_limit: int, danger_limit: int) -> float:
    if effective_lines <= healthy_limit:
        return 100
    if effective_lines >= danger_limit:
        return 0

    score = (danger_limit - effective_lines) / (danger_limit - healthy_limit) * 100
    return round(score, 1)


def score_file(group: GroupName, effective_lines: int) -> tuple[float, ScoreStatus]:
    if group == "backend":
        score = linear_score(effective_lines, healthy_limit=300, danger_limit=600)
    else:
        score = linear_score(effective_lines, healthy_limit=600, danger_limit=1000)

    if score >= 100:
        return score, "healthy"
    if score <= 0:
        return score, "danger"
    return score, "warning"


def iter_source_files(root: Path) -> Iterable[tuple[Path, GroupName]]:
    backend_root = root / "src" / "server"
    frontend_root = root / "src" / "client"

    if backend_root.exists():
        for path in sorted(backend_root.rglob("*.py")):
            if not has_ignored_parent(path.relative_to(root)):
                yield path, "backend"

    if frontend_root.exists():
        for path in sorted(frontend_root.rglob("*")):
            if (
                path.is_file()
                and path.suffix in FRONTEND_EXTENSIONS
                and not has_ignored_parent(path.relative_to(root))
            ):
                yield path, "frontend"


def build_file_score(root: Path, path: Path, group: GroupName, include_tests: bool) -> FileScore:
    if group == "backend":
        effective_lines, warning = count_python_effective_lines(path)
    else:
        effective_lines, warning = count_web_effective_lines(path)

    score, status = score_file(group, effective_lines)
    test_file = is_test_file(path.relative_to(root))

    return FileScore(
        path=path,
        relative_path=path.relative_to(root).as_posix(),
        group=group,
        effective_lines=effective_lines,
        score=score,
        status=status,
        included_in_score=include_tests or not test_file,
        is_test=test_file,
        warning=warning,
    )


def summarize_group(
    name: GroupName | Literal["system"],
    label: str,
    files: list[FileScore],
) -> GroupScore:
    included = [file for file in files if file.included_in_score]
    total_effective_lines = sum(file.effective_lines for file in included)
    score: float | None
    if not included:
        score = None
    elif total_effective_lines == 0:
        score = 100
    else:
        score = sum(
            file.score * file.effective_lines for file in included
        ) / total_effective_lines

    return GroupScore(
        name=name,
        label=label,
        score=score,
        file_count=len(files),
        included_count=len(included),
        healthy_count=sum(1 for file in included if file.status == "healthy"),
        warning_count=sum(1 for file in included if file.status == "warning"),
        danger_count=sum(1 for file in included if file.status == "danger"),
        total_effective_lines=total_effective_lines,
    )


def build_report(root: Path, include_tests: bool = False) -> QualityReport:
    root = root.resolve()
    files = [
        build_file_score(root, path, group, include_tests)
        for path, group in iter_source_files(root)
    ]
    backend_files = [file for file in files if file.group == "backend"]
    frontend_files = [file for file in files if file.group == "frontend"]

    return QualityReport(
        root=root,
        generated_at=datetime.now(),
        include_tests=include_tests,
        files=files,
        backend=summarize_group("backend", "后端", backend_files),
        frontend=summarize_group("frontend", "前端", frontend_files),
        system=summarize_group("system", "系统", files),
    )


def format_score(score: float | int | None) -> str:
    if score is None:
        return "N/A"
    if float(score).is_integer():
        return str(int(score))
    return f"{score:.1f}"


def score_class(score: float | None) -> str:
    if score is None:
        return "na"
    if score >= 90:
        return "healthy"
    if score >= 60:
        return "warning"
    return "danger"


def group_to_dict(group: GroupScore) -> dict[str, object]:
    return {
        "score": None if group.score is None else round(group.score, 1),
        "fileCount": group.file_count,
        "includedCount": group.included_count,
        "healthyCount": group.healthy_count,
        "warningCount": group.warning_count,
        "dangerCount": group.danger_count,
        "totalEffectiveLines": group.total_effective_lines,
    }


def render_metric_card(group: GroupScore) -> str:
    score = format_score(group.score)
    css_class = score_class(group.score)
    score_value = "null" if group.score is None else f"{group.score:.1f}"
    return f"""
        <section class="score-card {css_class}">
          <div class="score-card__meta">
            <span>{html.escape(group.label)}</span>
            <strong>{group.included_count}</strong>
          </div>
          <div class="score-card__score" data-score="{score_value}">{score}</div>
          <div class="score-card__bar"><span style="--target:{0 if group.score is None else group.score}%"></span></div>
          <dl class="score-card__details">
            <div><dt>健康</dt><dd>{group.healthy_count}</dd></div>
            <div><dt>警告</dt><dd>{group.warning_count}</dd></div>
            <div><dt>危险</dt><dd>{group.danger_count}</dd></div>
            <div><dt>有效行</dt><dd>{group.total_effective_lines}</dd></div>
          </dl>
        </section>
    """


def render_file_rows(files: list[FileScore]) -> str:
    rows = []
    for file in sorted(files, key=lambda item: (-item.effective_lines, item.relative_path)):
        rows.append(
            f"""
            <tr class="{file.status}{'' if file.included_in_score else ' excluded'}" data-status="{file.status}">
              <td><code>{html.escape(file.relative_path)}</code></td>
              <td>{'后端' if file.group == 'backend' else '前端'}</td>
              <td>{file.effective_lines}</td>
              <td>{format_score(file.score)}</td>
              <td><span class="pill {file.status}">{status_label(file.status)}</span></td>
              <td>{'计分' if file.included_in_score else '测试文件'}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def status_label(status: ScoreStatus) -> str:
    return {"healthy": "健康", "warning": "警告", "danger": "危险"}[status]


def render_top_files(files: list[FileScore]) -> str:
    included = [file for file in files if file.included_in_score]
    ranked_files = sorted(included, key=lambda item: (-item.effective_lines, item.relative_path))
    if not ranked_files:
        return '<p class="empty">没有可计分文件。</p>'

    items = []
    max_lines = max(file.effective_lines for file in ranked_files) or 1
    for file in ranked_files:
        width = round(file.effective_lines / max_lines * 100, 2)
        items.append(
            f"""
            <li data-group="{file.group}">
              <div>
                <strong>{html.escape(file.relative_path)}</strong>
                <span>{file.effective_lines} 行 · {format_score(file.score)} 分</span>
              </div>
              <div class="rank-bar {file.status}"><span style="--target:{width}%"></span></div>
            </li>
            """
        )
    return f"""
      <ol class="top-list" data-top-list>{''.join(items)}</ol>
      <p class="empty is-hidden" data-top-empty>没有匹配的文件。</p>
    """


def render_warnings(files: list[FileScore]) -> str:
    warnings = [file for file in files if file.warning]
    if not warnings:
        return ""

    items = "".join(
        f"<li><code>{html.escape(file.relative_path)}</code>: {html.escape(file.warning or '')}</li>"
        for file in warnings
    )
    return f"""
      <section class="panel warnings">
        <h2>解析警告</h2>
        <ul>{items}</ul>
      </section>
    """


def render_html(report: QualityReport) -> str:
    report_json = json.dumps(
        {
            "system": group_to_dict(report.system),
            "backend": group_to_dict(report.backend),
            "frontend": group_to_dict(report.frontend),
        },
        ensure_ascii=False,
    )
    generated_at = report.generated_at.strftime("%Y-%m-%d %H:%M:%S")
    test_policy = "参与评分" if report.include_tests else "默认排除"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>代码质量报告</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --surface: #ffffff;
      --surface-strong: #101828;
      --muted: #667085;
      --line: #d9e2ec;
      --green: #169b62;
      --green-soft: #e7f7ef;
      --amber: #c97706;
      --amber-soft: #fff4dc;
      --red: #d33f49;
      --red-soft: #ffeaec;
      --blue: #276ef1;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(135deg, rgba(39, 110, 241, 0.08), transparent 28rem),
        linear-gradient(315deg, rgba(22, 155, 98, 0.08), transparent 30rem),
        var(--bg);
      color: #172033;
    }}
    main {{
      width: min(1440px, calc(100% - 48px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: end;
      margin-bottom: 24px;
    }}
    h1, h2 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 32px; line-height: 1.15; color: var(--surface-strong); }}
    h2 {{ font-size: 18px; }}
    .subtitle {{ margin: 10px 0 0; color: var(--muted); max-width: 820px; line-height: 1.7; }}
    .meta {{
      display: grid;
      gap: 8px;
      min-width: 260px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,0.76);
    }}
    .meta span {{ color: var(--muted); font-size: 13px; }}
    .meta strong {{ font-size: 14px; color: #24304a; }}
    .score-grid {{
      display: grid;
      grid-template-columns: 1.1fr 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .score-card, .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,0.9);
      box-shadow: 0 18px 40px rgba(16, 24, 40, 0.08);
    }}
    .score-card {{ padding: 20px; overflow: hidden; }}
    .score-card__meta {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      color: var(--muted);
      font-size: 14px;
    }}
    .score-card__meta strong {{ color: #24304a; }}
    .score-card__score {{
      margin-top: 12px;
      font-size: 56px;
      line-height: 1;
      font-weight: 760;
      color: var(--surface-strong);
    }}
    .score-card.healthy .score-card__score {{ color: var(--green); }}
    .score-card.warning .score-card__score {{ color: var(--amber); }}
    .score-card.danger .score-card__score {{ color: var(--red); }}
    .score-card__bar, .rank-bar {{
      height: 9px;
      margin-top: 16px;
      overflow: hidden;
      border-radius: 999px;
      background: #edf1f7;
    }}
    .score-card__bar span, .rank-bar span {{
      display: block;
      width: var(--target);
      height: 100%;
      transform-origin: left;
      animation: grow 880ms cubic-bezier(.2,.8,.2,1) both;
    }}
    .score-card.healthy .score-card__bar span, .rank-bar.healthy span {{ background: var(--green); }}
    .score-card.warning .score-card__bar span, .rank-bar.warning span {{ background: var(--amber); }}
    .score-card.danger .score-card__bar span, .rank-bar.danger span {{ background: var(--red); }}
    .score-card.na .score-card__bar span {{ background: var(--muted); }}
    .score-card__details {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin: 18px 0 0;
    }}
    .score-card__details div {{
      min-width: 0;
      padding: 10px;
      border-radius: 8px;
      background: #f7f9fc;
    }}
    dt {{ color: var(--muted); font-size: 12px; }}
    dd {{ margin: 3px 0 0; font-weight: 720; color: #24304a; }}
    .content-grid {{
      display: grid;
      grid-template-columns: 0.92fr 1.08fr;
      gap: 16px;
      align-items: start;
    }}
    .panel {{ padding: 18px; }}
    .panel__header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      margin-bottom: 14px;
    }}
    .panel h2 {{ margin-bottom: 0; }}
    .segmented {{
      display: inline-flex;
      flex-wrap: wrap;
      gap: 4px;
      padding: 4px;
      border: 1px solid #dfe7f0;
      border-radius: 8px;
      background: #f7f9fc;
    }}
    .segmented button {{
      min-height: 30px;
      border: 0;
      border-radius: 6px;
      padding: 5px 10px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      font-weight: 720;
    }}
    .segmented button:hover {{ color: #24304a; background: #edf2f8; }}
    .segmented button.active {{
      color: #ffffff;
      background: var(--surface-strong);
      box-shadow: 0 6px 14px rgba(16, 24, 40, 0.16);
    }}
    .top-list {{
      display: grid;
      gap: 13px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .top-list li {{
      display: grid;
      gap: 8px;
      padding: 12px;
      border: 1px solid #e8edf4;
      border-radius: 8px;
      background: #fbfcfe;
    }}
    .top-list div:first-child {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
    }}
    .top-list strong {{
      min-width: 0;
      overflow-wrap: anywhere;
      font-size: 13px;
    }}
    .top-list span {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 860px; background: #fff; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid #edf1f7; text-align: left; font-size: 13px; }}
    th {{ position: sticky; top: 0; background: #f7f9fc; color: #344054; font-weight: 720; }}
    td:nth-child(3), td:nth-child(4) {{ font-variant-numeric: tabular-nums; }}
    tr:last-child td {{ border-bottom: 0; }}
    tr.warning {{ background: var(--amber-soft); }}
    tr.danger {{ background: var(--red-soft); }}
    tr.excluded {{ opacity: .58; }}
    code {{
      color: #18243a;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      overflow-wrap: anywhere;
    }}
    .pill {{
      display: inline-flex;
      min-width: 44px;
      justify-content: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: 700;
    }}
    .pill.healthy {{ background: var(--green-soft); color: var(--green); }}
    .pill.warning {{ background: #ffe8b3; color: var(--amber); }}
    .pill.danger {{ background: #ffd6da; color: var(--red); }}
    .warnings {{ margin-top: 16px; }}
    .warnings ul {{ margin: 0; padding-left: 18px; color: var(--muted); }}
    .empty {{ color: var(--muted); margin: 0; }}
    .is-hidden {{ display: none !important; }}
    @keyframes grow {{
      from {{ transform: scaleX(0); }}
      to {{ transform: scaleX(1); }}
    }}
    @media (max-width: 960px) {{
      main {{ width: min(100% - 28px, 1440px); padding-top: 22px; }}
      header, .content-grid, .score-grid {{ grid-template-columns: 1fr; }}
      .meta {{ min-width: 0; }}
      .score-card__score {{ font-size: 46px; }}
      .panel__header {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>代码质量报告</h1>
        <p class="subtitle">基于有效代码行数评分：后端 300 行以内满分，300 到 600 行线性降至 0 分；前端 600 行以内满分，600 到 1000 行线性降至 0 分。汇总分数按参与评分文件的有效代码行数加权平均。</p>
      </div>
      <div class="meta">
        <span>生成时间</span><strong>{html.escape(generated_at)}</strong>
        <span>项目根目录</span><strong>{html.escape(str(report.root))}</strong>
        <span>测试文件</span><strong>{test_policy}</strong>
      </div>
    </header>

    <section class="score-grid">
      {render_metric_card(report.system)}
      {render_metric_card(report.backend)}
      {render_metric_card(report.frontend)}
    </section>

    <section class="content-grid">
      <section class="panel">
        <div class="panel__header">
          <h2>最大文件排行</h2>
          <div class="segmented" data-filter-group="top">
            <button type="button" class="active" data-filter-value="all">全部</button>
            <button type="button" data-filter-value="backend">后端</button>
            <button type="button" data-filter-value="frontend">前端</button>
          </div>
        </div>
        {render_top_files(report.files)}
      </section>
      <section class="panel">
        <div class="panel__header">
          <h2>文件明细</h2>
          <div class="segmented" data-filter-group="details">
            <button type="button" class="active" data-filter-value="all">全部</button>
            <button type="button" data-filter-value="healthy">健康</button>
            <button type="button" data-filter-value="warning">警告</button>
            <button type="button" data-filter-value="danger">危险</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>文件</th>
                <th>分组</th>
                <th>有效行</th>
                <th>分数</th>
                <th>状态</th>
                <th>范围</th>
              </tr>
            </thead>
            <tbody>{render_file_rows(report.files)}</tbody>
          </table>
        </div>
        <p class="empty is-hidden" data-detail-empty>没有匹配的文件。</p>
      </section>
    </section>

    {render_warnings(report.files)}
  </main>
  <script type="application/json" id="quality-data">{html.escape(report_json)}</script>
  <script>
    const setActiveButton = (button) => {{
      const group = button.closest('[data-filter-group]');
      for (const item of group.querySelectorAll('button')) item.classList.remove('active');
      button.classList.add('active');
    }};

    const applyTopFilter = (value) => {{
      const list = document.querySelector('[data-top-list]');
      const empty = document.querySelector('[data-top-empty]');
      if (!list) return;
      let shown = 0;
      for (const item of list.querySelectorAll('li')) {{
        const matchesGroup = value === 'all' || item.dataset.group === value;
        const visible = matchesGroup && shown < 8;
        item.classList.toggle('is-hidden', !visible);
        if (visible) shown += 1;
      }}
      if (empty) empty.classList.toggle('is-hidden', shown > 0);
    }};

    const applyDetailFilter = (value) => {{
      let shown = 0;
      for (const row of document.querySelectorAll('tbody tr[data-status]')) {{
        const visible = value === 'all' || row.dataset.status === value;
        row.classList.toggle('is-hidden', !visible);
        if (visible) shown += 1;
      }}
      const empty = document.querySelector('[data-detail-empty]');
      if (empty) empty.classList.toggle('is-hidden', shown > 0);
    }};

    for (const button of document.querySelectorAll('[data-filter-group="top"] button')) {{
      button.addEventListener('click', () => {{
        setActiveButton(button);
        applyTopFilter(button.dataset.filterValue);
      }});
    }}

    for (const button of document.querySelectorAll('[data-filter-group="details"] button')) {{
      button.addEventListener('click', () => {{
        setActiveButton(button);
        applyDetailFilter(button.dataset.filterValue);
      }});
    }}

    applyTopFilter('all');
    applyDetailFilter('all');

    for (const el of document.querySelectorAll('[data-score]')) {{
      const raw = el.dataset.score;
      if (raw === 'null') continue;
      const target = Number(raw);
      const duration = 760;
      const startedAt = performance.now();
      const tick = (now) => {{
        const progress = Math.min((now - startedAt) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const value = target * eased;
        el.textContent = Number.isInteger(target) ? String(Math.round(value)) : value.toFixed(1);
        if (progress < 1) requestAnimationFrame(tick);
        else el.textContent = Number.isInteger(target) ? String(target) : target.toFixed(1);
      }};
      requestAnimationFrame(tick);
    }}
  </script>
</body>
</html>
"""


def write_report(report: QualityReport, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(report), encoding="utf-8")
    return output_path
