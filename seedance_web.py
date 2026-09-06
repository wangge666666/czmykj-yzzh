from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from workflow_core import PROJECT_DIR, WorkflowError


SEEDANCE_WEB_URL = (
    "https://console.volcengine.com/ark/region:cn-beijing/experience/gen_video"
    "?model=doubao-seedance-2-0-260128"
)
SEEDANCE_PROFILE_DIR = PROJECT_DIR / ".seedance-browser-profile"
DEFAULT_CDP_PORT = 9333
DEFAULT_GENERATION_TIMEOUT_SECONDS = 30 * 60
VIDEO_URL_RE = re.compile(
    r"^(?:https?://[^\s\"']+\.mp4(?:\?[^\s\"']*)?|"
    r"https?://[^\s\"']*ark-content-generation[^\s\"']*)$",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})(?:\.\d+)?\s*%")
REFERENCE_TOKEN_RE = re.compile(r"@(视频|图片)\s*(\d+)")
EXPECTED_REFERENCE_LABELS = ("视频1", "图片1", "图片2", "图片3")


class SeedanceLoginRequired(WorkflowError):
    pass


@dataclass(frozen=True)
class SeedanceBrowserStatus:
    available: bool
    running: bool
    logged_in: bool
    page_ready: bool
    message: str
    page_url: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class SeedanceWebBridge:
    """Control a dedicated, persistent Edge profile through local CDP only."""

    def __init__(self) -> None:
        self.port = int(os.getenv("SEEDANCE_CDP_PORT", str(DEFAULT_CDP_PORT)))
        self.profile_dir = Path(
            os.getenv("SEEDANCE_BROWSER_PROFILE", str(SEEDANCE_PROFILE_DIR))
        ).expanduser().resolve()
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _edge_path() -> Path | None:
        configured = os.getenv("SEEDANCE_EDGE_PATH", "").strip()
        candidates = [
            Path(configured).expanduser() if configured else None,
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        for candidate in candidates:
            if candidate is not None and candidate.is_file():
                return candidate.resolve()
        return None

    @staticmethod
    def playwright_available() -> bool:
        return importlib.util.find_spec("playwright.sync_api") is not None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _cdp_running(self) -> bool:
        try:
            response = requests.get(f"{self.cdp_url}/json/version", timeout=1.5)
            return response.ok and bool(response.json().get("webSocketDebuggerUrl"))
        except (requests.RequestException, ValueError):
            return False

    def available(self) -> bool:
        return self.playwright_available() and self._edge_path() is not None

    def launch(self) -> None:
        with self._lock:
            if not self.available():
                raise WorkflowError(
                    "Seedance 网页自动化组件不可用，请重新运行“安装运行环境.ps1”。"
                )
            if self._cdp_running():
                return
            edge = self._edge_path()
            assert edge is not None
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            command = [
                str(edge),
                f"--remote-debugging-port={self.port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                SEEDANCE_WEB_URL,
            ]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._process = subprocess.Popen(command, creationflags=flags)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self._cdp_running():
                    return
                if self._process.poll() is not None:
                    break
                time.sleep(0.4)
            raise WorkflowError("Seedance 专用浏览器未能启动，请关闭已有专用窗口后重试。")

    @staticmethod
    def _find_seedance_page(browser: Any) -> Any | None:
        fallback = None
        for context in browser.contexts:
            for page in context.pages:
                fallback = fallback or page
                if "console.volcengine.com/ark" in page.url:
                    return page
        return fallback

    @staticmethod
    def _is_logged_in(page: Any) -> bool:
        if "/login" in page.url.lower():
            return False
        try:
            login_buttons = page.get_by_role("button", name="登录", exact=True)
            if login_buttons.count() and login_buttons.first.is_visible():
                return False
        except Exception:
            pass
        return True

    def _connect(self) -> tuple[Any, Any, Any]:
        from playwright.sync_api import sync_playwright

        runtime = sync_playwright().start()
        try:
            browser = runtime.chromium.connect_over_cdp(self.cdp_url, timeout=15_000)
            page = self._find_seedance_page(browser)
            if page is None:
                context = browser.contexts[0]
                page = context.new_page()
                page.goto(SEEDANCE_WEB_URL, wait_until="domcontentloaded", timeout=45_000)
            return runtime, browser, page
        except Exception:
            runtime.stop()
            raise

    def status(self, *, launch: bool = False) -> SeedanceBrowserStatus:
        with self._lock:
            if not self.available():
                return SeedanceBrowserStatus(
                    available=False,
                    running=False,
                    logged_in=False,
                    page_ready=False,
                    message="网页自动化组件尚未安装完整。",
                )
            if launch:
                self.launch()
            if not self._cdp_running():
                return SeedanceBrowserStatus(
                    available=True,
                    running=False,
                    logged_in=False,
                    page_ready=False,
                    message="Seedance 专用浏览器尚未打开。",
                )
            runtime = None
            try:
                runtime, _browser, page = self._connect()
                logged_in = self._is_logged_in(page)
                return SeedanceBrowserStatus(
                    available=True,
                    running=True,
                    logged_in=logged_in,
                    page_ready="console.volcengine.com/ark" in page.url,
                    message=(
                        "Seedance 网页已登录，可以自动上传素材。"
                        if logged_in
                        else "请在专用浏览器中登录火山方舟，完成后返回本地网页检查连接。"
                    ),
                    page_url=page.url,
                )
            except Exception as exc:
                return SeedanceBrowserStatus(
                    available=True,
                    running=True,
                    logged_in=False,
                    page_ready=False,
                    message=f"暂时无法读取 Seedance 网页状态：{str(exc)[:180]}",
                )
            finally:
                if runtime is not None:
                    runtime.stop()

    @staticmethod
    def _frames(page: Any) -> Iterable[Any]:
        yield page.main_frame
        for frame in page.frames:
            if frame is not page.main_frame:
                yield frame

    @staticmethod
    def _visible_prompt_box(page: Any) -> Any | None:
        selectors = (
            "textarea:visible",
            "[contenteditable='true']:visible",
            "[role='textbox']:visible",
        )
        best = None
        best_area = -1.0
        for frame in SeedanceWebBridge._frames(page):
            for selector in selectors:
                locator = frame.locator(selector)
                for index in range(locator.count()):
                    item = locator.nth(index)
                    try:
                        box = item.bounding_box()
                    except Exception:
                        continue
                    if box is None:
                        continue
                    area = float(box["width"] * box["height"])
                    if area > best_area:
                        best = item
                        best_area = area
        return best

    @staticmethod
    def _upload_inputs(page: Any) -> list[Any]:
        inputs: list[Any] = []
        for frame in SeedanceWebBridge._frames(page):
            locator = frame.locator("input[type='file']")
            for index in range(locator.count()):
                inputs.append(locator.nth(index))
        return inputs

    @staticmethod
    def _input_meta(locator: Any) -> tuple[str, bool]:
        accept = (locator.get_attribute("accept") or "").lower()
        multiple = locator.get_attribute("multiple") is not None
        return accept, multiple

    @staticmethod
    def _reference_uploader(page: Any) -> Any:
        return page.locator("[data-testid='stacked-reference-uploader']").first

    @staticmethod
    def _reference_items(page: Any) -> Any:
        return SeedanceWebBridge._reference_uploader(page).locator(
            "li[data-uploader-uid]"
        )

    @staticmethod
    def _dismiss_pending_cost_modal(page: Any) -> None:
        cancel = page.locator("[data-testid='gen-video-cost-confirm-cancel']")
        if cancel.count() and cancel.first.is_visible():
            cancel.first.click()
            page.wait_for_timeout(250)

    def _clear_existing_references(self, page: Any) -> None:
        items = self._reference_items(page)
        if not items.count():
            return
        clear = page.get_by_text("全部清空", exact=True)
        for index in range(clear.count()):
            candidate = clear.nth(index)
            if candidate.is_visible():
                candidate.click()
                break
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self._reference_items(page).count():
                return
            page.wait_for_timeout(250)

        # The text action can change between console releases. Fall back to
        # the close controls inside the uploader without touching other cards.
        for _ in range(20):
            close_buttons = self._reference_uploader(page).locator(
                "[class*='close-icon']"
            )
            if not close_buttons.count():
                break
            close_buttons.last.click(force=True)
            page.wait_for_timeout(150)
        if self._reference_items(page).count():
            raise WorkflowError("无法清空 Seedance 页面中上一次遗留的参考素材。")

    def _wait_for_reference_order(self, page: Any, timeout_ms: int = 120_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        latest_labels: list[str] = []
        while time.monotonic() < deadline:
            items = self._reference_items(page)
            latest_labels = []
            for index in range(items.count()):
                label = items.nth(index).locator("[class*='stacked-label-text']")
                if label.count():
                    latest_labels.append((label.first.inner_text() or "").strip())
            if tuple(latest_labels) == EXPECTED_REFERENCE_LABELS:
                preset_ids = [
                    items.nth(index).get_attribute("data-uploader-uid") or ""
                    for index in range(items.count())
                ]
                if any(item.startswith("tpl-doc-") for item in preset_ids):
                    raise WorkflowError("Seedance 仍保留了示例素材，已停止以避免引用错误。")
                return
            page.wait_for_timeout(400)
        raise WorkflowError(
            "Seedance 未能按“视频1、图片1、图片2、图片3”完成素材映射；"
            f"当前识别为：{latest_labels or '空'}。"
        )

    @staticmethod
    def _insert_editor_text(page: Any, text: str) -> None:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if part:
                page.keyboard.insert_text(part)
            if index < len(parts) - 1:
                # Plain Enter submits the Seedance composer. Shift+Enter is
                # the editor's non-submitting line-break command.
                page.keyboard.press("Shift+Enter")

    def _insert_reference_mention(self, page: Any, label: str) -> None:
        page.keyboard.insert_text("@")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            items = page.locator("[class*='mentionListItem-']:visible")
            matches: list[tuple[float, Any]] = []
            for index in range(items.count()):
                item = items.nth(index)
                if (item.inner_text() or "").strip() != label:
                    continue
                box = item.bounding_box()
                if box is not None:
                    matches.append((float(box["width"] * box["height"]), item))
            if matches:
                matches.sort(key=lambda value: value[0])
                matches[0][1].click()
                page.wait_for_timeout(120)
                return
            page.wait_for_timeout(150)
        raise WorkflowError(f"Seedance 的 @ 菜单中找不到真实引用：{label}。")

    def _fill_prompt_with_mentions(self, page: Any, prompt: str) -> None:
        prompt_box = self._visible_prompt_box(page)
        if prompt_box is None:
            raise WorkflowError("无法找到 Seedance 提示词输入框。")
        prompt_box.fill("")
        prompt_box.click()
        cursor = 0
        mention_count = 0
        for match in REFERENCE_TOKEN_RE.finditer(prompt):
            self._insert_editor_text(page, prompt[cursor : match.start()])
            label = f"{match.group(1)}{match.group(2)}"
            if label not in EXPECTED_REFERENCE_LABELS:
                raise WorkflowError(f"提示词引用了未上传的素材：@{label}。")
            self._insert_reference_mention(page, label)
            mention_count += 1
            cursor = match.end()
        self._insert_editor_text(page, prompt[cursor:])
        semantic_mentions = prompt_box.locator(".node-mediaTagSlot").count()
        if semantic_mentions != mention_count:
            raise WorkflowError(
                "提示词中的 @素材 未全部转换为 Seedance 真实引用节点："
                f"期望 {mention_count} 个，实际 {semantic_mentions} 个。"
            )

    @staticmethod
    def _collect_response_artifacts(value: Any, artifacts: dict[str, list[str]]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower()
                if isinstance(item, str):
                    text = item.strip()
                    if normalized in {"task_id", "taskid", "id"} and text:
                        if text not in artifacts["task_ids"]:
                            artifacts["task_ids"].append(text)
                    if (
                        normalized in {"video_url", "videourl", "download_url", "downloadurl", "url"}
                        and VIDEO_URL_RE.match(text)
                        and text not in artifacts["video_urls"]
                    ):
                        artifacts["video_urls"].append(text)
                SeedanceWebBridge._collect_response_artifacts(item, artifacts)
        elif isinstance(value, list):
            for item in value:
                SeedanceWebBridge._collect_response_artifacts(item, artifacts)

    @staticmethod
    def _page_video_urls(page: Any) -> list[str]:
        urls: list[str] = []
        for frame in SeedanceWebBridge._frames(page):
            for selector, expression in (
                ("video", "element => element.currentSrc || element.src || ''"),
                ("a[href]", "element => element.href || ''"),
            ):
                locator = frame.locator(selector)
                for index in range(locator.count()):
                    try:
                        url = str(locator.nth(index).evaluate(expression) or "").strip()
                    except Exception:
                        continue
                    if VIDEO_URL_RE.match(url) and url not in urls:
                        urls.append(url)
        return urls

    @staticmethod
    def _visible_page_text(page: Any) -> str:
        chunks: list[str] = []
        for frame in SeedanceWebBridge._frames(page):
            try:
                text = frame.locator("body").inner_text(timeout=3_000)
            except Exception:
                continue
            if text:
                chunks.append(text)
        return "\n".join(chunks)

    @staticmethod
    def _download_result(page: Any, url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        response = page.context.request.get(url, timeout=120_000)
        if not response.ok:
            raise WorkflowError(f"Seedance 成片下载失败（HTTP {response.status}）。")
        body = response.body()
        if len(body) < 1024:
            raise WorkflowError("Seedance 成片下载内容异常，文件过小。")
        output_path.write_bytes(body)

    def _wait_for_result(
        self,
        *,
        page: Any,
        artifacts: dict[str, list[str]],
        initial_video_urls: set[str],
        output_dir: Path,
        on_progress: Callable[[int, str], None] | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(60, timeout_seconds)
        started = time.monotonic()
        last_progress = -1
        last_stage = ""

        def report(progress: int, stage: str) -> None:
            nonlocal last_progress, last_stage
            progress = max(60, min(99, int(progress)))
            if on_progress is not None and (progress != last_progress or stage != last_stage):
                on_progress(progress, stage)
            last_progress = progress
            last_stage = stage

        report(62, "Seedance 已接收任务，正在排队")
        while time.monotonic() < deadline:
            page.wait_for_timeout(2_000)
            text = self._visible_page_text(page)
            percentages = [
                int(match.group(1))
                for match in PERCENT_RE.finditer(text)
                if 0 <= int(match.group(1)) <= 100
            ]
            if percentages:
                official = max(percentages)
                report(62 + round(official * 0.34), f"Seedance 网页进度：{official}%")
            else:
                elapsed = time.monotonic() - started
                estimated = min(92, 62 + int(elapsed / 30))
                stage = "Seedance 正在生成（网页未提供精确百分比）"
                if "排队" in text:
                    stage = "Seedance 正在排队"
                elif any(word in text for word in ("生成中", "处理中", "渲染中")):
                    stage = "Seedance 正在生成"
                report(estimated, stage)

            candidates: list[str] = []
            for url in artifacts["video_urls"] + self._page_video_urls(page):
                if url not in initial_video_urls and url not in candidates:
                    candidates.append(url)
            for url in reversed(candidates):
                output_path = output_dir / "Seedance_最终成片.mp4"
                try:
                    self._download_result(page, url, output_path)
                except Exception:
                    continue
                report(99, "Seedance 已生成，正在保存成片")
                return {
                    "output_path": str(output_path),
                    "video_url": url,
                    "task_id": artifacts["task_ids"][-1] if artifacts["task_ids"] else "",
                    "timed_out": False,
                }

        return {
            "output_path": "",
            "video_url": "",
            "task_id": artifacts["task_ids"][-1] if artifacts["task_ids"] else "",
            "timed_out": True,
        }

    def inspect_controls(self) -> dict[str, Any]:
        """Return non-sensitive UI metadata used to adapt selectors after login."""
        with self._lock:
            self.launch()
            runtime = None
            try:
                runtime, _browser, page = self._connect()
                page.bring_to_front()
                inputs = [
                    {"accept": self._input_meta(item)[0], "multiple": self._input_meta(item)[1]}
                    for item in self._upload_inputs(page)
                ]
                buttons: list[str] = []
                for frame in self._frames(page):
                    locator = frame.locator("button:visible")
                    for index in range(min(locator.count(), 80)):
                        text = (locator.nth(index).inner_text() or "").strip()
                        if text and text not in buttons:
                            buttons.append(text[:80])
                return {
                    "logged_in": self._is_logged_in(page),
                    "url": page.url,
                    "file_inputs": inputs,
                    "has_prompt_box": self._visible_prompt_box(page) is not None,
                    "buttons": buttons[:80],
                }
            finally:
                if runtime is not None:
                    runtime.stop()

    def prepare_materials(
        self,
        *,
        depth_video: str | Path,
        person_image: str | Path,
        clothing_image: str | Path,
        scene_image: str | Path,
        prompt: str,
        submit: bool = False,
        wait_for_result: bool = False,
        output_dir: str | Path | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        paths = [Path(item).expanduser().resolve() for item in (
            depth_video,
            person_image,
            clothing_image,
            scene_image,
        )]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise WorkflowError("自动上传时找不到本地素材：" + "、".join(missing))
        with self._lock:
            self.launch()
            runtime = None
            try:
                runtime, _browser, page = self._connect()
                page.bring_to_front()
                if not self._is_logged_in(page):
                    raise SeedanceLoginRequired(
                        "请先在已打开的 Seedance 专用浏览器中登录火山方舟。"
                    )
                if "/experience/gen_video" not in page.url or "doubao-seedance-2-0-260128" not in page.url:
                    page.goto(SEEDANCE_WEB_URL, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(3_000)
                self._dismiss_pending_cost_modal(page)
                self._clear_existing_references(page)
                inputs = self._upload_inputs(page)
                if not inputs:
                    raise WorkflowError(
                        "当前 Seedance 页面尚未显示素材上传控件；请确认已进入视频生成界面。"
                    )

                mixed = None
                video_input = None
                image_input = None
                for item in inputs:
                    accept, multiple = self._input_meta(item)
                    accepts_video = "video" in accept or ".mp4" in accept or not accept
                    accepts_image = "image" in accept or not accept
                    if accepts_video and accepts_image and multiple:
                        mixed = item
                        break
                    if video_input is None and accepts_video:
                        video_input = item
                    if image_input is None and accepts_image:
                        image_input = item

                if mixed is not None:
                    mixed.set_input_files([str(path) for path in paths])
                elif video_input is not None and image_input is not None:
                    video_input.set_input_files(str(paths[0]))
                    image_input.set_input_files([str(path) for path in paths[1:]])
                else:
                    raise WorkflowError("无法识别 Seedance 的视频与图片上传控件。")
                self._wait_for_reference_order(page)
                self._fill_prompt_with_mentions(page, prompt)
                page.wait_for_timeout(500)

                if not submit:
                    return {"submitted": False, "page_url": page.url}

                artifacts: dict[str, list[str]] = {"task_ids": [], "video_urls": []}
                initial_video_urls = set(self._page_video_urls(page))

                def remember_response(response: Any) -> None:
                    try:
                        content_type = (response.headers.get("content-type") or "").lower()
                        if "json" not in content_type or response.status >= 400:
                            return
                        body = response.body()
                        if len(body) > 5_000_000:
                            return
                        payload = json.loads(body)
                        self._collect_response_artifacts(payload, artifacts)
                    except Exception:
                        return

                page.on("response", remember_response)
                submit_button = page.locator(
                    "[data-testid='video-sender-submit-button']"
                )
                submit_deadline = time.monotonic() + 10 * 60
                while time.monotonic() < submit_deadline:
                    if (
                        submit_button.count() == 1
                        and submit_button.first.is_visible()
                        and submit_button.first.is_enabled()
                    ):
                        break
                    page.wait_for_timeout(500)
                else:
                    raise WorkflowError(
                        "素材和提示词已正确填入，但 Seedance 生成按钮长时间不可用；"
                        "请检查素材审核或上传状态。"
                    )
                submit_button.first.click()
                page.wait_for_timeout(350)
                cost_confirm = page.locator(
                    "[data-testid='gen-video-cost-confirm-ok']"
                )
                if cost_confirm.count() and cost_confirm.first.is_visible():
                    cost_confirm.first.click()
                page.wait_for_timeout(1200)
                result: dict[str, Any] = {
                    "submitted": True,
                    "page_url": page.url,
                    "task_id": artifacts["task_ids"][-1] if artifacts["task_ids"] else "",
                }
                if wait_for_result:
                    if output_dir is None:
                        raise WorkflowError("自动等待成片时缺少本地保存目录。")
                    result.update(
                        self._wait_for_result(
                            page=page,
                            artifacts=artifacts,
                            initial_video_urls=initial_video_urls,
                            output_dir=Path(output_dir).expanduser().resolve(),
                            on_progress=on_progress,
                            timeout_seconds=(
                                timeout_seconds
                                if timeout_seconds is not None
                                else int(
                                    os.getenv(
                                        "SEEDANCE_WEB_TIMEOUT_SECONDS",
                                        str(DEFAULT_GENERATION_TIMEOUT_SECONDS),
                                    )
                                )
                            ),
                        )
                    )
                return result
            finally:
                if runtime is not None:
                    runtime.stop()


SEEDANCE_WEB = SeedanceWebBridge()
