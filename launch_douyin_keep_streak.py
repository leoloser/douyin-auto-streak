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
ISOLATED_DESKTOP_AUTO_RETURN_TIMEOUT_SEC = 20
ISOLATED_DESKTOP_WINDOW_SCAN_INTERVAL_SEC = 0.8
EDGE_PROFILE_DIR_NAME = "DouyinKeepStreakEdgeProfile"


class IsolatedDesktopManager:
    """Create a temporary virtual desktop for scheduled unattended runs."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.available = False
        self._original_desktop = None
        self._isolated_desktop = None
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._console_hwnd = 0
        self._returned_to_original = False

    def __enter__(self) -> "IsolatedDesktopManager":
        if not self.enabled:
            return self

        try:
            from pyvda import AppView, VirtualDesktop
        except Exception as exc:  # noqa: BLE001
            print(f"未启用虚拟桌面隔离：pyvda 不可用：{exc}")
            return self

        try:
            self._original_desktop = VirtualDesktop.current()
            self._isolated_desktop = VirtualDesktop.create()
            try:
                self._isolated_desktop.rename(ISOLATED_DESKTOP_NAME)
            except Exception:
                pass

            self._console_hwnd = int(ctypes.windll.kernel32.GetConsoleWindow())
            if self._console_hwnd:
                self._move_window_to_desktop(AppView, self._console_hwnd, self._isolated_desktop)

            print(f"已创建临时虚拟桌面：{ISOLATED_DESKTOP_NAME}")
            self._isolated_desktop.go()
            self.available = True
            self._worker = threading.Thread(target=self._watch_and_move_windows, daemon=True)
            self._worker.start()
        except Exception as exc:  # noqa: BLE001
            print(f"虚拟桌面隔离启动失败，改为当前桌面运行：{exc}")
            self._remove_isolated_desktop()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self._stop_event.set()
        if self._worker:
            self._worker.join(timeout=3)
        self._remove_isolated_desktop()

    def _watch_and_move_windows(self) -> None:
        try:
            import comtypes
            from pyvda import AppView, VirtualDesktop

            comtypes.CoInitialize()
            original = VirtualDesktop(desktop_id=self._original_desktop.id)
            isolated = VirtualDesktop(desktop_id=self._isolated_desktop.id)
            edge_seen = False
            start_at = time.time()

            while not self._stop_event.is_set():
                if self._console_hwnd:
                    self._move_window_to_desktop(AppView, self._console_hwnd, isolated)

                for hwnd in self._automation_edge_window_handles():
                    if self._move_window_to_desktop(AppView, hwnd, isolated):
                        edge_seen = True

                if not self._returned_to_original:
                    timed_out = time.time() - start_at >= ISOLATED_DESKTOP_AUTO_RETURN_TIMEOUT_SEC
                    if edge_seen or timed_out:
                        original.go()
                        self._returned_to_original = True
                        if edge_seen:
                            print("自动化 Edge 已移入临时虚拟桌面，已切回原桌面。")
                        else:
                            print("临时虚拟桌面等待超时，已切回原桌面继续后台监控窗口。")

                time.sleep(ISOLATED_DESKTOP_WINDOW_SCAN_INTERVAL_SEC)
        except Exception as exc:  # noqa: BLE001
            print(f"虚拟桌面窗口监控停止：{exc}")
        finally:
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass

    def _remove_isolated_desktop(self) -> None:
        if not self._isolated_desktop or not self._original_desktop:
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
        finally:
            self._isolated_desktop = None

    @staticmethod
    def _move_window_to_desktop(app_view_class, hwnd: int, desktop) -> bool:  # noqa: ANN001
        try:
            app_view_class(hwnd=hwnd).move(desktop)
            return True
        except Exception:
            return False

    def _automation_edge_window_handles(self) -> list[int]:
        edge_pids = self._automation_edge_pids()
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

    @staticmethod
    def _automation_edge_pids() -> set[int]:
        profile_marker = EDGE_PROFILE_DIR_NAME.lower()
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
        pids: set[int] = set()
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            pid_text, command_line = line.split("\t", 1)
            if profile_marker not in command_line.lower():
                continue
            try:
                pids.add(int(pid_text))
            except ValueError:
                continue
        return pids


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行抖音续火花脚本")
    parser.add_argument(
        "--isolated-desktop",
        action="store_true",
        help="新建临时 Windows 虚拟桌面运行，适合任务计划自动触发。",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    isolated_desktop = args.isolated_desktop or os.environ.get(
        "DOUYIN_ISOLATED_DESKTOP", ""
    ).lower() in {"1", "true", "yes"}
    script_path = Path(__file__).with_name("douyin_keep_streak.py")
    exit_code = 0
    with IsolatedDesktopManager(isolated_desktop):
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
