"""
Douyin web streak helper for Microsoft Edge.

What this script does:
- opens the Douyin web private-message page
- searches only the configured friend list
- opens each chat
- skips chats that already have today's activity
- sends one short message
- waits between actions to avoid bursty automation

Important:
- this script does not handle login
- it expects a logged-in Edge profile / session to already exist
- third-party web automation can trigger rate limits, account review, or bans
"""

from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from urllib.error import URLError
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# =========================
# 配置区
# =========================

# 需要续火花的好友昵称。
# 公开仓库里不放你的真实好友列表；请在本地自行填写。
TARGET_FRIENDS = []

# 发送的内容：默认只发一个“1”，用于维持聊天火花。
MESSAGE_TO_SEND = "1"

# 打开聊天框后，如果检测到今天已经有聊天消息/视频活动，则跳过发送。
# 说明：抖音网页端没有稳定公开接口，这里按聊天区域里的日期/时间文字做启发式判断。
SKIP_IF_TODAY_ALREADY_ACTIVE = True

# 自测开关：命令行临时设置 DOUYIN_DRY_RUN=1 时，只打开聊天窗口，不真正发送。
DRY_RUN_OPEN_CHAT_ONLY = os.environ.get("DOUYIN_DRY_RUN", "").lower() in {"1", "true", "yes"}

# 打开抖音网页版聊天页。/message 会显示 404，真实聊天入口是 /chat?isPopup=1
DOUYIN_MESSAGE_URL = "https://www.douyin.com/chat?isPopup=1"

# 自动化专用 Edge 用户数据目录。
# 你这台机器的默认 Edge 用户目录无法稳定开放 WebDriver/9222 控制端口；
# 因此脚本使用独立目录保证可控。首次使用只需在该窗口登录一次抖音。
EDGE_USER_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\Public\AppData\Local"),
    "DouyinKeepStreakEdgeProfile",
)

# 常见配置文件名：Default / Profile 1 / Profile 2 ...
EDGE_PROFILE_DIRECTORY = "Default"

# 脚本通过这个端口接管 Edge。普通 Edge 窗口无法半路接管，需要用远程调试端口启动。
REMOTE_DEBUGGING_PORT: Optional[int] = 9222

# 如果端口未开启，脚本会尝试自动启动一个带远程调试端口的 Edge。
AUTO_START_EDGE_WITH_DEBUGGING = True

# 每次运行前清理上一次残留的自动化专用 Edge，避免接管旧窗口/旧标签页。
# 只匹配 EDGE_USER_DATA_DIR，不会关闭你平时手动打开的普通 Edge。
CLEAN_STALE_AUTOMATION_EDGE_ON_START = True

# 使用专用用户数据目录后，通常不需要关闭你平时用的 Edge。
AUTO_CLOSE_EDGE_TO_ENABLE_DEBUGGING = False

# Microsoft Edge 程序路径，通常不用改。
EDGE_EXE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 运行结束后是否关闭浏览器。
# 只关闭“本次脚本自己启动”的自动化 Edge，不关闭你手动打开的其他 Edge。
CLOSE_STARTED_EDGE_ON_EXIT = True

# 自动执行时间，格式 HH:MM。设为 None 表示双击后立即执行一次。
DAILY_RUN_AT: Optional[str] = None

# 操作节奏
OPEN_PAGE_WAIT_SEC = 4
AFTER_SEARCH_WAIT_SEC = 2
CHAT_OPEN_WAIT_SEC = 6
MESSAGE_BOX_WAIT_SEC = 25
BETWEEN_FRIENDS_MIN_SEC = 3
BETWEEN_FRIENDS_MAX_SEC = 6
AFTER_SEND_WAIT_SEC = 1.5
PAGE_LOAD_TIMEOUT_SEC = 20
EDGE_DEBUG_START_WAIT_SEC = 8
EDGE_RESTART_WAIT_SEC = 3
LOGIN_WAIT_TIMEOUT_SEC = 300
MESSAGE_BUTTON_VERIFY_WAIT_SEC = 6

# 点击失败时保存页面截图，方便看清脚本当时点到了哪里。
SAVE_SCREENSHOT_ON_FAILURE = True

# 页面上的候选元素选择器。Douyin 前端会变，保留成可调配置更稳妥。
SEARCH_INPUT_SELECTORS = [
    'input[placeholder*="搜索"]',
    'input[placeholder*="search"]',
    'input[type="search"]',
]

MESSAGE_BOX_SELECTORS = [
    '[placeholder*="发送消息"]',
    '[placeholder*="发消息"]',
    '[aria-label*="发送消息"]',
    '[aria-label*="发消息"]',
    'textarea',
    '[contenteditable="true"]',
]

SEND_BUTTON_SELECTORS = [
    'button',
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("douyin-keep-streak")

STARTED_EDGE_PID: Optional[int] = None
STARTED_EDGE_PROFILE_PIDS: set[int] = set()


@dataclass
class TargetResult:
    friend: str
    ok: bool
    detail: str


@dataclass
class TargetFriend:
    search: str
    confirm: str


def _normalize_target(entry: Any) -> TargetFriend:
    if isinstance(entry, dict):
        search = str(entry.get("search", "")).strip()
        confirm = str(entry.get("confirm") or search).strip()
    else:
        search = str(entry).strip()
        confirm = search
    if not search or not confirm:
        raise ValueError(f"好友配置无效：{entry!r}")
    return TargetFriend(search=search, confirm=confirm)


def _sleep_random(min_sec: float, max_sec: float) -> None:
    delay = min_sec if max_sec <= min_sec else random.uniform(min_sec, max_sec)
    time.sleep(delay)


def _xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def _debugger_is_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as resp:
            return resp.status == 200
    except (OSError, URLError):
        return False


def _find_edge_exe() -> str:
    for candidate in EDGE_EXE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("未找到 msedge.exe，请检查 EDGE_EXE_CANDIDATES 配置")


def _edge_processes_are_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="ignore",
        check=False,
    )
    return "msedge.exe" in result.stdout.lower()


