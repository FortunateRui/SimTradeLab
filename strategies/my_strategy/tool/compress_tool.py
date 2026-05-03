"""
压缩工具

用法：
python compress_tool.py

参数：
- target_name: 目标文件夹相对路径

返回：
- 压缩包
"""

#!/usr/bin/env python3
from pathlib import Path
import tarfile

target_name = "2021-01-01"


def main():
    # 当前 .py 文件所在目录；在部分 Notebook 环境中没有 __file__
    try:
        script_dir = Path(__file__).resolve().parent
    except NameError:
        script_dir = Path.cwd()

    target_dir = script_dir / target_name
    output_file = script_dir / f"{target_name}.tar.gz"

    if not target_dir.is_dir():
        print(f"错误：未找到文件夹：{target_dir}")
        return

    if output_file.exists():
        print(f"错误：压缩包已存在，避免覆盖：{output_file}")
        return

    with tarfile.open(output_file, "w:gz") as tar:
        tar.add(target_dir, arcname=target_dir.name)

    print(f"压缩完成：{output_file}")


if __name__ == "__main__":
    main()