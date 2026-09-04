const state = {
  config: null,
  prompt: "",
  depthJobId: null,
  activeJobId: null,
  pollTimer: null,
  personMode: "file",
  sourceDuration: 0,
  depthSourceDuration: 0,
  depthGenerationDuration: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3600);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(window.DepthFlowUI?.formatApiError?.(payload, response) || payload.error || payload.message || `请求失败：${response.status}`);
  return payload;
}

function setBadge(selector, ready, readyText, missingText) {
  const badge = $(selector);
  if (!badge) return;
  badge.className = `badge ${ready ? "ready" : "missing"}`;
  badge.innerHTML = `<i></i>${ready ? readyText : missingText}`;
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    state.config = config;
    state.prompt = config.prompt;
    $("#prompt").value = config.prompt;
    $("#model").value = config.model;
    setBadge("#arkBadge", config.ark_ready, "API 已配置", "API 未配置");
    setBadge(
      "#tosBadge",
      config.temporary_upload_ready,
      "免费临时通道可用",
      "上传通道不可用"
    );
    if (config.latest_depth?.has_depth) {
      state.depthJobId = config.latest_depth.id;
      state.activeJobId = config.latest_depth.id;
      $("#depthState").textContent = "已找到最近生成的合规深度视频，可直接复用";
      renderJob(config.latest_depth, true);
    } else if (!config.tos_ready) {
      $("#depthState").textContent = "等待参考视频；输出自动限制为 14.5 秒";
    }
  } catch (error) {
    toast(error.message, true);
  }
}

function bindFile(inputSelector, labelSelector) {
  const input = $(inputSelector);
  input.addEventListener("change", () => {
    const file = input.files[0];
    if (file) $(labelSelector).textContent = file.name;
  });
}

function videoDurationSuggestion(file) {
  if (!file) return;
  const video = document.createElement("video");
  const url = URL.createObjectURL(file);
  video.preload = "metadata";
  video.onloadedmetadata = () => {
    const rawDuration = video.duration || 5;
    const seconds = Math.min(15, Math.max(4, Math.floor(Math.min(rawDuration, 14.5) + 0.5)));
    state.sourceDuration = rawDuration;
    $("#duration").value = seconds;
    $("#durationHint").textContent = `原片 ${rawDuration.toFixed(2)} 秒 → Seedance 自动选择 ${seconds} 秒`;
    URL.revokeObjectURL(url);
  };
  video.src = url;
}

function setBusy(busy) {
  ["#depthBtn", "#reuseDepthBtn", "#fullBtn", "#recoverBtn"].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = busy;
  });
}

function validateAssets() {
  if (state.personMode === "file" && !$("#personImage").files[0]) throw new Error("请选择人物形象图片。");
  if (state.personMode === "asset" && !$("#personAsset").value.trim().startsWith("asset://")) throw new Error("请输入以 asset:// 开头的授权人物素材 ID。");
  if (!$("#clothingImage").files[0]) throw new Error("请选择服装参考图。");
  if (!$("#sceneImage").files[0]) throw new Error("请选择场景参考图。");
}

function appendSharedFields(form) {
  if (state.personMode === "file") form.append("person_image", $("#personImage").files[0]);
  else form.append("person_asset", $("#personAsset").value.trim());
  form.append("clothing_image", $("#clothingImage").files[0]);
  form.append("scene_image", $("#sceneImage").files[0]);
  form.append("prompt", $("#prompt").value.trim());
  form.append("generation_channel", $("#generationChannel").value);
  form.append("model", $("#model").value.trim());
  form.append("resolution", $("#resolution").value);
  form.append("ratio", $("#ratio").value);
  form.append("duration", $("#duration").value);
  form.append("generate_audio", $("#generateAudio").checked ? "true" : "false");
  form.append("watermark", $("#watermark").checked ? "true" : "false");
  form.append("delete_tos_after", $("#deleteTos").checked ? "true" : "false");
}

function paidConfirm() {
  return window.confirm(
    `即将通过 Seedance API 提交付费任务\n\n模型：${$("#model").value}\n输出：${$("#resolution").value} / ${$("#ratio").value} / ${$("#duration").value} 秒\n\n深度视频会临时公开，并在任务结束后立即删除。生成模型会尽量匹配动作和运镜，但不能保证数学意义上的逐帧完全一致。是否继续？`
  );
}

