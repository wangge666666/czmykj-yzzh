const state = {
  config: null,
  sourceJobId: null,
  activeJobId: null,
  retrySubjectJobId: null,
  recoverSeedanceJobId: null,
  sourceHasScene: false,
  pollTimer: null,
  personPrompt: "",
  clothingPrompt: "",
  videoPrompt: "",
};

const $ = (selector) => document.querySelector(selector);

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 4200);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try { payload = await response.json(); } catch (_error) { payload = {}; }
  if (!response.ok) throw new Error(window.DepthFlowUI?.formatApiError?.(payload, response) || payload.error || payload.message || `请求失败（HTTP ${response.status}）`);
  return payload;
}

function setBadge(selector, ready, readyText, missingText) {
  const badge = $(selector);
  badge.className = `badge ${ready ? "ready" : "missing"}`;
  badge.innerHTML = `<i></i>${ready ? readyText : missingText}`;
}

function bindFile(inputSelector, labelSelector) {
  $(inputSelector).addEventListener("change", () => {
    const file = $(inputSelector).files[0];
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
    $("#duration").value = seconds;
    $("#durationHint").textContent = `原片 ${rawDuration.toFixed(2)} 秒 → Seedance 自动选择 ${seconds} 秒`;
    URL.revokeObjectURL(url);
  };
  video.src = url;
}

function setBusy(busy) {
  ["#prepareBtn", "#generateBtn", "#fullBtn", "#recoverBtn", "#retrySubjectBtn", "#recoverSeedanceBtn"].forEach((selector) => {
    $(selector).disabled = busy;
  });
}

function appendExtractionFields(form) {
  form.append("blur_range", $("#blurRange").value.trim());
  form.append("person_prompt", $("#personPrompt").value.trim());
  form.append("clothing_prompt", $("#clothingPrompt").value.trim());
  form.append("image_model", $("#imageModel").value.trim());
}

function appendTargetScene(form, allowSavedScene = false) {
  const scene = $("#sceneImage").files[0];
  if (!scene && !(allowSavedScene && state.sourceHasScene)) throw new Error("请选择需要替换的新场景图。 ");
  if (scene) form.append("scene_image", scene);
}

function appendGenerationFields(form) {
  const personAsset = $("#personAsset")?.value.trim() || "";
  if (personAsset) form.append("person_asset", personAsset);
  form.append("prompt", $("#prompt").value.trim());
  form.append("generation_channel", "api");
  form.append("model", $("#model").value.trim());
  form.append("resolution", $("#resolution").value);
  form.append("ratio", $("#ratio").value);
  form.append("duration", $("#duration").value);
  form.append("generate_audio", $("#generateAudio").checked ? "true" : "false");
  form.append("watermark", $("#watermark").checked ? "true" : "false");
  form.append("delete_tos_after", $("#deleteTos").checked ? "true" : "false");
  form.append("depth_reference", $("#depthReference").value.trim());
}

function paidConfirm(mode) {
  const messages = {
    prepare: `即将调用 Seedream 5.0 两次\n\n1. 生成 16:9 原人物三视图\n2. 生成白底纯服装三视图（无模特/人台/玩偶）\n同时会在本地生成深度视频。\n\n是否继续？`,
    generate: `即将调用 Seedance 2.0 付费生成场景替换成片\n\n输出：${$("#resolution").value} / ${$("#ratio").value} / ${$("#duration").value} 秒\n\n是否继续？`,
    full: `即将执行完整付费流程\n\n1. Seedream 生成 16:9 人物三视图\n2. Seedream 生成白底纯服装三视图\n3. Seedance 生成场景替换成片\n\n是否继续？`,
    retry: "将只补充缺失的人物或服装三视图。已经生成成功的结果和深度视频会直接复用。\n\n这可能发起一至两次新的 Seedream 付费请求，是否继续？",
  };
  return window.confirm(messages[mode]);
}

async function prepareSource() {
  const video = $("#referenceVideo").files[0];
  if (!video) return toast("请先选择原视频。", true);
  if (!paidConfirm("prepare")) return;
  const form = new FormData();
  form.append("reference_video", video);
  appendExtractionFields(form);
  try {
    setBusy(true);
    $("#prepareState").textContent = "正在生成深度并提取人物服装三视图……";
    const job = await api("/api/scene-only/prepare", { method: "POST", body: form });
    state.sourceJobId = job.id;
    monitorJob(job.id, true);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function generateFromPrepared() {
  try {
    if (!state.sourceJobId) throw new Error("请先生成深度视频与人物服装三视图。 ");
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    appendTargetScene(form, true);
    appendGenerationFields(form);
    if (!paidConfirm("generate")) return;
    setBusy(true);
    const job = await api("/api/scene-only/generate", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function createFull() {
  try {
    const video = $("#referenceVideo").files[0];
    if (!video) throw new Error("请选择原视频。 ");
    const form = new FormData();
    form.append("reference_video", video);
    appendTargetScene(form);
    appendExtractionFields(form);
    appendGenerationFields(form);
    if (!paidConfirm("full")) return;
    setBusy(true);
    const job = await api("/api/scene-only/full", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function retrySubjectExtraction() {
  if (!state.retrySubjectJobId) return toast("当前没有可重试的人物服装提取。", true);
  if (!paidConfirm("retry")) return;
  const form = new FormData();
  form.append("source_job_id", state.retrySubjectJobId);
  form.append("person_prompt", $("#personPrompt").value.trim());
  form.append("clothing_prompt", $("#clothingPrompt").value.trim());
  form.append("image_model", $("#imageModel").value.trim());
  try {
    setBusy(true);
    const job = await api("/api/scene-only/retry-subject", { method: "POST", body: form });
    monitorJob(job.id, true);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function recoverSeedanceSubmission() {
  if (!state.recoverSeedanceJobId) return toast("当前没有可找回的 Seedance 提交。", true);
  const form = new FormData();
  form.append("source_job_id", state.recoverSeedanceJobId);
  form.append("model", $("#model").value.trim());
  form.append("resolution", $("#resolution").value);
  form.append("ratio", $("#ratio").value);
  form.append("duration", $("#duration").value);
  form.append("generate_audio", $("#generateAudio").checked ? "true" : "false");
  try {
    setBusy(true);
    const job = await api("/api/scene-only/recover-seedance", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function saveFromLink(event, selector, defaultName, kind) {
  event.preventDefault();
  const link = $(selector);
  const url = link.dataset.downloadUrl;
  if (!url) return toast("文件尚未生成。", true);
  const suggestedName = link.dataset.suggestedName || defaultName;
  const pickerType = kind === "image"
    ? { description: "图片", accept: { "image/jpeg": [".jpg", ".jpeg"], "image/png": [".png"] } }
    : { description: "MP4 视频", accept: { "video/mp4": [".mp4"] } };
  if (typeof window.showSaveFilePicker !== "function") {
    const fallback = document.createElement("a");
    fallback.href = `${url}${url.includes("?") ? "&" : "?"}download=1`;
    fallback.download = suggestedName;
    fallback.click();
    return toast("当前浏览器不支持选择完整路径，已使用默认下载。", true);
  }
  try {
    const handle = await window.showSaveFilePicker({ suggestedName, types: [pickerType] });
    const response = await fetch(url);
    if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`);
    const writable = await handle.createWritable();
    if (response.body) await response.body.pipeTo(writable);
    else { await writable.write(await response.blob()); await writable.close(); }
    toast(`文件已保存：${handle.name}`);
  } catch (error) {
    if (error?.name === "AbortError") return toast("已取消保存。 ");
    toast(error.message || "保存失败。", true);
  }
}

function renderJob(job, preparationOnly = false) {
  $("#emptyTask").classList.add("hidden");
  $("#activeTask").classList.remove("hidden");
  const labels = { scene_prepare: "深度与人物服装准备", scene_prepare_retry: "安全重试人物服装提取", scene_generate: "只更换场景成片", scene_full: "一键场景替换", query: "恢复任务" };
  $("#jobKind").textContent = labels[job.kind] || "只更换场景";
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

  const retrySubject = job.status === "failed" && job.recovery_action === "retry_scene_subject";
  $("#subjectRetryArea").classList.toggle("hidden", !retrySubject);
  state.retrySubjectJobId = retrySubject ? job.id : null;
  const recoverSeedance = job.status === "failed" && job.recovery_action === "recover_seedance_submission";
  $("#seedanceRecoverArea").classList.toggle("hidden", !recoverSeedance);
  state.recoverSeedanceJobId = recoverSeedance ? job.id : null;

  const loadVideo = (selector, url, key) => {
    const video = $(selector);
    if (video.dataset.key === key) return;
    video.src = `${url}${url.includes("?") ? "&" : "?"}preview=${encodeURIComponent(key)}`;
    video.dataset.key = key;
    video.load();
  };
  const loadImage = (selector, url, key) => {
    const image = $(selector);
    if (image.dataset.key === key) return;
    image.src = `${url}${url.includes("?") ? "&" : "?"}preview=${encodeURIComponent(key)}`;
    image.dataset.key = key;
  };
  const bindDownload = (selector, url, name) => {
    const link = $(selector);
    link.href = `${url}?download=1`;
    link.dataset.downloadUrl = url;
    link.dataset.suggestedName = name;
  };

  $("#depthResultArea").classList.toggle("hidden", !job.has_depth);
  if (job.has_depth) {
    loadVideo("#depthResultVideo", job.depth_url, `${job.id}-depth-${job.created_at}`);
    bindDownload("#depthDownloadLink", job.depth_url, `项目4_深度视频_${job.id}.mp4`);
  }
  $("#personResultArea").classList.toggle("hidden", !job.has_person_reference);
  if (job.has_person_reference) {
    loadImage("#personResultImage", job.person_url, `${job.id}-person-${job.created_at}`);
    bindDownload("#personDownloadLink", job.person_url, `原人物三视图_${job.id}.jpg`);
  }
  $("#clothingResultArea").classList.toggle("hidden", !job.has_clothing_reference);
  if (job.has_clothing_reference) {
    loadImage("#clothingResultImage", job.clothing_url, `${job.id}-clothing-${job.created_at}`);
    bindDownload("#clothingDownloadLink", job.clothing_url, `原服装三视图_${job.id}.jpg`);
  }
  $("#sceneResultArea").classList.toggle("hidden", !job.has_scene);
  if (job.has_scene) {
    loadImage("#sceneResultImage", job.scene_url, `${job.id}-scene-${job.created_at}`);
    bindDownload("#sceneDownloadLink", job.scene_url, `新场景参考_${job.id}.jpg`);
  }
  if (job.has_depth && job.has_person_reference && job.has_clothing_reference) {
    state.sourceJobId = job.id;
    state.sourceHasScene = Boolean(job.has_scene);
    $("#prepareState").textContent = "深度、原人物与原服装三视图均已准备完成";
  }

  const seedanceVisible = !preparationOnly && ["scene_generate", "scene_full", "query"].includes(job.kind) && (job.has_output || job.task_id || job.cloud_status || job.progress >= 60);
  $("#finalResultArea").classList.toggle("hidden", !seedanceVisible);
  $("#finalResultVideo").classList.toggle("hidden", !job.has_output);
  $("#finalResultActions").classList.toggle("hidden", !job.has_output);
  $("#seedanceLivePreview").classList.toggle("hidden", job.has_output || !seedanceVisible);
  if (job.has_output) {
    loadVideo("#finalResultVideo", job.output_url, `${job.id}-output-${job.created_at}`);
    bindDownload("#finalDownloadLink", job.output_url, `只更换场景成片_${job.id}.mp4`);
    $("#seedancePreviewCaption").textContent = "场景替换完成，可预览和下载";
  } else if (seedanceVisible) {
    const labelsByStatus = { queued: "Seedance 云端排队中", running: "Seedance 2.0 正在替换场景", succeeded: "生成完成，正在下载", failed: "Seedance 生成失败" };
    $("#seedanceProgressPercent").textContent = `${job.progress}%`;
    $("#seedanceProgressBar").style.width = `${job.progress}%`;
    $("#seedanceCloudStatus").textContent = labelsByStatus[job.cloud_status] || job.stage;
    $("#seedanceProgressNote").textContent = job.progress_estimated ? `官方状态已同步；百分比为阶段估算 · 已运行 ${job.cloud_elapsed_seconds || 0} 秒` : "正在同步任务阶段";
  }
  const anyResult = job.has_depth || job.has_person_reference || job.has_clothing_reference || job.has_scene || seedanceVisible;
  $("#resultArea").classList.toggle("hidden", !anyResult);
}

function monitorJob(jobId, preparationOnly = false) {
  state.activeJobId = jobId;
  clearInterval(state.pollTimer);
  const poll = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      renderJob(job, preparationOnly);
      if (["succeeded", "failed", "submitted"].includes(job.status)) {
        clearInterval(state.pollTimer);
        setBusy(false);
        if (job.status === "succeeded") toast(preparationOnly ? "深度与人物服装三视图已准备完成。" : "场景替换成片已下载到本地。 ");
        else if (job.status === "submitted") toast("Seedance 已开始云端生成。 ");
        else toast(window.DepthFlowUI?.formatJobError?.(job) || job.error || "任务失败。", true);
      }
    } catch (error) { clearInterval(state.pollTimer); setBusy(false); toast(error.message, true); }
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
    const job = await api("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: taskId, project: "scene_only_replacement", source_job_id: state.sourceJobId || "" }) });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    state.config = config;
    state.personPrompt = config.person_triview_prompt;
    state.clothingPrompt = config.clothing_triview_prompt;
    state.videoPrompt = config.scene_only_prompt;
    $("#personPrompt").value = state.personPrompt;
    $("#clothingPrompt").value = state.clothingPrompt;
    $("#prompt").value = state.videoPrompt;
    $("#model").value = config.model;
    $("#imageModel").value = config.image_model;
    setBadge("#arkBadge", config.ark_ready, "API 已配置", "API 未配置");
    setBadge("#channelBadge", config.temporary_upload_ready, "免费临时通道可用", "临时通道不可用");
    if (config.latest_cloud?.project === "scene_only_replacement" && (config.latest_cloud.has_output || config.latest_cloud.task_id)) {
      state.activeJobId = config.latest_cloud.id;
      renderJob(config.latest_cloud);
    } else if (config.latest_scene_only?.has_depth) {
      state.activeJobId = config.latest_scene_only.id;
      renderJob(config.latest_scene_only, config.latest_scene_only.recovery_action !== "recover_seedance_submission");
    }
  } catch (error) { toast(error.message, true); }
}

function initDropzones() {
  const videoZone = $("#videoDropzone");
  const sceneZone = $("#sceneDropzone");
  [[videoZone, "#referenceVideo"], [sceneZone, "#sceneImage"]].forEach(([zone, inputSelector]) => {
    ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("dragging"); }));
    ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("dragging"); }));
    zone.addEventListener("drop", (event) => {
      if (!event.dataTransfer.files.length) return;
      const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]);
      $(inputSelector).files = transfer.files;
      $(inputSelector).dispatchEvent(new Event("change"));
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig(); initDropzones();
  bindFile("#referenceVideo", "#videoFileName"); bindFile("#sceneImage", "#sceneFileName");
  $("#referenceVideo").addEventListener("change", () => videoDurationSuggestion($("#referenceVideo").files[0]));
  $("#resetPersonPromptBtn").addEventListener("click", () => { $("#personPrompt").value = state.personPrompt; });
  $("#resetClothingPromptBtn").addEventListener("click", () => { $("#clothingPrompt").value = state.clothingPrompt; });
  $("#resetPromptBtn").addEventListener("click", () => { $("#prompt").value = state.videoPrompt; });
  $("#prepareBtn").addEventListener("click", prepareSource);
  $("#generateBtn").addEventListener("click", generateFromPrepared);
  $("#fullBtn").addEventListener("click", createFull);
  $("#retrySubjectBtn").addEventListener("click", retrySubjectExtraction);
  $("#recoverSeedanceBtn").addEventListener("click", recoverSeedanceSubmission);
  $("#recoverBtn").addEventListener("click", recoverTask);
  $("#depthDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#depthDownloadLink", "项目4_深度视频.mp4", "video"));
  $("#personDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#personDownloadLink", "原人物三视图.jpg", "image"));
  $("#clothingDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#clothingDownloadLink", "原服装三视图.jpg", "image"));
  $("#sceneDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#sceneDownloadLink", "新场景参考.jpg", "image"));
  $("#finalDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#finalDownloadLink", "只更换场景成片.mp4", "video"));
  $("#openFolderBtn").addEventListener("click", async () => {
    if (!state.activeJobId) return;
    try { await api(`/api/jobs/${state.activeJobId}/open-folder`, { method: "POST" }); }
    catch (error) { toast(error.message, true); }
  });
});
