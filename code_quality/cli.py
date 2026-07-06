"""Typer CLI for generating code quality reports."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from code_quality.report import build_report, format_score, write_report


app = typer.Typer(
    add_completion=False,
    help="生成代码质量 HTML 报告。",
)


@app.command()
def main(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            "-r",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="项目根目录；默认扫描当前工作目录。",
        ),
    ] = Path.cwd(),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            file_okay=True,
            dir_okay=False,
            writable=True,
            resolve_path=True,
            help="HTML 报告输出路径；默认写入 <root>/tmp/code_quality_report.html。",
        ),
    ] = None,
    include_tests: Annotated[
        bool,
        typer.Option(
            "--include-tests",
            help="让测试文件参与评分；默认仅展示测试文件但不参与平均分。",
        ),
    ] = False,
) -> None:
    output_path = output or root / "tmp" / "code_quality_report.html"
    report = build_report(root, include_tests=include_tests)
    written_path = write_report(report, output_path)

    typer.echo(f"代码质量报告已生成: {written_path}")
    typer.echo(
        f"系统分数: {format_score(report.system.score)} | "
        f"后端: {format_score(report.backend.score)} | "
        f"前端: {format_score(report.frontend.score)}"
    )


if __name__ == "__main__":
    app()
