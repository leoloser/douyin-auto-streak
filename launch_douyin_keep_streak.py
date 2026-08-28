from __future__ import annotations

import runpy
import sys
import time
import traceback
from pathlib import Path


KEEP_WINDOW_OPEN_ON_EXIT = False
KEEP_WINDOW_OPEN_ON_ERROR = True


def main() -> int:
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


if __name__ == "__main__":
    sys.exit(main())
