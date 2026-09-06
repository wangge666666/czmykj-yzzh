const state = {
  config: null,
  sourceJobId: null,
  activeJobId: null,
  retrySceneJobId: null,
  recoverSeedanceJobId: null,
  sourceHasPersonReference: false,
  sourceHasClothingReference: false,
  pollTimer: null,
  scenePrompt: "",
  videoPrompt: "",
};

const $ = (selector) => document.querySelector(selector);

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3800);
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

function buildVideoPrompt() {
  return "参考@视频1，将原视频中的主角完整替换为@图片1中的人物形象，主角服装严格参考@图片2；原视频场景必须保留，背景、空间结构、固定陈设、色彩、材质、透视和环境光照严格参考@图片3，不得改变、扩建或重新设计场景；完全复刻原视频主角的全部动作、表情、视线和表演节奏，精准匹配脚步、抬手、摆臂、躯干倾斜、弹跳重心变化、转向、停顿与定格姿势，所有动作的起始时间、落点、强拍定格和节奏变化均与原视频帧级对齐，镜头运镜与画面构图完全复刻原视频；保持新人物身份和服装从头到尾稳定，禁止出现原人物面部、串脸、换装、重影或额外人物；@视频1仅作为深度、动作、遮挡和镜头约束，不要输出黑白深度风格，最终输出正常彩色视频。";
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
  ["#prepareBtn", "#generateBtn", "#fullBtn", "#recoverBtn", "#retrySceneBtn", "#recoverSeedanceBtn"].forEach((selector) => {
    $(selector).disabled = busy;
  });
}

function appendPreparationFields(form) {
  form.append("blur_range", $("#blurRange").value.trim());
  form.append("scene_prompt", $("#scenePrompt").value.trim());
  form.append("image_model", $("#imageModel").value.trim());
}

function appendReplacementFields(form, allowSavedReferences = false) {
  const person = $("#personImage").files[0];
  const asset = $("#personAsset").value.trim();
  const clothing = $("#clothingImage").files[0];
  const canReusePerson = allowSavedReferences && state.sourceHasPersonReference;
  const canReuseClothing = allowSavedReferences && state.sourceHasClothingReference;
  if (!person && !asset && !canReusePerson) throw new Error("请选择新人物图片，或填写授权人物素材 ID。 ");
  if (asset && !asset.startsWith("asset://")) throw new Error("授权人物素材 ID 必须以 asset:// 开头。 ");
  if (!clothing && !canReuseClothing) throw new Error("请选择新服装参考图。 ");
  if (asset) form.append("person_asset", asset);
  else if (person) form.append("person_image", person);
  if (clothing) form.append("clothing_image", clothing);
}

function appendGenerationFields(form, allowSavedReferences = false) {
  appendReplacementFields(form, allowSavedReferences);
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
    prepare: `即将调用 Seedream 5.0 付费生成原片干净场景\n\n模型：${$("#imageModel").value}\n同时会在本地生成深度视频。\n\n是否继续？`,
    generate: `即将调用 Seedance 2.0 付费生成最终人物替换成片\n\n输出：${$("#resolution").value} / ${$("#ratio").value} / ${$("#duration").value} 秒\n\n是否继续？`,
    full: `即将执行完整付费流程\n\n1. Seedream 5.0 提取原场景\n2. Seedance 2.0 生成人物替换成片\n输出：${$("#resolution").value} / ${$("#ratio").value} / ${$("#duration").value} 秒\n\n是否继续？`,
    retryScene: "上一次 Seedream 请求的连接在返回结果前中断，云端是否已完成无法确认。\n\n本次操作会发起一次新的 Seedream 付费请求，但会复用现有深度视频，不会重新转换深度，也不会自动提交 Seedance。\n\n确认重试吗？",
  };
  return window.confirm(messages[mode]);
}

