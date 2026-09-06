from __future__ import annotations

import os
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Text, Tk, filedialog, messagebox
from tkinter import ttk
from typing import Any, Callable

from workflow_core import (
    DEFAULT_ARK_BASE_URL,
    DEFAULT_PROMPT,
    DEFAULT_SEEDANCE_MODEL,
    PROJECT_DIR,
    ArkVideoClient,
    TosMediaStore,
    WorkflowError,
    build_seedance_payload,
    download_file,
    inspect_video,
    load_env_file,
    run_depth_generation,
    save_job_record,
    timestamped_run_dir,
    validate_seedance_reference_video,
    video_info_dict,
)


class WorkflowApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("深度视频 → Seedance 2.0 自动工作流")
        self.root.geometry("1080x900")
        self.root.minsize(900, 720)
        load_env_file()

        self.reference_video = StringVar()
        self.depth_output = StringVar()
        self.blur_range = StringVar()
        self.person_source = StringVar()
        self.clothing_source = StringVar()
        self.scene_source = StringVar()
        self.depth_reference = StringVar()
        self.model = StringVar(value=os.getenv("ARK_VIDEO_MODEL", DEFAULT_SEEDANCE_MODEL))
        self.resolution = StringVar(value="720p")
        self.ratio = StringVar(value="adaptive")
        self.duration = IntVar(value=5)
        self.generate_audio = BooleanVar(value=False)
        self.watermark = BooleanVar(value=False)
        self.delete_tos_after = BooleanVar(value=True)
        self.task_id = StringVar()
        self.status = StringVar(value="准备就绪")
        self.busy = False
        self.controls: list[ttk.Button] = []
        self.uploaded_object_key = ""
        self.last_output_dir: Path | None = None

        self._configure_style()
        self._build_ui()
        self._log("已载入工作流。请依次选择参考视频、人物、服装和场景素材。")
        if os.getenv("ARK_API_KEY", "").strip():
            self._log("已检测到 ARK_API_KEY（密钥内容不会显示）。")
        else:
            self._log("未检测到 ARK_API_KEY，请检查项目目录中的 .env。")

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#5f6368")
        style.configure("Status.TLabel", foreground="#0b57d0", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        canvas = __import__("tkinter").Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas, padding=16)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(body_window, width=event.width))
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        ttk.Label(body, text="深度视频 → Seedance 2.0 自动工作流", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="本地生成清晰单目深度视频，再用人物、服装、场景参考生成新视频。付费任务提交前会再次确认。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        step1 = ttk.LabelFrame(body, text="第 1–2 步：参考视频与深度视频", style="Section.TLabelframe", padding=12)
        step1.pack(fill="x", pady=(0, 12))
        self._file_row(step1, 0, "参考视频", self.reference_video, self._browse_reference_video, "选择 MP4 / MOV")
        self._file_row(step1, 1, "深度视频输出", self.depth_output, self._browse_depth_output, "自动建议输出位置")
        ttk.Label(step1, text="运动模糊补偿帧（可选）").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(step1, textvariable=self.blur_range, width=26).grid(row=2, column=1, sticky="w", padx=(8, 8), pady=6)
        ttk.Label(step1, text="格式示例：291:305；一般留空", style="Hint.TLabel").grid(row=2, column=2, sticky="w")
        depth_button = ttk.Button(step1, text="生成深度视频", command=self._start_depth_only)
        depth_button.grid(row=3, column=1, sticky="w", padx=8, pady=(8, 0))
        self.controls.append(depth_button)
        step1.columnconfigure(1, weight=1)

        step3 = ttk.LabelFrame(body, text="第 3 步：替换素材", style="Section.TLabelframe", padding=12)
        step3.pack(fill="x", pady=(0, 12))
        self._file_row(step3, 0, "人物形象（@图片 1）", self.person_source, lambda: self._browse_image(self.person_source), "本地图或 asset://ID")
        self._file_row(step3, 1, "服装参考（@图片 2）", self.clothing_source, lambda: self._browse_image(self.clothing_source), "建议无脸服装图")
        self._file_row(step3, 2, "场景参考（@图片 3）", self.scene_source, lambda: self._browse_image(self.scene_source), "场景完整、透视清楚")
        ttk.Label(
            step3,
            text="真人形象必须使用已授权的 asset:// 素材 ID；本地人物图仅适合无真人脸、虚构或 AI 角色。",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        step3.columnconfigure(1, weight=1)

        step4 = ttk.LabelFrame(body, text="第 4 步：Seedance 2.0 生成", style="Section.TLabelframe", padding=12)
        step4.pack(fill="x", pady=(0, 12))
        self._file_row(
            step4,
            0,
            "深度视频 URL（@视频 1）",
            self.depth_reference,
            self._start_upload_only,
            "粘贴公网 URL，或点右侧自动上传 TOS",
            button_text="上传 TOS",
        )

        options = ttk.Frame(step4)
        options.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        ttk.Label(options, text="模型").pack(side="left")
        ttk.Entry(options, textvariable=self.model, width=34).pack(side="left", padx=(6, 16))
        ttk.Label(options, text="分辨率").pack(side="left")
        ttk.Combobox(options, textvariable=self.resolution, values=("480p", "720p", "1080p", "4k"), width=8, state="readonly").pack(side="left", padx=(6, 16))
        ttk.Label(options, text="比例").pack(side="left")
        ttk.Combobox(options, textvariable=self.ratio, values=("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"), width=9, state="readonly").pack(side="left", padx=(6, 16))
        ttk.Label(options, text="时长").pack(side="left")
        ttk.Spinbox(options, from_=4, to=15, textvariable=self.duration, width=5).pack(side="left", padx=(6, 2))
        ttk.Label(options, text="秒").pack(side="left")

        toggles = ttk.Frame(step4)
        toggles.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Checkbutton(toggles, text="生成声音", variable=self.generate_audio).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(toggles, text="添加 AI 水印", variable=self.watermark).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(toggles, text="完成后删除本次 TOS 临时视频", variable=self.delete_tos_after).pack(side="left")

        ttk.Label(step4, text="提示词（素材编号由程序固定映射）").grid(row=3, column=0, sticky="nw", pady=(3, 4))
        self.prompt_text = Text(step4, height=11, wrap="word", font=("Microsoft YaHei UI", 10), undo=True)
        self.prompt_text.grid(row=3, column=1, columnspan=2, sticky="nsew", padx=(8, 0), pady=(3, 4))
        self.prompt_text.insert("1.0", DEFAULT_PROMPT)
        step4.columnconfigure(1, weight=1)

        actions = ttk.Frame(step4)
        actions.grid(row=4, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0))
        for label, command in (
            ("检查 API 配置（免费）", self._start_api_check),
            ("提交并等待成片", self._confirm_and_start_submit),
            ("一键执行完整流程", self._confirm_and_start_full),
        ):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side="left", padx=(0, 8))
            self.controls.append(button)

        history = ttk.LabelFrame(body, text="任务查询与下载", style="Section.TLabelframe", padding=12)
        history.pack(fill="x", pady=(0, 12))
        ttk.Label(history, text="任务 ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(history, textvariable=self.task_id).grid(row=0, column=1, sticky="ew", padx=8)
        query_button = ttk.Button(history, text="查询并下载", command=self._start_query)
        query_button.grid(row=0, column=2)
        self.controls.append(query_button)
        history.columnconfigure(1, weight=1)

        logs = ttk.LabelFrame(body, text="运行状态", style="Section.TLabelframe", padding=12)
        logs.pack(fill="both", expand=True, pady=(0, 12))
        ttk.Label(logs, textvariable=self.status, style="Status.TLabel").pack(anchor="w", pady=(0, 6))
        self.log_text = Text(logs, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        footer = ttk.Frame(logs)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Button(footer, text="打开最近输出目录", command=self._open_last_output).pack(side="left")
        ttk.Button(footer, text="打开方舟模型开通页", command=lambda: webbrowser.open("https://console.volcengine.com/ark/region:cn-beijing/openManagement")).pack(side="left", padx=8)

    def _file_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: StringVar,
        command: Callable[[], None],
        hint: str,
        *,
        button_text: str = "浏览…",
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=6)
        side = ttk.Frame(parent)
        side.grid(row=row, column=2, sticky="e", pady=6)
        ttk.Label(side, text=hint, style="Hint.TLabel").pack(side="left", padx=(0, 8))
        button = ttk.Button(side, text=button_text, command=command)
        button.pack(side="left")
        self.controls.append(button)

    def _browse_reference_video(self) -> None:
        value = filedialog.askopenfilename(title="选择参考视频", filetypes=(("视频", "*.mp4 *.mov"), ("所有文件", "*.*")))
        if not value:
            return
        self.reference_video.set(value)
        source = Path(value)
        output_dir = PROJECT_DIR / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        candidate = output_dir / f"{source.stem}_深度图.mp4"
        if candidate.exists():
            candidate = output_dir / f"{source.stem}_深度图_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        self.depth_output.set(str(candidate))
        try:
            info = inspect_video(source)
            suggested = min(15, max(4, round(info.duration)))
            self.duration.set(suggested)
            self._log(f"参考视频：{info.width}×{info.height}，{info.fps:.2f} FPS，约 {info.duration:.2f} 秒。")
        except WorkflowError as exc:
            self._log(str(exc))

    def _browse_depth_output(self) -> None:
        value = filedialog.asksaveasfilename(title="保存深度视频", defaultextension=".mp4", filetypes=(("MP4", "*.mp4"),))
        if value:
            self.depth_output.set(value)

    def _browse_image(self, variable: StringVar) -> None:
        value = filedialog.askopenfilename(
            title="选择参考图片",
            filetypes=(("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif *.heic *.heif"), ("所有文件", "*.*")),
        )
        if value:
            variable.set(value)

    def _log(self, message: str) -> None:
        def write() -> None:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{stamp}] {message}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        if threading.current_thread() is threading.main_thread():
            write()
        else:
            self.root.after(0, write)

    def _set_status(self, value: str) -> None:
        self.root.after(0, lambda: self.status.set(value))

    def _set_variable(self, variable: StringVar, value: str) -> None:
        self.root.after(0, lambda: variable.set(value))

    def _run_async(self, name: str, job: Callable[[], Any]) -> None:
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束。")
            return
        self.busy = True
        for control in self.controls:
            try:
                control.configure(state="disabled")
            except Exception:
                pass
        self.status.set(name)

        def worker() -> None:
            try:
                job()
            except WorkflowError as exc:
                self._log(f"失败：{exc}")
                self._set_status("任务失败")
                self.root.after(0, lambda: messagebox.showerror("任务失败", str(exc)))
            except Exception as exc:  # pragma: no cover - GUI safety net
                self._log(f"未预期错误：{exc}")
                self._log(traceback.format_exc())
                self._set_status("任务失败")
                self.root.after(0, lambda: messagebox.showerror("未预期错误", str(exc)))
            finally:
                def release() -> None:
                    self.busy = False
                    for control in self.controls:
                        try:
                            control.configure(state="normal")
                        except Exception:
                            pass

                self.root.after(0, release)

        threading.Thread(target=worker, name="workflow-worker", daemon=True).start()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "reference_video": self.reference_video.get().strip(),
            "depth_output": self.depth_output.get().strip(),
            "blur_range": self.blur_range.get().strip(),
            "person_source": self.person_source.get().strip(),
            "clothing_source": self.clothing_source.get().strip(),
            "scene_source": self.scene_source.get().strip(),
            "depth_reference": self.depth_reference.get().strip(),
            "model": self.model.get().strip(),
            "resolution": self.resolution.get(),
            "ratio": self.ratio.get(),
            "duration": int(self.duration.get()),
            "generate_audio": bool(self.generate_audio.get()),
            "watermark": bool(self.watermark.get()),
            "delete_tos_after": bool(self.delete_tos_after.get()),
            "prompt": self.prompt_text.get("1.0", "end").strip(),
            "task_id": self.task_id.get().strip(),
        }

    def _api_client(self) -> ArkVideoClient:
        load_env_file(override=True)
        return ArkVideoClient(
            os.getenv("ARK_API_KEY", ""),
            base_url=os.getenv("ARK_BASE_URL", DEFAULT_ARK_BASE_URL),
        )

    def _start_api_check(self) -> None:
        def job() -> None:
            self._log("正在执行只读鉴权检查，不会创建视频任务。")
            result = self._api_client().check_credentials()
            self._log(result)
            if TosMediaStore.configured():
                self._log("TOS 自动上传配置已齐全。")
            else:
                self._log("TOS 尚未配置；可粘贴深度视频公网 URL，或按 README 配置 TOS。")
            self._set_status("配置检查完成")

        self._run_async("正在检查 API 配置…", job)

    def _generate_depth(self, data: dict[str, Any]) -> Path:
        if not data["reference_video"]:
            raise WorkflowError("请先选择参考视频。")
        output_text = data["depth_output"]
        if not output_text:
            output_dir = PROJECT_DIR / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            source = Path(data["reference_video"])
            output_text = str(output_dir / f"{source.stem}_深度图_{datetime.now():%Y%m%d_%H%M%S}.mp4")
            self._set_variable(self.depth_output, output_text)
        output = Path(output_text).expanduser().resolve()
        self._set_status("正在生成深度视频…")
        self._log(f"深度处理开始：{Path(data['reference_video']).name}")
        result = run_depth_generation(
            data["reference_video"],
            output,
            blur_range=data["blur_range"],
            on_log=self._log,
        )
        self.last_output_dir = result.parent
        self._log(f"深度视频已生成：{result}")
        return result

    def _start_depth_only(self) -> None:
        data = self._snapshot()

        def job() -> None:
            self._generate_depth(data)
            self._set_status("深度视频生成完成")

        self._run_async("正在生成深度视频…", job)

    def _upload_depth(self, depth_path: str) -> str:
        validate_seedance_reference_video(depth_path)
        self._set_status("正在上传深度视频到 TOS…")
        self._log("正在上传深度视频到私有 TOS，并生成 3 天有效的签名 URL。")
        store = TosMediaStore()
        uploaded = store.upload_video(depth_path)
        self.uploaded_object_key = uploaded.object_key
        self._set_variable(self.depth_reference, uploaded.signed_url)
        self._log("TOS 上传完成。签名 URL 已自动填入（日志不显示 URL）。")
        return uploaded.signed_url

    def _start_upload_only(self) -> None:
        data = self._snapshot()
        depth_path = data["depth_output"]

        def job() -> None:
            if not depth_path:
                raise WorkflowError("请先生成或选择深度视频。")
            self._upload_depth(depth_path)
            self._set_status("深度视频上传完成")

        self._run_async("正在上传深度视频…", job)

    def _confirm_paid(self, title: str, data: dict[str, Any]) -> bool:
        message = (
            f"模型：{data['model']}\n"
            f"输出：{data['resolution']} / {data['ratio']} / {data['duration']} 秒\n\n"
            "继续后会提交 Seedance 2.0 付费生成任务。生成式模型会尽量匹配动作和运镜，但不能保证数学意义上的逐帧完全一致。\n\n"
            "确认提交吗？"
        )
        return messagebox.askyesno(title, message, icon="warning")

    def _confirm_and_start_submit(self) -> None:
        data = self._snapshot()
        if not self._confirm_paid("确认付费提交", data):
            return
        self._run_async("准备提交 Seedance 任务…", lambda: self._submit_and_wait(data))

    def _ensure_depth_reference(self, data: dict[str, Any], depth_path: str | None = None) -> str:
        existing = data["depth_reference"].strip()
        if existing:
            return existing
        local_path = depth_path or data["depth_output"]
        if not local_path:
            raise WorkflowError("缺少深度视频 URL，也没有可自动上传的本地深度视频。")
        return self._upload_depth(local_path)

    def _submit_and_wait(self, data: dict[str, Any], *, depth_path: str | None = None) -> Path:
        depth_reference = self._ensure_depth_reference(data, depth_path)
        payload = build_seedance_payload(
            prompt=data["prompt"],
            person_source=data["person_source"],
            clothing_source=data["clothing_source"],
            scene_source=data["scene_source"],
            depth_video_reference=depth_reference,
            model=data["model"],
            resolution=data["resolution"],
            ratio=data["ratio"],
            duration=data["duration"],
            generate_audio=data["generate_audio"],
            watermark=data["watermark"],
        )
        run_dir = timestamped_run_dir("seedance")
        self.last_output_dir = run_dir
        client = self._api_client()
        self._set_status("正在提交 Seedance 2.0 任务…")
        self._log("正在提交付费生成任务；为防重复计费，POST 请求不会自动重试。")
        task_id = client.create_task(payload)
        self._set_variable(self.task_id, task_id)
        self._log(f"任务已提交：{task_id}")
        record = {
            "task_id": task_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "model": data["model"],
            "resolution": data["resolution"],
            "ratio": data["ratio"],
            "duration": data["duration"],
            "generate_audio": data["generate_audio"],
            "watermark": data["watermark"],
            "reference_video": data["reference_video"],
            "depth_video": depth_path or data["depth_output"],
            "person_source": data["person_source"],
            "clothing_source": data["clothing_source"],
            "scene_source": data["scene_source"],
            "prompt": data["prompt"],
            "status": "submitted",
        }
        save_job_record(run_dir, record)

        def on_status(task: dict[str, Any]) -> None:
            status = str(task.get("status", "unknown"))
            self._set_status(f"Seedance 状态：{status}")
            self._log(f"Seedance 状态更新：{status}")

        task = client.wait_for_task(task_id, on_status=on_status)
        video_url = str((task.get("content") or {}).get("video_url") or "")
        if not video_url:
            raise WorkflowError("任务已成功，但响应中没有成片下载 URL。")
        output = run_dir / f"生成成片_{task_id}.mp4"
        self._set_status("正在下载成片…")
        self._log("生成成功，正在下载 24 小时有效的成片 URL。")

        last_percent = -1

        def progress(downloaded: int, total: int) -> None:
            nonlocal last_percent
            if total <= 0:
                return
            percent = int(downloaded * 100 / total)
            if percent // 10 != last_percent // 10:
                last_percent = percent
                self._log(f"下载进度：{percent}%")

        download_file(video_url, output, on_progress=progress)
        record["status"] = "succeeded"
        record["output"] = str(output)
        record["usage"] = task.get("usage")
        save_job_record(run_dir, record)
        self._log(f"成片已保存：{output}")

        if data["delete_tos_after"] and self.uploaded_object_key:
            try:
                TosMediaStore().delete(self.uploaded_object_key)
                self._log("本次 TOS 临时深度视频已删除。")
                self.uploaded_object_key = ""
            except WorkflowError as exc:
                self._log(f"提示：{exc}")
        self._set_status("全部完成")
        self.root.after(0, lambda: messagebox.showinfo("生成完成", f"成片已保存到：\n{output}"))
        return output

    def _confirm_and_start_full(self) -> None:
        data = self._snapshot()
        if not self._confirm_paid("确认执行完整流程", data):
            return

        def job() -> None:
            depth_path = data["depth_output"]
            if depth_path and Path(depth_path).is_file():
                self._log("检测到现有深度视频，将直接复用。")
            else:
                depth_path = str(self._generate_depth(data))
            self._submit_and_wait(data, depth_path=depth_path)

        self._run_async("正在执行完整流程…", job)

    def _start_query(self) -> None:
        data = self._snapshot()
        task_id = data["task_id"]

        def job() -> None:
            if not task_id:
                raise WorkflowError("请填写任务 ID。")
            client = self._api_client()
            self._log(f"正在查询任务：{task_id}")
            task = client.get_task(task_id)
            status = str(task.get("status") or "unknown")
            self._log(f"当前状态：{status}")
            self._set_status(f"任务状态：{status}")
            if status != "succeeded":
                if status in {"queued", "running"}:
                    self._log("任务仍在运行；可稍后再次查询。")
                    return
                error = task.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise WorkflowError(f"任务未成功：{status}，{message or '未提供原因'}")
            video_url = str((task.get("content") or {}).get("video_url") or "")
            if not video_url:
                raise WorkflowError("成功任务中没有找到 video_url，可能已超过 24 小时有效期。")
            run_dir = timestamped_run_dir("download")
            self.last_output_dir = run_dir
            output = run_dir / f"生成成片_{task_id}.mp4"
            download_file(video_url, output)
            self._log(f"成片已下载：{output}")
            self._set_status("下载完成")

        self._run_async("正在查询任务…", job)

    def _open_last_output(self) -> None:
        path = self.last_output_dir or PROJECT_DIR / "runs"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))


def main() -> None:
    root = Tk()
    WorkflowApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

