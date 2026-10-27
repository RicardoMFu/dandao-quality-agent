from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    print("旦导风险质检 Agent V1.2")
    print("请依次输入日志文件和时间范围。时间结束点采用左闭右开，例如统计到10月14日，应填10月15日 00:00。\n")

    input_path = Path(ask("问答日志文件路径")).expanduser().resolve()
    if not input_path.exists():
        print(f"文件不存在：{input_path}", file=sys.stderr)
        return 2

    sheet = ask("Excel 工作表名称", "问答记录")
    start_time = ask("开始时间", "2026-09-01 00:00:00")
    end_time = ask("结束时间（不含）", "2026-10-15 00:00:00")
    default_output = project_dir / "outputs" / input_path.stem
    output_dir = Path(ask("输出目录", str(default_output))).expanduser().resolve()

    command = [
        sys.executable,
        str(project_dir / "v1_agent.py"),
        "--input",
        str(input_path),
        "--sheet",
        sheet,
        "--start-time",
        start_time,
        "--end-time",
        end_time,
        "--output-dir",
        str(output_dir),
    ]
    print("\n开始扫描，请稍候……")
    completed = subprocess.run(command, cwd=project_dir, check=False)
    if completed.returncode == 0:
        print(f"\n扫描完成。结果目录：{output_dir}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