async function prepareSource() {
  const video = $("#referenceVideo").files[0];
  if (!video) return toast("请先选择原视频。", true);
  if (!paidConfirm("prepare")) return;
  const form = new FormData();
  form.append("reference_video", video);
  appendPreparationFields(form);
  try {
    setBusy(true);
    $("#prepareState").textContent = "正在生成深度并提取原场景……";
    const job = await api("/api/person-only/prepare", { method: "POST", body: form });
    state.sourceJobId = job.id;
    monitorJob(job.id, true);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function generateFromPrepared() {
  try {
    if (!state.sourceJobId) throw new Error("请先生成深度视频和原片场景参考。 ");
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    appendGenerationFields(form, true);
    if (!paidConfirm("generate")) return;
    setBusy(true);
    const job = await api("/api/person-only/generate", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function createFull() {
  try {
    const video = $("#referenceVideo").files[0];
    if (!video) throw new Error("请选择原视频。 ");
    const form = new FormData();
    form.append("reference_video", video);
    appendPreparationFields(form);
    appendGenerationFields(form);
    if (!paidConfirm("full")) return;
    setBusy(true);
    const job = await api("/api/person-only/full", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function retrySceneExtraction() {
  const sourceJobId = state.retrySceneJobId;
  if (!sourceJobId) return toast("当前没有可安全重试的场景提取任务。", true);
  if (!paidConfirm("retryScene")) return;
  const form = new FormData();
  form.append("source_job_id", sourceJobId);
  form.append("scene_prompt", $("#scenePrompt").value.trim());
  form.append("image_model", $("#imageModel").value.trim());
  try {
    setBusy(true);
    const job = await api("/api/person-only/retry-scene", { method: "POST", body: form });
    state.retrySceneJobId = null;
    monitorJob(job.id, true);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function recoverSeedanceSubmission() {
  const sourceJobId = state.recoverSeedanceJobId;
  if (!sourceJobId) return toast("当前没有可安全找回的 Seedance 提交。", true);
  const form = new FormData();
  form.append("source_job_id", sourceJobId);
  form.append("model", $("#model").value.trim());
  form.append("resolution", $("#resolution").value);
  form.append("ratio", $("#ratio").value);
  form.append("duration", $("#duration").value);
  form.append("generate_audio", $("#generateAudio").checked ? "true" : "false");
  try {
    setBusy(true);
    const job = await api("/api/person-only/recover-seedance", { method: "POST", body: form });
    state.recoverSeedanceJobId = null;
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
    ? { description: "JPEG 图片", accept: { "image/jpeg": [".jpg", ".jpeg"] } }
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
    toast("正在保存，请稍候……");
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
  const labels = { person_prepare: "深度与场景准备", person_prepare_retry: "安全重试场景提取", person_generate: "人物替换成片", person_full: "一键人物替换", query: "恢复任务" };
  $("#jobKind").textContent = labels[job.kind] || "只更换人物";
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
  const canRetryScene = job.status === "failed" && job.has_depth && !job.has_scene && job.recovery_action === "retry_scene";
  $("#sceneRetryArea").classList.toggle("hidden", !canRetryScene);
  state.retrySceneJobId = canRetryScene ? job.id : null;
  const canRecoverSeedance = job.status === "failed" && job.recovery_action === "recover_seedance_submission";
  $("#seedanceRecoverArea").classList.toggle("hidden", !canRecoverSeedance);
  state.recoverSeedanceJobId = canRecoverSeedance ? job.id : null;
  state.sourceHasPersonReference = Boolean(job.has_person_reference);
  state.sourceHasClothingReference = Boolean(job.has_clothing_reference);

  const loadVideo = (selector, url, key) => {
    const video = $(selector);
    if (video.dataset.key === key) return;
    const separator = url.includes("?") ? "&" : "?";
    video.src = `${url}${separator}preview=${encodeURIComponent(key)}`;
    video.dataset.key = key;
    video.load();
  };
  $("#depthResultArea").classList.toggle("hidden", !job.has_depth);
  if (job.has_depth) {
    loadVideo("#depthResultVideo", job.depth_url, `${job.id}-depth-${job.created_at}`);
    $("#depthDownloadLink").href = `${job.depth_url}?download=1`;
    $("#depthDownloadLink").dataset.downloadUrl = job.depth_url;
    $("#depthDownloadLink").dataset.suggestedName = `人物替换_深度视频_${job.id}.mp4`;
  }

  $("#sceneResultArea").classList.toggle("hidden", !job.has_scene);
  if (job.has_scene) {
    const image = $("#sceneResultImage");
    const key = `${job.id}-scene-${job.created_at}`;
    if (image.dataset.key !== key) {
      image.src = `${job.scene_url}?preview=${encodeURIComponent(key)}`;
      image.dataset.key = key;
    }
    $("#sceneDownloadLink").href = `${job.scene_url}?download=1`;
    $("#sceneDownloadLink").dataset.downloadUrl = job.scene_url;
    $("#sceneDownloadLink").dataset.suggestedName = `原片干净场景_${job.id}.jpg`;
  }
  if (job.has_depth && job.has_scene) {
    state.sourceJobId = job.id;
    $("#prepareState").textContent = "深度视频与原片干净场景均已准备完成";
  }

  const seedanceVisible = !preparationOnly && ["person_generate", "person_full", "query"].includes(job.kind) && (job.has_output || job.task_id || job.cloud_status || job.progress >= 50);
  $("#finalResultArea").classList.toggle("hidden", !seedanceVisible);
  $("#finalResultVideo").classList.toggle("hidden", !job.has_output);
  $("#finalResultActions").classList.toggle("hidden", !job.has_output);
  $("#seedanceLivePreview").classList.toggle("hidden", job.has_output || !seedanceVisible);
  if (job.has_output) {
    loadVideo("#finalResultVideo", job.output_url, `${job.id}-output-${job.created_at}`);
    $("#finalDownloadLink").href = `${job.output_url}?download=1`;
    $("#finalDownloadLink").dataset.downloadUrl = job.output_url;
    $("#finalDownloadLink").dataset.suggestedName = `只更换人物成片_${job.id}.mp4`;
    $("#seedancePreviewCaption").textContent = "人物替换完成，可预览和下载";
  } else if (seedanceVisible) {
    const statusLabels = { queued: "Seedance 云端排队中", running: "Seedance 2.0 正在替换人物", succeeded: "生成完成，正在下载", failed: "Seedance 生成失败" };
    $("#seedanceProgressPercent").textContent = `${job.progress}%`;
    $("#seedanceProgressBar").style.width = `${job.progress}%`;
    $("#seedanceCloudStatus").textContent = statusLabels[job.cloud_status] || job.stage;
    $("#seedanceProgressNote").textContent = job.progress_estimated ? `官方状态已同步；百分比为阶段估算 · 已运行 ${job.cloud_elapsed_seconds || 0} 秒` : "正在同步任务阶段";
  }
  $("#resultArea").classList.toggle("hidden", !(job.has_depth || job.has_scene || seedanceVisible));
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
        if (job.status === "succeeded") toast(preparationOnly ? "深度视频与原片场景已准备完成。" : "人物替换成片已下载到本地。 ");
        else if (job.status === "submitted") toast("Seedance 已开始云端生成，可稍后使用任务 ID 恢复。 ");
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
    const job = await api("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: taskId }) });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    state.config = config;
    state.scenePrompt = config.scene_prompt;
    state.videoPrompt = buildVideoPrompt();
    $("#scenePrompt").value = state.scenePrompt;
    $("#prompt").value = state.videoPrompt;
    $("#model").value = config.model;
    $("#imageModel").value = config.image_model;
    setBadge("#arkBadge", config.ark_ready, "API 已配置", "API 未配置");
    setBadge("#channelBadge", config.temporary_upload_ready, "免费临时通道可用", "临时通道不可用");
    if (config.latest_cloud?.project === "person_only_replacement" && (config.latest_cloud.has_output || config.latest_cloud.task_id)) {
      state.activeJobId = config.latest_cloud.id;
      renderJob(config.latest_cloud);
    } else if (config.latest_person_submission?.has_depth) {
      state.activeJobId = config.latest_person_submission.id;
      renderJob(config.latest_person_submission);
    } else if (config.latest_person_retry?.has_depth) {
      state.activeJobId = config.latest_person_retry.id;
      renderJob(config.latest_person_retry, true);
    } else if (config.latest_scene?.has_scene) {
      if (config.latest_scene.has_depth) state.sourceJobId = config.latest_scene.id;
      state.activeJobId = config.latest_scene.id;
      renderJob({ ...config.latest_scene, kind: "person_prepare", task_id: "", cloud_status: "", has_output: false, output_url: "" }, true);
    }
  } catch (error) { toast(error.message, true); }
}

function initDropzone() {
  const zone = $("#videoDropzone");
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("dragging"); }));
  zone.addEventListener("drop", (event) => {
    if (!event.dataTransfer.files.length) return;
    const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]);
    $("#referenceVideo").files = transfer.files;
    $("#referenceVideo").dispatchEvent(new Event("change"));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig(); initDropzone();
  bindFile("#referenceVideo", "#videoFileName"); bindFile("#personImage", "#personFileName"); bindFile("#clothingImage", "#clothingFileName");
  $("#referenceVideo").addEventListener("change", () => videoDurationSuggestion($("#referenceVideo").files[0]));
  $("#resetScenePromptBtn").addEventListener("click", () => { $("#scenePrompt").value = state.scenePrompt; });
  $("#resetPromptBtn").addEventListener("click", () => { $("#prompt").value = state.videoPrompt; });
  $("#prepareBtn").addEventListener("click", prepareSource);
  $("#generateBtn").addEventListener("click", generateFromPrepared);
  $("#fullBtn").addEventListener("click", createFull);
  $("#retrySceneBtn").addEventListener("click", retrySceneExtraction);
  $("#recoverSeedanceBtn").addEventListener("click", recoverSeedanceSubmission);
  $("#recoverBtn").addEventListener("click", recoverTask);
  $("#depthDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#depthDownloadLink", "深度视频.mp4", "video"));
  $("#sceneDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#sceneDownloadLink", "原片干净场景.jpg", "image"));
  $("#finalDownloadLink").addEventListener("click", (event) => saveFromLink(event, "#finalDownloadLink", "只更换人物成片.mp4", "video"));
  $("#openFolderBtn").addEventListener("click", async () => {
    if (!state.activeJobId) return;
    try { await api(`/api/jobs/${state.activeJobId}/open-folder`, { method: "POST" }); }
    catch (error) { toast(error.message, true); }
  });
});
