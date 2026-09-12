from __future__ import annotations

import argparse
import os
import runpy
import sys
import time
import traceback
from pathlib import Path


KEEP_WINDOW_OPEN_ON_EXIT = False
KEEP_WINDOW_OPEN_ON_ERROR = True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行抖音续火花脚本")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打开并检查聊天，不发送消息。",
    )
    return parser.parse_args()


def _run_automation_script() -> int:
    script_path = Path(__file__).with_name("douyin_keep_streak.py")
    exit_code = 0
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            exit_code = 0
        else:
            exit_code = int(code) if isinstance(code, int) else 1
    except Exception:
        traceback.print_exc()
        exit_code = 1
    finally:
        if KEEP_WINDOW_OPEN_ON_EXIT or (exit_code != 0 and KEEP_WINDOW_OPEN_ON_ERROR):
            print()
            print("脚本已结束，窗口将在 10 秒后自动关闭...")
            time.sleep(10)
    return exit_code


def main() -> int:
    args = _parse_args()

    if args.dry_run:
        os.environ["DOUYIN_DRY_RUN"] = "1"
    return _run_automation_script()


if __name__ == "__main__":
    sys.exit(main())
