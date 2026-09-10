from __future__ import annotations

import argparse
import ctypes
import os
import runpy
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path


KEEP_WINDOW_OPEN_ON_EXIT = False
KEEP_WINDOW_OPEN_ON_ERROR = True
ISOLATED_DESKTOP_NAME = "抖音续火花"
ISOLATED_DESKTOP_PROCESS_WINDOW_WAIT_SEC = 3
ISOLATED_DESKTOP_WINDOW_SCAN_INTERVAL_SEC = 0.5
EDGE_PROFILE_DIR_NAME = "DouyinKeepStreakEdgeProfile"
DESKTOP_MODE_CURRENT = "current"
DESKTOP_MODE_ISOLATED = "isolated"
ENV_DESKTOP_WORKER = "DOUYIN_DESKTOP_WORKER"
ENV_ORIGINAL_DESKTOP_ID = "DOUYIN_ORIGINAL_DESKTOP_ID"
ENV_ISOLATED_DESKTOP_ID = "DOUYIN_ISOLATED_DESKTOP_ID"


class IsolatedDesktopCleanup:
    """Restore the original desktop and remove the temporary one on exit."""

    def __init__(self, original_desktop_id: str, isolated_desktop_id: str) -> None:
        self._original_desktop_id = original_desktop_id
        self._isolated_desktop_id = isolated_desktop_id
        self._original_desktop = None
        self._isolated_desktop = None
        self._console_hwnd = 0
        self._moved_edge_hwnds: set[int] = set()

    def __enter__(self) -> "IsolatedDesktopCleanup":
        from comtypes import GUID
        from pyvda import VirtualDesktop

        self._original_desktop = VirtualDesktop(desktop_id=GUID(self._original_desktop_id))
        self._isolated_desktop = VirtualDesktop(desktop_id=GUID(self._isolated_desktop_id))
        self._console_hwnd = int(ctypes.windll.kernel32.GetConsoleWindow())
        self.keep_automation_windows_on_isolated_desktop()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if not self._original_desktop or not self._isolated_desktop:
            return
        try:
            self._original_desktop.go()
        except Exception:
            pass
        try:
            self._isolated_desktop.remove(fallback=self._original_desktop)
            print(f"已关闭临时虚拟桌面：{ISOLATED_DESKTOP_NAME}")
        except Exception as exc:  # noqa: BLE001
            print(f"关闭临时虚拟桌面失败，请稍后用任务视图手动关闭：{exc}")

    def keep_automation_windows_on_isolated_desktop(self) -> None:
        if not self._isolated_desktop:
            return

        from pyvda import AppView

        if self._console_hwnd:
            self._move_window(AppView, self._console_hwnd)

        current_edge_hwnds = set(_automation_edge_window_handles())
        self._moved_edge_hwnds.intersection_update(current_edge_hwnds)
        for hwnd in current_edge_hwnds:
            if not self._move_window(AppView, hwnd):
                continue
            if hwnd not in self._moved_edge_hwnds:
                print("自动化 Edge 已移入临时虚拟桌面。")
                self._moved_edge_hwnds.add(hwnd)

    def _move_window(self, app_view_class, hwnd: int) -> bool:  # noqa: ANN001
        try:
            view = app_view_class(hwnd=hwnd)
            if not view.is_on_desktop(self._isolated_desktop, include_pinned=False):
                view.move(self._isolated_desktop)
            return True
        except Exception:
            return False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行抖音续火花脚本")
    parser.add_argument(
        "--desktop-mode",
        choices=[DESKTOP_MODE_CURRENT, DESKTOP_MODE_ISOLATED],
        help="current 表示在当前桌面运行，isolated 表示先开临时桌面再在那里启动脚本。",
    )
    parser.add_argument(
        "--isolated-desktop",
        action="store_true",
        help="兼容旧参数：等同于 --desktop-mode isolated。",
    )
    parser.add_argument(
        "--desktop-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打开并检查聊天，不发送消息。",
    )
    return parser.parse_args()


def _resolve_desktop_mode(args: argparse.Namespace) -> str:
    if args.desktop_mode:
        return args.desktop_mode

    env_mode = os.environ.get("DOUYIN_DESKTOP_MODE", "").strip().lower()
    if env_mode in {DESKTOP_MODE_CURRENT, DESKTOP_MODE_ISOLATED}:
        return env_mode

    if args.isolated_desktop:
        return DESKTOP_MODE_ISOLATED

    if os.environ.get("DOUYIN_ISOLATED_DESKTOP", "").lower() in {"1", "true", "yes"}:
        return DESKTOP_MODE_ISOLATED

    return DESKTOP_MODE_CURRENT


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