def _edge_command_lines() -> list[str]:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -eq 'msedge.exe' } | "
                "ForEach-Object { $_.CommandLine }"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _automation_edge_pids() -> set[int]:
    expected_profile = os.path.normcase(os.path.normpath(EDGE_USER_DATA_DIR)).lower()
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -eq 'msedge.exe' } | "
                "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
            ),
        ],
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
        normalized = os.path.normcase(os.path.normpath(command_line.replace("/", "\\"))).lower()
        if expected_profile not in normalized:
            continue
        try:
            pids.add(int(pid_text))
        except ValueError:
            continue
    return pids


def _debugger_uses_expected_profile(port: int) -> bool:
    expected = os.path.normcase(os.path.normpath(EDGE_USER_DATA_DIR)).lower()
    for line in _edge_command_lines():
        normalized = os.path.normcase(os.path.normpath(line.replace("/", "\\"))).lower()
        if f"remote-debugging-port={port}" in normalized and expected in normalized:
            return True
    return False


def _close_edge_processes() -> None:
    if not _edge_processes_are_running():
        return
    log.warning("检测到普通 Edge 进程正在运行，会阻止 9222 调试端口生效。")
    log.warning("准备关闭所有 Edge 窗口并重新启动可接管的 Edge。请确认重要网页内容已保存。")
    time.sleep(2)

    subprocess.run(
        ["taskkill", "/IM", "msedge.exe", "/T"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    time.sleep(EDGE_RESTART_WAIT_SEC)

    if _edge_processes_are_running():
        subprocess.run(
            ["taskkill", "/F", "/IM", "msedge.exe", "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(EDGE_RESTART_WAIT_SEC)


def _process_command_line(pid: int) -> str:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    return result.stdout.strip()


def _started_edge_pid_looks_safe(pid: int) -> bool:
    command_line = _process_command_line(pid)
    if not command_line:
        return False
    normalized = os.path.normcase(os.path.normpath(command_line.replace("/", "\\"))).lower()
    expected_profile = os.path.normcase(os.path.normpath(EDGE_USER_DATA_DIR)).lower()
    return "msedge.exe" in normalized and expected_profile in normalized


def _close_started_edge_process() -> None:
    if not CLOSE_STARTED_EDGE_ON_EXIT:
        return

    candidate_pids = set(STARTED_EDGE_PROFILE_PIDS)
    if STARTED_EDGE_PID:
        candidate_pids.add(STARTED_EDGE_PID)
    if not candidate_pids:
        return

    closed_any = False
    for pid in sorted(candidate_pids):
        if not _started_edge_pid_looks_safe(pid):
            if _process_command_line(pid):
                log.warning(
                    "跳过关闭 Edge：PID %s 已不是本次自动化专用 Edge，避免误关其他窗口。",
                    pid,
                )
            continue
        log.info("关闭本次脚本启动的自动化 Edge：PID %s", pid)
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        closed_any = True

    if not closed_any:
        log.info("没有需要关闭的本次自动化 Edge 进程。")
        return

    time.sleep(2)
    remaining_pids = _automation_edge_pids().intersection(candidate_pids)
    for pid in sorted(remaining_pids):
        if not _started_edge_pid_looks_safe(pid):
            continue
        log.info("强制关闭残留的本次自动化 Edge：PID %s", pid)
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _close_all_automation_edge_processes(reason: str) -> None:
    pids = _automation_edge_pids()
    if not pids:
        return
    log.info("%s，关闭旧的自动化 Edge 进程：%s", reason, sorted(pids))
    for pid in sorted(pids):
        if not _started_edge_pid_looks_safe(pid):
            continue
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    end_at = time.time() + 8
    while time.time() < end_at:
        if not _automation_edge_pids():
            return
        time.sleep(0.5)
    remaining = sorted(_automation_edge_pids())
    if remaining:
        log.warning("仍有自动化 Edge 进程未退出：%s", remaining)


def _close_started_edge_browser(driver: webdriver.Edge) -> None:
    if not CLOSE_STARTED_EDGE_ON_EXIT or not STARTED_EDGE_PID:
        return
    try:
        log.info("请求关闭本次脚本启动的自动化 Edge 浏览器窗口")
        driver.execute_cdp_cmd("Browser.close", {})
        time.sleep(2)
    except Exception as exc:  # noqa: BLE001
        log.warning("通过浏览器接口关闭 Edge 失败，改用进程兜底：%s", exc)


def _start_edge_with_debugging(port: int) -> None:
    global STARTED_EDGE_PID, STARTED_EDGE_PROFILE_PIDS

    edge_exe = _find_edge_exe()
    before_pids = _automation_edge_pids()
    log.info("未检测到可接管的 Edge，正在启动远程调试 Edge：127.0.0.1:%s", port)
    process = subprocess.Popen(
        [
            edge_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={EDGE_USER_DATA_DIR}",
            f"--profile-directory={EDGE_PROFILE_DIRECTORY}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-notifications",
            "--disable-features=msEdgeStartupBoost",
            "--disable-background-mode",
            "--no-session-restore",
            "--new-window",
            DOUYIN_MESSAGE_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    STARTED_EDGE_PID = process.pid
    log.info("本次脚本启动的 Edge PID：%s", STARTED_EDGE_PID)
    end_at = time.time() + EDGE_DEBUG_START_WAIT_SEC
    while time.time() < end_at:
        after_pids = _automation_edge_pids()
        STARTED_EDGE_PROFILE_PIDS.update(after_pids - before_pids)
        if _debugger_is_ready(port):
            return
        time.sleep(0.5)


def _build_driver() -> webdriver.Edge:
    global STARTED_EDGE_PID, STARTED_EDGE_PROFILE_PIDS

    STARTED_EDGE_PID = None
    STARTED_EDGE_PROFILE_PIDS = set()

    if CLEAN_STALE_AUTOMATION_EDGE_ON_START:
        _close_all_automation_edge_processes("任务开始前检测到程序自己打开的旧 Edge")

    if REMOTE_DEBUGGING_PORT:
        if _debugger_is_ready(REMOTE_DEBUGGING_PORT) and not _debugger_uses_expected_profile(
            REMOTE_DEBUGGING_PORT
        ):
            log.warning("9222 端口上已有 Edge，但不是当前配置的用户数据目录。")
            if AUTO_CLOSE_EDGE_TO_ENABLE_DEBUGGING:
                _close_edge_processes()
            else:
                raise RuntimeError("9222 端口被其他 Edge 配置占用，请关闭该 Edge 后重试。")
        if not _debugger_is_ready(REMOTE_DEBUGGING_PORT) and AUTO_START_EDGE_WITH_DEBUGGING:
            _start_edge_with_debugging(REMOTE_DEBUGGING_PORT)
        if (
            not _debugger_is_ready(REMOTE_DEBUGGING_PORT)
            and AUTO_CLOSE_EDGE_TO_ENABLE_DEBUGGING
        ):
            _close_edge_processes()
            _start_edge_with_debugging(REMOTE_DEBUGGING_PORT)
        if not _debugger_is_ready(REMOTE_DEBUGGING_PORT):
            raise RuntimeError(
                "无法接管 Edge：127.0.0.1:9222 未开启。请确认没有其他程序占用 9222 端口，"
                "然后重新双击桌面快捷方式。"
            )
        options = EdgeOptions()
        options.add_experimental_option(
            "debuggerAddress", f"127.0.0.1:{REMOTE_DEBUGGING_PORT}"
        )
    else:
        options = EdgeOptions()
        options.add_argument(f"--user-data-dir={EDGE_USER_DATA_DIR}")
        options.add_argument(f"--profile-directory={EDGE_PROFILE_DIRECTORY}")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")

    try:
        driver = webdriver.Edge(options=options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SEC)
        _prepare_single_chat_tab(driver)
        return driver
    except WebDriverException as exc:
        raise RuntimeError(
            "无法启动或接管 Edge。若要真正复用已打开实例，请先用远程调试端口启动 Edge，"
            "或确认 Edge 用户数据目录未被其他实例独占。"
        ) from exc


def _prepare_single_chat_tab(driver: webdriver.Edge) -> None:
    try:
        handles = list(driver.window_handles)
    except WebDriverException:
        return
    if not handles:
        return

    chosen_handle = None
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            if "douyin.com/chat" in (driver.current_url or ""):
                chosen_handle = handle
                break
        except WebDriverException:
            continue

    if not chosen_handle:
        chosen_handle = handles[-1]
        try:
            driver.switch_to.window(chosen_handle)
        except WebDriverException:
            return

    for handle in handles:
        if handle == chosen_handle:
            continue
        try:
            driver.switch_to.window(handle)
            if "douyin.com/chat" in (driver.current_url or "") or "douyin.com" in (driver.current_url or ""):
                driver.close()
        except WebDriverException:
            continue

    try:
        driver.switch_to.window(chosen_handle)
        driver.get(DOUYIN_MESSAGE_URL)
    except WebDriverException:
        pass


def _wait_for_any(driver: webdriver.Edge, selectors: Iterable[str], timeout: int):
    last_exc: Optional[Exception] = None
    for selector in selectors:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if last_exc:
        raise TimeoutException(str(last_exc))
    raise TimeoutException("no selector matched")


def _find_visible_elements(driver: webdriver.Edge, selectors: Iterable[str]):
    found = []
    for selector in selectors:
        try:
            found.extend(driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:  # noqa: BLE001
            continue
    return [el for el in found if el.is_displayed()]


def _find_message_box(driver: webdriver.Edge):
    boxes = _find_visible_elements(driver, MESSAGE_BOX_SELECTORS)
    viewport_height = driver.execute_script("return window.innerHeight")
    bottom_boxes = []
    for box in boxes:
        rect = box.rect
        center_y = rect["y"] + rect["height"] / 2
        if center_y > viewport_height * 0.65:
            bottom_boxes.append((center_y, box))
    if not bottom_boxes:
        return None
    bottom_boxes.sort(key=lambda item: item[0], reverse=True)
    return bottom_boxes[0][1]


def _wait_for_message_box(driver: webdriver.Edge, timeout: float = MESSAGE_BOX_WAIT_SEC):
    end_at = time.time() + timeout
    while time.time() < end_at:
        message_box = _find_message_box(driver)
        if message_box:
            return message_box
        time.sleep(1)
    return None


def _right_panel_looks_open(driver: webdriver.Edge) -> bool:
    selectors = [
        '[placeholder*="发送消息"]',
        '[placeholder*="发消息"]',
        '[aria-label*="发送消息"]',
        '[aria-label*="发消息"]',
        'textarea',
    ]
    try:
        for selector in selectors:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _save_failure_screenshot(driver: webdriver.Edge, target_name: str, reason: str) -> None:
    if not SAVE_SCREENSHOT_ON_FAILURE:
        return
    try:
        safe_name = "".join(ch if ch.isalnum() else "_" for ch in target_name).strip("_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.abspath(f"douyin_failure_{safe_name}_{timestamp}.png")
        driver.save_screenshot(path)
        log.warning("%s，已保存截图：%s", reason, path)
    except Exception:  # noqa: BLE001
        log.warning("%s，但保存截图失败", reason)


def _log_message_button_candidates(driver: webdriver.Edge, target_name: str) -> None:
    try:
        candidates = driver.execute_script(
            """
            const target = (arguments[0] || '').replace(/\\s+/g, '').trim();
            const norm = (text) => (text || '').replace(/\\s+/g, '').trim();
            const visible = (el) => {
              if (!el) return false;
              const style = getComputedStyle(el);
              const rect = el.getBoundingClientRect();
              return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            return Array.from(document.querySelectorAll('button,[role=\"button\"],a,div,span'))
              .filter((el) => visible(el) && norm(el.innerText || el.textContent || '').includes('发消息'))
              .slice(0, 8)
              .map((el) => {
                const rect = el.getBoundingClientRect();
                let rowText = '';
                let cur = el;
                for (let i = 0; cur && i < 7; i += 1, cur = cur.parentElement) {
                  const text = norm(cur.innerText || cur.textContent || '');
                  if (!rowText || (target && text.includes(target) && text.length < 80)) rowText = text;
                }
                return {
                  tag: el.tagName,
                  text: (el.innerText || el.textContent || '').trim().slice(0, 50),
                  x: Math.round(rect.left),
                  y: Math.round(rect.top),
                  w: Math.round(rect.width),
                  h: Math.round(rect.height),
                  cursor: getComputedStyle(el).cursor,
                  rowText: rowText.slice(0, 80),
                };
              });
            """,
            target_name,
        )
        if candidates:
            log.warning("页面上的“发消息”候选：%s", candidates)
        else:
            log.warning("页面上没有检测到可见的“发消息”候选")
    except Exception:  # noqa: BLE001
        log.warning("读取“发消息”候选失败")


def _find_clickable_text(driver: webdriver.Edge, text: str):
    candidates = []
    xpath = (
        "//*[self::div or self::span or self::a or self::button or self::li]"
        f"[contains(normalize-space(.), {_xpath_literal(text)})]"
    )
    candidates.extend(driver.find_elements(By.XPATH, xpath))
    for el in candidates:
        if el.is_displayed():
            return el
    return None


def _click_cancel_search(driver: webdriver.Edge) -> bool:
    button = _find_clickable_text(driver, "取消")
    if not button:
        return False
    try:
        return _click_element_by_cdp(driver, button, "取消搜索")
    except WebDriverException:
        return _click_human_like(driver, button)


def _click_human_like(driver: webdriver.Edge, element) -> bool:
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            element,
        )
        ActionChains(driver).move_to_element(element).pause(0.15).click().perform()
        return True
    except WebDriverException:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except WebDriverException:
            return False


def _click_viewport_point(driver: webdriver.Edge, x: float, y: float) -> bool:
    """Use Chrome DevTools Protocol to send a real mouse click at viewport coordinates."""
    try:
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": x,
                "y": y,
                "button": "none",
            },
        )
        time.sleep(random.uniform(0.08, 0.18))
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
        )
        time.sleep(random.uniform(0.05, 0.12))
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
        )
        return True
    except WebDriverException:
        return False


def _element_hitbox(driver: webdriver.Edge, element):
    return driver.execute_script(
        """
        const el = arguments[0];
        if (!el) return null;
        el.scrollIntoView({block: 'center', inline: 'center'});
        const rect = el.getBoundingClientRect();
        const padX = Math.min(Math.max(rect.width * 0.18, 6), 16);
        const padY = Math.min(Math.max(rect.height * 0.22, 4), 10);
        const x = Math.min(Math.max(rect.left + rect.width / 2, rect.left + padX), rect.right - padX);
        const y = Math.min(Math.max(rect.top + rect.height / 2, rect.top + padY), rect.bottom - padY);
        const atPoint = document.elementFromPoint(x, y);
        return {
          x,
          y,
          left: rect.left,
          top: rect.top,
          width: rect.width,
          height: rect.height,
          tag: el.tagName,
          text: (el.innerText || el.textContent || '').trim().slice(0, 80),
          pointTag: atPoint ? atPoint.tagName : '',
          pointText: atPoint ? ((atPoint.innerText || atPoint.textContent || '').trim().slice(0, 80)) : '',
        };
        """,
        element,
    )


def _click_element_by_cdp(driver: webdriver.Edge, element, label: str) -> bool:
    info = _element_hitbox(driver, element)
    if not info:
        return False

    # Keep the point away from exact borders; Douyin's button often has nested spans.
    x = float(info["x"]) + random.uniform(-2.0, 2.0)
    y = float(info["y"]) + random.uniform(-1.5, 1.5)
    log.info(
        "点击候选：%s，坐标=(%.1f, %.1f)，元素=%s，文本=%r，命中点=%s/%r",
        label,
        x,
        y,
        info.get("tag"),
        info.get("text"),
        info.get("pointTag"),
        info.get("pointText"),
    )
    if _click_viewport_point(driver, x, y):
        return True
    return _click_human_like(driver, element)


def _find_message_button(driver: webdriver.Edge, target_name: str):
    script = """
    const target = (arguments[0] || '').replace(/\\s+/g, '').trim();
    const keywords = ['发消息', '发送消息'];
    const visible = (el) => {
      if (!el) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const norm = (text) => (text || '').replace(/\\s+/g, '').trim();
    const buttonTextLooksRight = (text) => keywords.some((k) => text === k || text.includes(k));
    const targetRects = target
      ? Array.from(document.querySelectorAll('div,span,a,button'))
          .filter((el) => visible(el) && norm(el.innerText || el.textContent || '').includes(target))
          .map((el) => {
            const rect = el.getBoundingClientRect();
            return {
              left: rect.left,
              right: rect.right,
              top: rect.top,
              bottom: rect.bottom,
              cx: rect.left + rect.width / 2,
              cy: rect.top + rect.height / 2,
              width: rect.width,
              height: rect.height,
            };
          })
          .filter((rect) => rect.left < window.innerWidth * 0.75 && rect.height >= 12 && rect.height <= 160)
      : [];
    const clickableAncestor = (el) => {
      let best = el;
      let cur = el;
      for (let i = 0; cur && i < 8; i += 1, cur = cur.parentElement) {
        if (!visible(cur)) continue;
        const style = getComputedStyle(cur);
        const rect = cur.getBoundingClientRect();
        const text = norm(cur.innerText || cur.textContent || '');
        const looksClickable =
          cur.matches('button,[role=\"button\"],a') ||
          style.cursor === 'pointer' ||
          cur.onclick ||
          cur.getAttribute('tabindex') !== null;
        const buttonSized = rect.width >= 42 && rect.width <= 140 && rect.height >= 22 && rect.height <= 60;
        if ((looksClickable || buttonSized) && buttonTextLooksRight(text)) {
          best = cur;
        }
      }
      return best;
    };
    const rowContainsTarget = (el) => {
      if (!target) return true;
      let cur = el;
      for (let i = 0; cur && i < 9; i += 1, cur = cur.parentElement) {
        const rect = cur.getBoundingClientRect();
        const text = norm(cur.innerText || cur.textContent || '');
        if (text.includes(target) && rect.height <= 180 && rect.width >= 120) return true;
      }
      return false;
    };
    const sameVisualRowAsTarget = (rect) => {
      if (!targetRects.length) return false;
      const cy = rect.top + rect.height / 2;
      return targetRects.some((targetRect) => {
        const verticalGap = Math.abs(cy - targetRect.cy);
        const rightOfName = rect.left >= targetRect.left;
        const closeEnough = rect.left - targetRect.left < 560;
        return verticalGap <= Math.max(38, targetRect.height * 0.65) && rightOfName && closeEnough;
      });
    };
    const nodes = Array.from(document.querySelectorAll('button,[role=\"button\"],a,div,span'));
    let bestEl = null;
    let bestScore = -1;
    for (const node of nodes) {
      if (!visible(node)) continue;
      const text = norm(node.innerText || node.textContent || '');
      if (!buttonTextLooksRight(text)) continue;
      const el = clickableAncestor(node);
      if (!visible(el)) continue;
      const rect = el.getBoundingClientRect();
      const atPoint = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      let score = 0;
      if (rowContainsTarget(el)) score += 100;
      if (sameVisualRowAsTarget(rect)) score += 90;
      if (el.matches('button,[role=\"button\"],a')) score += 35;
      if (getComputedStyle(el).cursor === 'pointer') score += 25;
      if (text === '发消息' || text === '发送消息') score += 20;
      if (rect.width >= 48 && rect.width <= 120 && rect.height >= 24 && rect.height <= 50) score += 20;
      if (rect.left > 0 && rect.left < window.innerWidth * 0.82) score += 10;
      if (atPoint && (el === atPoint || el.contains(atPoint) || atPoint.contains(el))) score += 10;
      if (score > bestScore) {
        bestScore = score;
        bestEl = el;
      }
    }
    return bestEl;
    """
    return driver.execute_script(script, target_name)


def _click_message_button_for_target(driver: webdriver.Edge, target_name: str) -> bool:
    button = _find_message_button(driver, target_name)
    if button:
        return _click_element_by_cdp(driver, button, f"{target_name} 的发消息按钮")
    return False


def _text_has_non_bmp_char(text: str) -> bool:
    return any(ord(ch) > 0xFFFF for ch in text)


def _insert_text_with_javascript(driver: webdriver.Edge, element, text: str) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
                const el = arguments[0];
                const text = arguments[1];
                if (!el) return false;
                el.focus();

                const fireInput = () => {
                  try {
                    el.dispatchEvent(new InputEvent('input', {
                      bubbles: true,
                      composed: true,
                      inputType: 'insertText',
                      data: text,
                    }));
                  } catch (_) {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                  }
                };

                if ('value' in el) {
                  const start = Number.isInteger(el.selectionStart) ? el.selectionStart : el.value.length;
                  const end = Number.isInteger(el.selectionEnd) ? el.selectionEnd : el.value.length;
                  const nextValue = el.value.slice(0, start) + text + el.value.slice(end);
                  const proto = Object.getPrototypeOf(el);
                  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                  if (descriptor && descriptor.set) {
                    descriptor.set.call(el, nextValue);
                  } else {
                    el.value = nextValue;
                  }
                  const caret = start + text.length;
                  if (el.setSelectionRange) el.setSelectionRange(caret, caret);
                  fireInput();
                  return true;
                }

                if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
                  const selection = window.getSelection();
                  const range = document.createRange();
                  range.selectNodeContents(el);
                  range.collapse(false);
                  selection.removeAllRanges();
                  selection.addRange(range);

                  let inserted = false;
                  try {
                    inserted = document.execCommand('insertText', false, text);
                  } catch (_) {
                    inserted = false;
                  }
                  if (!inserted) {
                    const textNode = document.createTextNode(text);
                    range.insertNode(textNode);
                    range.setStartAfter(textNode);
                    range.collapse(true);
                    selection.removeAllRanges();
                    selection.addRange(range);
                  }
                  fireInput();
                  return true;
                }

                return false;
                """,
                element,
                text,
            )
        )
    except WebDriverException:
        return False


def _insert_message_text(driver: webdriver.Edge, message_box, text: str) -> bool:
    try:
        message_box.click()
    except WebDriverException:
        try:
            driver.execute_script("arguments[0].focus();", message_box)
        except WebDriverException:
            return False
    time.sleep(random.uniform(0.15, 0.35))

    # Selenium/EdgeDriver cannot send non-BMP emoji like U+1F642 via send_keys().
    # CDP inserts text through the browser input pipeline, so this also works if
    # MESSAGE_TO_SEND is changed back to an emoji later.
    try:
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        return True
    except WebDriverException as exc:
        log.warning("CDP 插入表情失败，尝试备用输入方式：%s", exc.msg.splitlines()[0])

    if not _text_has_non_bmp_char(text):
        try:
            message_box.send_keys(text)
            return True
        except WebDriverException:
            pass

    return _insert_text_with_javascript(driver, message_box, text)


def _press_enter_to_send(driver: webdriver.Edge, message_box) -> bool:
    try:
        message_box.send_keys(Keys.ENTER)
        return True
    except WebDriverException:
        pass

    try:
        driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )
        driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )
        return True
    except WebDriverException:
        return False


def _click_send_button(driver: webdriver.Edge) -> bool:
    # 先找带“发送”文字或 aria-label 的按钮；Douyin 若改样式，这条仍比较稳。
    for selector in SEND_BUTTON_SELECTORS:
        buttons = driver.find_elements(By.CSS_SELECTOR, selector)
        for btn in buttons:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            label = (btn.text or "").strip()
            aria = (btn.get_attribute("aria-label") or "").strip()
            title = (btn.get_attribute("title") or "").strip()
            if "发送" in label or "发送" in aria or "发送" in title:
                btn.click()
                return True

    # 图 4 的发送按钮是底部输入栏最右侧圆形按钮，很多时候没有文字。
    # 这里只在页面底部区域选最靠右的可点按钮，避免误点左侧搜索/更多按钮。
    viewport_width = driver.execute_script("return window.innerWidth")
    viewport_height = driver.execute_script("return window.innerHeight")
    bottom_buttons = []
    for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
        if not btn.is_displayed() or not btn.is_enabled():
            continue
        rect = btn.rect
        center_x = rect["x"] + rect["width"] / 2
        center_y = rect["y"] + rect["height"] / 2
        if center_y > viewport_height * 0.75 and center_x > viewport_width * 0.5:
            bottom_buttons.append((center_x, btn))
    if bottom_buttons:
        bottom_buttons.sort(key=lambda item: item[0], reverse=True)
        bottom_buttons[0][1].click()
        return True
    return False


def _find_recent_chat_row(driver: webdriver.Edge, target_name: str):
    script = """
    const target = (arguments[0] || '').replace(/\\s+/g, '').trim();
    const norm = (text) => (text || '').replace(/\\s+/g, '').trim();
    const visible = (el) => {
      if (!el) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const hasTargetNameChild = (el) => {
      return Array.from(el.querySelectorAll('div,span,p,strong'))
        .filter((child) => visible(child))
        .some((child) => {
          const text = norm(child.innerText || child.textContent || '');
          return text === target || text.startsWith(target + '🔥') || text.startsWith(target + '🩶') || text.startsWith(target + '🖤');
        });
    };
    const nodes = Array.from(document.querySelectorAll('div,li,[role=\"listitem\"]'));
    let best = null;
    let bestScore = -1;
    for (const el of nodes) {
      if (!visible(el)) continue;
      const rect = el.getBoundingClientRect();
      const text = norm(el.innerText || el.textContent || '');
      if (!text.includes(target)) continue;
      if (text.includes('发消息') || text.includes('联系人') || text.includes('取消') || text.includes('搜索')) continue;
      if (rect.left < -5 || rect.left > window.innerWidth * 0.45) continue;
      if (rect.width < 180 || rect.width > window.innerWidth * 0.45) continue;
      if (rect.height < 42 || rect.height > 130) continue;

      let score = 0;
      if (hasTargetNameChild(el)) score += 100;
      if (getComputedStyle(el).cursor === 'pointer') score += 25;
      if (rect.left < 80) score += 15;
      if (rect.top > 70) score += 10;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
    """
    return driver.execute_script(script, target_name)


def _text_indicates_today_activity(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return False

    if any(token in compact for token in ("刚刚", "秒前", "分钟前", "今天")):
        return True

    today = datetime.now()
    today_tokens = {
        today.strftime("%Y-%m-%d"),
        today.strftime("%Y/%m/%d"),
        today.strftime("%m/%d"),
        f"{today.month}/{today.day}",
        f"{today.month}月{today.day}日",
    }
    if any(token and token in compact for token in today_tokens):
        return True

    if any(token in compact for token in ("昨天", "前天", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天")):
        return False
    if re.search(r"\b\d{1,2}[/-]\d{1,2}\b|\d{1,2}月\d{1,2}日", compact):
        return False

    return bool(re.search(r"(^|\s)(?:[01]?\d|2[0-3]):[0-5]\d($|\s)", compact))


def _left_list_shows_today_activity(driver: webdriver.Edge, target_name: str) -> tuple[bool, str]:
    try:
        row = _find_recent_chat_row(driver, target_name)
        if not row:
            return False, "左侧可见列表未找到会话行"
        text = driver.execute_script(
            "return (arguments[0].innerText || arguments[0].textContent || '').trim();",
            row,
        )
    except WebDriverException as exc:
        return False, f"读取左侧会话行失败：{exc.msg.splitlines()[0]}"

    if _text_indicates_today_activity(str(text)):
        return True, re.sub(r"\s+", " / ", str(text)).strip()
    return False, re.sub(r"\s+", " / ", str(text)).strip() or "左侧会话行无时间文字"


def _find_left_scroll_container(driver: webdriver.Edge):
    script = """
    const visible = (el) => {
      if (!el) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const candidates = Array.from(document.querySelectorAll('div,section,aside'))
      .filter((el) => {
        if (!visible(el)) return false;
        const rect = el.getBoundingClientRect();
        return rect.left >= -5 &&
          rect.left < window.innerWidth * 0.45 &&
          rect.width > 180 &&
          rect.width < window.innerWidth * 0.5 &&
          el.scrollHeight > el.clientHeight + 40;
      })
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (br.height * br.width) - (ar.height * ar.width);
      });
    return candidates[0] || null;
    """
    return driver.execute_script(script)


def _open_existing_chat_from_left_list(driver: webdriver.Edge, target: TargetFriend):
    log.info("尝试从左侧现有聊天列表打开：%s", target.confirm)
    driver.get(DOUYIN_MESSAGE_URL)
    time.sleep(OPEN_PAGE_WAIT_SEC)
    _wait_for_any(driver, SEARCH_INPUT_SELECTORS, timeout=10)
    _click_cancel_search(driver)
    time.sleep(1)

    scroll_container = _find_left_scroll_container(driver)
    if scroll_container:
        try:
            driver.execute_script("arguments[0].scrollTop = 0;", scroll_container)
        except WebDriverException:
            scroll_container = None

    for step in range(18):
        row = _find_recent_chat_row(driver, target.confirm)
        if row:
            log.info("在左侧聊天列表找到 %s，尝试直接打开会话", target.confirm)
            if _click_element_by_cdp(driver, row, f"{target.confirm} 的左侧聊天会话"):
                message_box = _wait_for_message_box(driver, timeout=CHAT_OPEN_WAIT_SEC)
                if message_box:
                    return message_box

        if not scroll_container:
            break
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollTop + Math.max(160, arguments[0].clientHeight * 0.55);",
            scroll_container,
        )
        time.sleep(0.6)
        at_bottom = driver.execute_script(
            "return arguments[0].scrollTop + arguments[0].clientHeight >= arguments[0].scrollHeight - 5;",
            scroll_container,
        )
        if at_bottom and step > 2:
            break

    return None


def _open_message_page(driver: webdriver.Edge) -> None:
    log.info("打开抖音私信页")
    driver.get(DOUYIN_MESSAGE_URL)
    time.sleep(OPEN_PAGE_WAIT_SEC)
    _dismiss_edge_first_run(driver)
    _wait_until_chat_ready(driver)


def _dismiss_edge_first_run(driver: webdriver.Edge) -> None:
    button_texts = ["是，继续", "是, 继续", "继续", "开始使用", "确认", "以后再说", "跳过"]
    for _ in range(3):
        clicked = False
        for text in button_texts:
            button = _find_clickable_text(driver, text)
            if button:
                try:
                    log.info("检测到 Edge 首次设置弹窗，点击：%s", text)
                    button.click()
                except WebDriverException:
                    driver.execute_script("arguments[0].click();", button)
                clicked = True
                time.sleep(2)
                break
        if not clicked:
            return
    driver.get(DOUYIN_MESSAGE_URL)
    time.sleep(OPEN_PAGE_WAIT_SEC)


def _page_has_douyin_verification(driver: webdriver.Edge) -> bool:
    try:
        frames = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('iframe'))
              .map((frame) => frame.src || frame.title || '')
              .some((value) => /verify|captcha|nocaptcha|验证/i.test(value));
            """
        )
        if frames:
            return True
        page_text = driver.execute_script("return document.body ? document.body.innerText : ''")
        return "安全验证" in page_text or "验证" in page_text
    except Exception:  # noqa: BLE001
        return False


def _wait_until_chat_ready(driver: webdriver.Edge) -> None:
    try:
        _wait_for_any(driver, SEARCH_INPUT_SELECTORS, timeout=10)
        return
    except TimeoutException:
        pass

    if _page_has_douyin_verification(driver):
        log.warning("检测到抖音安全验证，请在打开的 Edge 窗口里手动完成验证。")
    else:
        log.warning("尚未检测到聊天列表。若这是首次使用专用 Edge，请在打开的浏览器里手动登录抖音。")
    log.warning("脚本会最多等待 %s 秒，登录/验证完成并进入聊天页后会继续执行。", LOGIN_WAIT_TIMEOUT_SEC)

    end_at = time.time() + LOGIN_WAIT_TIMEOUT_SEC
    while time.time() < end_at:
        try:
            _wait_for_any(driver, SEARCH_INPUT_SELECTORS, timeout=5)
            log.info("已检测到聊天列表，继续执行。")
            return
        except TimeoutException:
            time.sleep(5)

    raise TimeoutException("等待登录或聊天页面加载超时")


def _chat_has_today_activity(driver: webdriver.Edge, message_box) -> tuple[bool, str]:
    """Return whether the visible chat history already shows today's activity."""
    today = datetime.now()
    today_tokens = [
        today.strftime("%Y-%m-%d"),
        today.strftime("%Y/%m/%d"),
        today.strftime("%m/%d"),
        today.strftime("%-m/%-d") if os.name != "nt" else f"{today.month}/{today.day}",
        f"{today.month}月{today.day}日",
    ]
    script = """
    const messageBox = arguments[0];
    const todayTokens = arguments[1] || [];
    if (!messageBox) return { ok: false, reason: '没有输入框，无法扫描聊天区' };

    const boxRect = messageBox.getBoundingClientRect();
    const leftLimit = Math.max(boxRect.left - 90, window.innerWidth * 0.34);
    const rightLimit = window.innerWidth - 8;
    const topLimit = 72;
    const bottomLimit = Math.max(topLimit + 80, boxRect.top - 4);

    const norm = (text) => (text || '').replace(/\\s+/g, ' ').trim();
    const visible = (el) => {
      if (!el) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' &&
        style.visibility !== 'hidden' &&
        rect.width > 0 &&
        rect.height > 0;
    };
    const inChatArea = (rect) => {
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      return cx >= leftLimit && cx <= rightLimit && cy >= topLimit && cy <= bottomLimit;
    };
    const timeOnlyPattern = /^(?:[01]?\\d|2[0-3]):[0-5]\\d$/;
    const oldDatePattern = /(昨天|前天|周[一二三四五六日天]|星期[一二三四五六日天]|\\d{4}[年\\-/\\. ]\\d{1,2}[月\\-/\\. ]\\d{1,2}|\\d{1,2}[\\-/]\\d{1,2}|\\d{1,2}月\\d{1,2}日)/;
    const explicitTodayPattern = /(今天|刚刚|分钟前|秒前)/;
    const currentDatePattern = new RegExp(todayTokens
      .filter(Boolean)
      .map((token) => token.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'))
      .join('|'));

    const textNodes = Array.from(document.querySelectorAll('div,span,p,time'))
      .filter((el) => visible(el))
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          text: norm(el.innerText || el.textContent || ''),
          x: rect.left,
          y: rect.top,
          width: rect.width,
          height: rect.height,
          centerY: rect.top + rect.height / 2,
        };
      })
      .filter((item) => item.text && item.text.length <= 80 && inChatArea({
        left: item.x,
        top: item.y,
        width: item.width,
        height: item.height,
      }))
      .sort((a, b) => a.centerY - b.centerY);

    let segment = 'unknown';
    for (const item of textNodes) {
      const text = item.text;
      if (explicitTodayPattern.test(text)) {
        return { ok: true, reason: `检测到今日标记：${text}` };
      }
      if (currentDatePattern.source !== '(?:)' && currentDatePattern.test(text)) {
        return { ok: true, reason: `检测到今天日期：${text}` };
      }
      if (oldDatePattern.test(text)) {
        segment = 'old';
        continue;
      }
      if (timeOnlyPattern.test(text) && segment !== 'old') {
        return { ok: true, reason: `检测到今日时间：${text}` };
      }
    }

    const mediaCount = Array.from(document.querySelectorAll('video,canvas,img'))
      .filter((el) => visible(el))
      .map((el) => el.getBoundingClientRect())
      .filter(inChatArea)
      .length;
    return {
      ok: false,
      reason: mediaCount ? `聊天区有媒体元素 ${mediaCount} 个，但未识别到今天时间` : '未识别到今天聊天记录',
    };
    """
    try:
        result = driver.execute_script(script, message_box, today_tokens)
    except WebDriverException as exc:
        return False, f"今日检测失败：{exc.msg.splitlines()[0]}"

    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("reason") or "")
    return False, "今日检测返回值异常"


def _open_chat_for_friend(driver: webdriver.Edge, target: TargetFriend) -> bool:
    log.info("正在处理 %s（搜索：%s）", target.confirm, target.search)

    try:
        driver.get(DOUYIN_MESSAGE_URL)
        time.sleep(2)
        search_box = _wait_for_any(driver, SEARCH_INPUT_SELECTORS, timeout=10)
        if SKIP_IF_TODAY_ALREADY_ACTIVE:
            left_active, left_reason = _left_list_shows_today_activity(driver, target.confirm)
            if left_active:
                log.info("左侧列表显示今日已续过，跳过发送：%s（%s）", target.confirm, left_reason)
                return True
        search_box.click()
        search_box.send_keys(Keys.CONTROL, "a")
        search_box.send_keys(Keys.BACKSPACE)
        search_box.send_keys(target.search)
        time.sleep(AFTER_SEARCH_WAIT_SEC)
        search_box.send_keys(Keys.ENTER)
    except TimeoutException:
        log.warning("未找到搜索框，尝试直接在页面里找聊天项")

    time.sleep(2)
    item = _find_clickable_text(driver, target.confirm)
    if item:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", item)
            clicked = _click_message_button_for_target(driver, target.confirm)
            if clicked:
                log.info("已命中 %s 的“发消息”候选并尝试点击", target.confirm)
            else:
                log.warning("未找到 %s 的“发消息”按钮，尝试点击联系人条目", target.confirm)
                clicked = _click_human_like(driver, item)
        except WebDriverException:
            clicked = _click_message_button_for_target(driver, target.confirm)
            if clicked:
                log.info("已命中 %s 的“发消息”候选并尝试点击", target.confirm)
            else:
                log.warning("未找到 %s 的“发消息”按钮，尝试用脚本点击联系人条目", target.confirm)
                clicked = _click_human_like(driver, item)
        if not clicked:
            return False
        message_box = _wait_for_message_box(driver, timeout=MESSAGE_BUTTON_VERIFY_WAIT_SEC)
        if not message_box:
            log.warning("点完 %s 的“发消息”后未出现输入框，准备重试按钮点击", target.confirm)
            _log_message_button_candidates(driver, target.confirm)
            if _click_message_button_for_target(driver, target.confirm):
                message_box = _wait_for_message_box(driver, timeout=CHAT_OPEN_WAIT_SEC)
        if not message_box:
            message_box = _open_existing_chat_from_left_list(driver, target)
        else:
            log.info("已打开 %s 的聊天输入框", target.confirm)
    else:
        log.warning("没有找到可确认的目标好友条目，跳过：%s", target.confirm)
        return False

    try:
        if not message_box:
            log.warning("点完 %s 后没有立刻出现消息输入框，重试一次联系人条目", target.confirm)
            if item and _click_human_like(driver, item):
                message_box = _wait_for_message_box(driver, timeout=CHAT_OPEN_WAIT_SEC)
        if not message_box:
            message_box = _open_existing_chat_from_left_list(driver, target)
        if not message_box:
            _save_failure_screenshot(driver, target.confirm, "未找到消息输入框")
            if _right_panel_looks_open(driver):
                raise TimeoutException("右侧面板已打开，但仍未找到消息输入框")
            raise TimeoutException("未找到消息输入框")

        if SKIP_IF_TODAY_ALREADY_ACTIVE:
            already_active, reason = _chat_has_today_activity(driver, message_box)
            if already_active:
                log.info("今日已续过，跳过发送：%s（%s）", target.confirm, reason)
                return True
            log.info("未检测到今日续火花记录，准备发送：%s（%s）", target.confirm, reason)

        if DRY_RUN_OPEN_CHAT_ONLY:
            log.info("自测模式：已打开 %s 的聊天框，不发送表情", target.confirm)
            return True

        if not _insert_message_text(driver, message_box, MESSAGE_TO_SEND):
            raise RuntimeError("未能把消息内容插入消息输入框")
        time.sleep(0.5)

        sent = _click_send_button(driver)
        if not sent:
            sent = _press_enter_to_send(driver, message_box)

        if not sent:
            raise RuntimeError("未能确认发送动作完成")

        time.sleep(AFTER_SEND_WAIT_SEC)
        log.info("发送完成：%s", target.confirm)
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("处理失败：%s", target.confirm)
        return False


def run_once() -> list[TargetResult]:
    driver = _build_driver()
    results: list[TargetResult] = []
    try:
        _open_message_page(driver)
        for entry in TARGET_FRIENDS:
            friend = _normalize_target(entry)
            ok = _open_chat_for_friend(driver, friend)
            results.append(
                TargetResult(friend=friend.confirm, ok=ok, detail="ok" if ok else "failed")
            )
            _sleep_random(BETWEEN_FRIENDS_MIN_SEC, BETWEEN_FRIENDS_MAX_SEC)
        return results
    finally:
        if STARTED_EDGE_PID:
            try:
                _close_started_edge_browser(driver)
            except Exception:  # noqa: BLE001
                pass
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
        _close_started_edge_process()


def _next_run_time(hhmm: str) -> datetime:
    now = datetime.now()
    hour, minute = map(int, hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_scheduler() -> None:
    if not DAILY_RUN_AT:
        raise ValueError("DAILY_RUN_AT 为空，无法进入定时模式")
    while True:
        target = _next_run_time(DAILY_RUN_AT)
        wait_sec = max(1, int((target - datetime.now()).total_seconds()))
        log.info("下一次执行时间：%s（约等待 %s 秒）", target.strftime("%Y-%m-%d %H:%M:%S"), wait_sec)
        time.sleep(wait_sec)
        try:
            run_once()
        except Exception:
            log.exception("定时任务执行失败")


def main() -> int:
    if DAILY_RUN_AT:
        run_scheduler()
    else:
        results = run_once()
        for item in results:
            logging.info("%s -> %s", item.friend, item.detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