async function createDepth() {
  const video = $("#referenceVideo").files[0];
  if (!video) return toast("请先选择参考视频。", true);
  const form = new FormData();
  form.append("reference_video", video);
  form.append("blur_range", $("#blurRange").value.trim());
  try {
    setBusy(true);
    $("#depthState").textContent = "正在上传并开始处理…";
    const job = await api("/api/depth", { method: "POST", body: form });
    state.depthJobId = job.id;
    monitorJob(job.id, true);
  } catch (error) {
    setBusy(false);
    toast(error.message, true);
  }
}

async function createFull() {
  try {
    const video = $("#referenceVideo").files[0];
    if (!video) throw new Error("请选择原视频；如果要复用已有深度视频，请点击左侧按钮。");
    validateAssets();
    if (!paidConfirm()) return;
    const form = new FormData();
    form.append("reference_video", video);
    form.append("blur_range", $("#blurRange").value.trim());
    appendSharedFields(form);
    setBusy(true);
    const job = await api("/api/full", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) {
    setBusy(false);
    toast(error.message, true);
  }
}

async function createFromDepth() {
  try {
    if (!state.depthJobId) throw new Error("当前没有可用的深度视频，请先执行功能一。");
    validateAssets();
    if (state.depthGenerationDuration) {
      $("#duration").value = state.depthGenerationDuration;
      const sourceText = state.depthSourceDuration
        ? `当前深度参考 ${Number(state.depthSourceDuration).toFixed(2)} 秒`
        : "当前深度参考";
      $("#durationHint").textContent = `${sourceText} → Seedance 自动选择 ${state.depthGenerationDuration} 秒`;
    }
    if (!paidConfirm()) return;
    const form = new FormData();
    form.append("depth_job_id", state.depthJobId);
    appendSharedFields(form);
    setBusy(true);
    const job = await api("/api/generate", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) {
    setBusy(false);
    toast(error.message, true);
  }
}

async function saveVideoFromLink(event, linkSelector, defaultName) {
  event.preventDefault();
  const link = $(linkSelector);
  const url = link.dataset.downloadUrl;
  if (!url) return toast("视频文件尚未生成。", true);

  const suggestedName = link.dataset.suggestedName || defaultName;
  if (typeof window.showSaveFilePicker !== "function") {
    const fallback = document.createElement("a");
    fallback.href = `${url}${url.includes("?") ? "&" : "?"}download=1`;
    fallback.download = suggestedName;
    document.body.appendChild(fallback);
    fallback.click();
    fallback.remove();
    toast("当前浏览器不支持选择完整保存路径，已使用浏览器默认下载。", true);
    return;
  }

  try {
    // This call must happen directly from the click gesture or Chromium will
    // block the native Save As dialog.
    const handle = await window.showSaveFilePicker({
      suggestedName,
      types: [{
        description: "MP4 视频",
        accept: { "video/mp4": [".mp4"] },
      }],
    });
    toast("正在保存视频，请稍候……");
    const response = await fetch(url);
    if (!response.ok) {
      let message = `下载失败（HTTP ${response.status}）`;
      try {
        const payload = await response.json();
        message = payload.error || payload.message || message;
      } catch (_error) {
        // The response may be a plain-text proxy error.
      }
      throw new Error(message);
    }
    const writable = await handle.createWritable();
    if (response.body) await response.body.pipeTo(writable);
    else {
      await writable.write(await response.blob());
      await writable.close();
    }
    toast(`视频已保存：${handle.name}`);
  } catch (error) {
    if (error?.name === "AbortError") return toast("已取消保存。 ");
    toast(error.message || "保存视频失败。", true);
  }
}

function renderJob(job, isDepthOnly = false) {
  $("#emptyTask").classList.add("hidden");
  $("#activeTask").classList.remove("hidden");
  $("#jobKind").textContent = job.kind === "full" ? "完整流程" : job.kind === "depth" ? "深度转换" : job.kind === "query" ? "恢复任务" : "成片生成";
  $("#jobStage").textContent = job.stage;
  $("#cloudTaskId").textContent = job.task_id ? `Seedance 任务 ID：${job.task_id}` : `本地任务：${job.id}`;
  $("#progressPercent").textContent = `${job.progress}%`;
  $("#progressBar").style.width = `${job.progress}%`;
  $("#jobLogs").textContent = job.logs.join("\n");
  $("#jobLogs").scrollTop = $("#jobLogs").scrollHeight;

  if (job.generation_duration) {
    $("#duration").value = job.generation_duration;
    const sourceText = job.source_duration ? `参考视频 ${Number(job.source_duration).toFixed(2)} 秒` : "参考视频";
    $("#durationHint").textContent = `${sourceText} → Seedance 自动选择 ${job.generation_duration} 秒`;
  }

  const loadPreviewVideo = (videoSelector, url, key) => {
    const video = $(videoSelector);
    if (video.dataset.key !== key) {
      const separator = url.includes("?") ? "&" : "?";
      video.src = `${url}${separator}preview=${encodeURIComponent(key)}`;
      video.dataset.key = key;
      video.dataset.rawUrl = url;
      video.load();
      window.setTimeout(() => {
        if (video.dataset.key !== key || video.readyState !== 0) return;
        video.src = `${url}${separator}preview=${encodeURIComponent(key)}-${Date.now()}`;
        video.load();
      }, 2800);
    }
  };

  $("#depthResultArea").classList.toggle("hidden", !job.has_depth);
  if (job.has_depth) {
    loadPreviewVideo("#depthResultVideo", job.depth_url, `${job.id}-depth-${job.created_at}`);
    $("#depthDownloadLink").href = `${job.depth_url}?download=1`;
    $("#depthDownloadLink").dataset.downloadUrl = job.depth_url;
    $("#depthDownloadLink").dataset.suggestedName = `深度视频_${job.id}.mp4`;
  }

  const seedanceVisible = job.kind !== "depth" && (
    job.has_output || job.task_id || job.cloud_status || job.progress >= 48
  );
  $("#finalResultArea").classList.toggle("hidden", !seedanceVisible);
  $("#finalResultVideo").classList.toggle("hidden", !job.has_output);
  $("#finalResultActions").classList.toggle("hidden", !job.has_output);
  $("#seedanceLivePreview").classList.toggle("hidden", job.has_output || !seedanceVisible);

  if (job.has_output) {
    loadPreviewVideo("#finalResultVideo", job.output_url, `${job.id}-output-${job.created_at}`);
    $("#finalDownloadLink").href = `${job.output_url}?download=1`;
    $("#finalDownloadLink").dataset.downloadUrl = job.output_url;
    $("#finalDownloadLink").dataset.suggestedName = `最终成片_${job.id}.mp4`;
    $("#seedancePreviewCaption").textContent = "生成完成，可直接预览和下载";
  } else if (seedanceVisible) {
    const statusLabels = {
      queued: "Seedance 云端排队中",
      running: "Seedance 2.0 正在生成",
      succeeded: "Seedance 生成完成，正在下载",
      failed: "Seedance 生成失败",
    };
    $("#seedanceProgressPercent").textContent = `${job.progress}%`;
    $("#seedanceProgressBar").style.width = `${job.progress}%`;
    $("#seedanceCloudStatus").textContent = statusLabels[job.cloud_status] || job.stage;
    $("#seedancePreviewCaption").textContent = job.task_id ? "Seedance 云端状态已同步" : "正在准备参考素材";
    const elapsed = Number(job.cloud_elapsed_seconds || 0);
    const elapsedText = elapsed >= 60 ? ` · 已运行 ${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒` : elapsed > 0 ? ` · 已运行 ${elapsed} 秒` : "";
    $("#seedanceProgressNote").textContent = job.progress_estimated
      ? `官方状态已同步；百分比为阶段估算${elapsedText}`
      : `正在同步任务阶段${elapsedText}`;
  }

  $("#resultArea").classList.toggle("hidden", !(job.has_depth || seedanceVisible));
  if (job.has_depth) {
    state.depthJobId = job.id;
    state.depthSourceDuration = Number(job.depth_duration || job.source_duration || 0);
    state.depthGenerationDuration = Number(job.generation_duration || 0);
    $("#depthState").textContent = job.kind === "full" && !job.has_output
      ? "深度视频已完成并可预览，正在继续生成成片"
      : "深度视频已完成并可预览";
  }
}

function monitorJob(jobId, isDepthOnly = false) {
  state.activeJobId = jobId;
  clearInterval(state.pollTimer);
  const poll = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      renderJob(job, isDepthOnly);
      if (isDepthOnly) $("#depthState").textContent = job.stage;
      if (["succeeded", "failed", "submitted"].includes(job.status)) {
        clearInterval(state.pollTimer);
        setBusy(false);
        if (job.status === "succeeded") {
          toast(isDepthOnly ? "深度视频生成完成。" : "任务完成，成片已下载到本机。 ");
        } else if (job.status === "submitted") {
          toast("Seedance 已开始云端生成，可使用任务 ID 恢复查询。");
        } else toast(window.DepthFlowUI?.formatJobError?.(job) || job.error || "任务失败。", true);
      }
    } catch (error) {
      clearInterval(state.pollTimer);
      setBusy(false);
      toast(error.message, true);
    }
  };
  poll();
  state.pollTimer = setInterval(poll, 1800);
  $(".task-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function recoverTask() {
  const taskId = $("#recoverTaskId").value.trim();
  if (!taskId) return toast("请输入 Seedance 任务 ID。", true);
  try {
    setBusy(true);
    const job = await api("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    });
    monitorJob(job.id);
  } catch (error) {
    setBusy(false);
    toast(error.message, true);
  }
}

async function checkApi() {
  try {
    $("#checkApiBtn").disabled = true;
    const result = await api("/api/check", { method: "POST" });
    setBadge("#arkBadge", result.ok, "API 可用", "API 不可用");
    setBadge(
      "#tosBadge",
      result.temporary_upload_ready,
      "免费临时通道可用",
      "上传通道不可用"
    );
    toast(result.message);
  } catch (error) {
    setBadge("#arkBadge", false, "API 可用", "API 不可用");
    toast(error.message, true);
  } finally {
    $("#checkApiBtn").disabled = false;
  }
}

function initDropzone() {
  const zone = $("#videoDropzone");
  ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
    event.preventDefault();
    zone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
  }));
  zone.addEventListener("drop", (event) => {
    const files = event.dataTransfer.files;
    if (!files.length) return;
    const transfer = new DataTransfer();
    transfer.items.add(files[0]);
    $("#referenceVideo").files = transfer.files;
    $("#referenceVideo").dispatchEvent(new Event("change"));
  });
}