def _worker_desktop_cleanup() -> IsolatedDesktopCleanup:
    original_id = os.environ.get(ENV_ORIGINAL_DESKTOP_ID, "").strip()
    isolated_id = os.environ.get(ENV_ISOLATED_DESKTOP_ID, "").strip()
    if not original_id or not isolated_id:
        raise RuntimeError("缺少隔离桌面标识，无法执行清理")
    return IsolatedDesktopCleanup(original_id, isolated_id)


def _run_automation_with_desktop_monitor(
    desktop_cleanup: IsolatedDesktopCleanup,
) -> int:
    result: list[int] = []

    def run_script() -> None:
        result.append(_run_automation_script())

    automation_thread = threading.Thread(
        target=run_script,
        name="douyin-automation",
    )
    automation_thread.start()
    while automation_thread.is_alive():
        desktop_cleanup.keep_automation_windows_on_isolated_desktop()
        automation_thread.join(timeout=ISOLATED_DESKTOP_WINDOW_SCAN_INTERVAL_SEC)
    desktop_cleanup.keep_automation_windows_on_isolated_desktop()
    return result[0] if result else 1


def _spawn_worker_in_isolated_desktop() -> int:
    try:
        from pyvda import AppView, VirtualDesktop
    except Exception as exc:  # noqa: BLE001
        print(f"未启用虚拟桌面隔离：pyvda 不可用：{exc}")
        return 1

    original_desktop = VirtualDesktop.current()
    isolated_desktop = VirtualDesktop.create()
    try:
        try:
            isolated_desktop.rename(ISOLATED_DESKTOP_NAME)
        except Exception:
            pass

        print(f"已创建临时虚拟桌面：{ISOLATED_DESKTOP_NAME}")
        isolated_desktop.go()

        env = os.environ.copy()
        env[ENV_DESKTOP_WORKER] = "1"
        env[ENV_ORIGINAL_DESKTOP_ID] = str(original_desktop.id)
        env[ENV_ISOLATED_DESKTOP_ID] = str(isolated_desktop.id)

        existing_windows = _visible_window_handles()
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--desktop-worker",
            ],
            env=env,
            creationflags=creationflags,
        )
        _wait_for_new_window_on_desktop(existing_windows, AppView, isolated_desktop)
        original_desktop.go()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"虚拟桌面隔离启动失败，已停止本次自动运行：{exc}")
        try:
            original_desktop.go()
        except Exception:
            pass
        try:
            isolated_desktop.remove(fallback=original_desktop)
        except Exception:
            pass
        return 1


def _wait_for_new_window_on_desktop(
    existing_windows: set[int],
    app_view_class,  # noqa: ANN001
    desktop,  # noqa: ANN001
) -> None:
    end_at = time.time() + ISOLATED_DESKTOP_PROCESS_WINDOW_WAIT_SEC
    while time.time() < end_at:
        new_windows = _visible_window_handles() - existing_windows
        if new_windows:
            hwnd = next(iter(new_windows))
            try:
                app_view_class(hwnd=hwnd).move(desktop)
            except Exception:
                pass
            return
        time.sleep(0.1)


def _visible_window_handles() -> set[int]:
    user32 = ctypes.windll.user32
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    handles: set[int] = set()

    def callback(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            handles.add(int(hwnd))
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return handles


def _automation_edge_pids() -> set[int]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'msedge.exe' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    profile_marker = EDGE_PROFILE_DIR_NAME.casefold()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        pid_text, command_line = line.split("\t", 1)
        if profile_marker not in command_line.casefold():
            continue
        try:
            pids.add(int(pid_text))
        except ValueError:
            continue
    return pids


def _automation_edge_window_handles() -> list[int]:
    edge_pids = _automation_edge_pids()
    if not edge_pids:
        return []

    handles: list[int] = []
    user32 = ctypes.windll.user32
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in edge_pids:
            handles.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return handles


def main() -> int:
    args = _parse_args()

    if args.dry_run:
        os.environ["DOUYIN_DRY_RUN"] = "1"

    if args.desktop_worker:
        with _worker_desktop_cleanup() as desktop_cleanup:
            return _run_automation_with_desktop_monitor(desktop_cleanup)

    desktop_mode = _resolve_desktop_mode(args)
    if desktop_mode == DESKTOP_MODE_ISOLATED:
        exit_code = _spawn_worker_in_isolated_desktop()
        return exit_code

    return _run_automation_script()


if __name__ == "__main__":
    sys.exit(main())