function initPersonTabs() {
  $$('[data-person-mode]').forEach((button) => button.addEventListener("click", () => {
    state.personMode = button.dataset.personMode;
    $$('[data-person-mode]').forEach((item) => item.classList.toggle("active", item === button));
    $("#personFileBox").classList.toggle("hidden", state.personMode !== "file");
    $("#personAssetBox").classList.toggle("hidden", state.personMode !== "asset");
  }));
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig();
  initDropzone();
  initPersonTabs();
  bindFile("#referenceVideo", "#videoFileName");
  bindFile("#personImage", "#personFileName");
  bindFile("#clothingImage", "#clothingFileName");
  bindFile("#sceneImage", "#sceneFileName");
  $("#referenceVideo").addEventListener("change", () => videoDurationSuggestion($("#referenceVideo").files[0]));
  $("#depthBtn").addEventListener("click", createDepth);
  $("#reuseDepthBtn").addEventListener("click", createFromDepth);
  $("#fullBtn").addEventListener("click", createFull);
  $("#recoverBtn").addEventListener("click", recoverTask);
  $("#checkApiBtn")?.addEventListener("click", checkApi);
  $("#depthDownloadLink").addEventListener("click", (event) => {
    saveVideoFromLink(event, "#depthDownloadLink", "深度视频.mp4");
  });
  $("#finalDownloadLink").addEventListener("click", (event) => {
    saveVideoFromLink(event, "#finalDownloadLink", "最终成片.mp4");
  });
  $("#resetPromptBtn").addEventListener("click", () => { $("#prompt").value = state.prompt; toast("已恢复推荐提示词。 "); });
  $("#openFolderBtn").addEventListener("click", async () => {
    if (!state.activeJobId) return;
    try { await api(`/api/jobs/${state.activeJobId}/open-folder`, { method: "POST" }); }
    catch (error) { toast(error.message, true); }
  });
});
