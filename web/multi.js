const state = {
  config: null,
  actorCount: 0,
  depthJobId: null,
  activeJobId: null,
  pollTimer: null,
  depthSourceDuration: 0,
  depthGenerationDuration: 0,
};

const $ = (selector) => document.querySelector(selector);
const defaultRoles = ["画面左侧人物", "画面右侧人物", "画面中间人物", "画面后方人物"];

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

function actorTemplate(index) {
  const personImage = index * 2 - 1;
  const clothingImage = index * 2;
  return `
    <article class="actor-card" data-actor-index="${index}">
      <div class="actor-card-head">
        <div><span class="actor-index">${String(index).padStart(2, "0")}</span><b>人物 ${index}</b><small>原片人物与新素材一一绑定</small></div>
        <span class="actor-map">@图片 ${personImage} 人物 · @图片 ${clothingImage} 服装</span>
      </div>
      <div class="actor-body">
        <div class="field role-field">
          <label for="roleDescription${index}">原片角色定位</label>
          <input id="roleDescription${index}" type="text" maxlength="200" value="${defaultRoles[index - 1]}">
          <span class="role-help">描述这个人在原片中的位置或特征，避免角色对应错误。</span>
        </div>
        <div class="actor-asset">
          <div class="actor-asset-title"><span>@图片 ${personImage}</span><b>新人物形象</b></div>
          <label class="mini-drop" for="personImage${index}">
            <input id="personImage${index}" type="file" accept="image/*,.heic,.heif">
            <span>选择人物图片</span><small id="personFileName${index}">虚构 / AI 角色图片</small>
          </label>
          <div class="asset-or">或使用已授权真人素材 ID</div>
          <input id="personAsset${index}" type="text" placeholder="asset://asset-...">
        </div>
        <div class="actor-asset">
          <div class="actor-asset-title"><span>@图片 ${clothingImage}</span><b>对应服装</b></div>
          <label class="mini-drop" for="clothingImage${index}">
            <input id="clothingImage${index}" type="file" accept="image/*,.heic,.heif">
            <span>选择服装图片</span><small id="clothingFileName${index}">建议无清晰真人面部</small>
          </label>
        </div>
      </div>
    </article>`;
}

function bindActor(index) {
  $(`#roleDescription${index}`).addEventListener("input", updatePrompt);
  $(`#personImage${index}`).addEventListener("change", () => {
    const file = $(`#personImage${index}`).files[0];
    if (file) $(`#personFileName${index}`).textContent = file.name;
  });
  $(`#clothingImage${index}`).addEventListener("change", () => {
    const file = $(`#clothingImage${index}`).files[0];
    if (file) $(`#clothingFileName${index}`).textContent = file.name;
  });
}

function updateReferenceMap() {
  const sceneImage = state.actorCount * 2 + 1;
  $("#referenceCount").textContent = `${sceneImage} / 9 张参考图`;
  $("#sceneReferenceLabel").textContent = `@图片 ${sceneImage}`;
  $("#addActorBtn").disabled = state.actorCount >= 4;
  $("#removeActorBtn").disabled = state.actorCount <= 2;
}

function addActor() {
  if (state.actorCount >= 4) return toast("Seedance 最多接收 9 张参考图，因此当前最多支持 4 人。", true);
  state.actorCount += 1;
  $("#actorList").insertAdjacentHTML("beforeend", actorTemplate(state.actorCount));
  bindActor(state.actorCount);
  updateReferenceMap();
  updatePrompt();
}

function removeActor() {
  if (state.actorCount <= 2) return toast("多人复刻至少需要两位人物。", true);
  $(`[data-actor-index="${state.actorCount}"]`).remove();
  state.actorCount -= 1;
  updateReferenceMap();
  updatePrompt();
}

function buildPrompt() {
  const clauses = ["参考@视频1"];
  for (let index = 1; index <= state.actorCount; index += 1) {
    const role = $(`#roleDescription${index}`).value.trim() || `原片中的人物${index}`;
    const personImage = index * 2 - 1;
    const clothingImage = index * 2;
    clauses.push(`将原视频中的「${role}」替换为@图片${personImage}中的人物形象，该人物的服装严格参考@图片${clothingImage}`);
  }
  clauses.push(`将视频背景更换为@图片${state.actorCount * 2 + 1}中的场景`);
  clauses.push("完全复刻原视频中所有人物各自的全部动作、表情、视线、站位和人物之间的互动关系；精准匹配每个人的脚步、抬手、摆臂、躯干倾斜、弹跳重心变化、转向、停顿、接触动作与定格姿势；所有动作的起始时间、落点、强拍定格和节奏变化均与原视频帧级对齐，镜头运镜与画面构图完全复刻原视频；人物身份必须从头到尾分别保持一致，禁止串脸、互换服装、合并肢体、增加人物或遗漏人物；深度视频只用于空间、动作、遮挡和镜头约束，不要生成黑白深度图风格，最终输出正常彩色视频");
  return `${clauses.join("；")}。`;
}

function updatePrompt() { $("#prompt").value = buildPrompt(); }

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
  ["#depthBtn", "#reuseDepthBtn", "#fullBtn", "#recoverBtn", "#addActorBtn", "#removeActorBtn"].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = busy;
  });
  if (!busy) updateReferenceMap();
}

function appendActorFields(form) {
  form.append("actor_count", String(state.actorCount));
  for (let index = 1; index <= state.actorCount; index += 1) {
    const role = $(`#roleDescription${index}`).value.trim();
    const person = $(`#personImage${index}`).files[0];
    const asset = $(`#personAsset${index}`).value.trim();
    const clothing = $(`#clothingImage${index}`).files[0];
    if (!role) throw new Error(`请填写人物 ${index} 在原片中的角色定位。`);
    if (!person && !asset) throw new Error(`请选择人物 ${index} 的人物图片，或填写授权素材 ID。`);
    if (asset && !asset.startsWith("asset://")) throw new Error(`人物 ${index} 的授权素材 ID 必须以 asset:// 开头。`);
    if (!clothing) throw new Error(`请选择人物 ${index} 的服装图片。`);
    form.append(`role_description_${index}`, role);
    if (asset) form.append(`person_asset_${index}`, asset);
    else form.append(`person_image_${index}`, person);
    form.append(`clothing_image_${index}`, clothing);
  }
  const scene = $("#sceneImage").files[0];
  if (!scene) throw new Error("请选择全部人物共享的场景参考图。 ");
  form.append("scene_image", scene);
}

function appendGenerationFields(form) {
  appendActorFields(form);
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

function paidConfirm() {
  return window.confirm(
    `即将通过 Seedance API 提交付费多人复刻任务\n\n人物数量：${state.actorCount} 人\n参考图片：${state.actorCount * 2 + 1} 张\n输出：${$("#resolution").value} / ${$("#ratio").value} / ${$("#duration").value} 秒\n\n是否继续？`
  );
}

async function createDepth() {
  const video = $("#referenceVideo").files[0];
  if (!video) return toast("请先选择多人原视频。", true);
  const form = new FormData();
  form.append("reference_video", video);
  form.append("blur_range", $("#blurRange").value.trim());
  try {
    setBusy(true);
    $("#depthState").textContent = "正在上传并生成多人深度……";
    const job = await api("/api/depth", { method: "POST", body: form });
    state.depthJobId = job.id;
    monitorJob(job.id, true);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function createFull() {
  try {
    const video = $("#referenceVideo").files[0];
    if (!video) throw new Error("请选择多人原视频。 ");
    const form = new FormData();
    form.append("reference_video", video);
    form.append("blur_range", $("#blurRange").value.trim());
    appendGenerationFields(form);
    if (!paidConfirm()) return;
    setBusy(true);
    const job = await api("/api/multi/full", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function createFromDepth() {
  try {
    if (!state.depthJobId && !$("#depthReference").value.trim()) throw new Error("当前没有可用深度视频，请先执行深度转换。 ");
    const form = new FormData();
    if (state.depthJobId) form.append("depth_job_id", state.depthJobId);
    appendGenerationFields(form);
    if (!paidConfirm()) return;
    setBusy(true);
    const job = await api("/api/multi/generate", { method: "POST", body: form });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function saveVideoFromLink(event, selector, defaultName) {
  event.preventDefault();
  const link = $(selector);
  const url = link.dataset.downloadUrl;
  if (!url) return toast("视频文件尚未生成。", true);
  const suggestedName = link.dataset.suggestedName || defaultName;
  if (typeof window.showSaveFilePicker !== "function") {
    const fallback = document.createElement("a");
    fallback.href = `${url}${url.includes("?") ? "&" : "?"}download=1`;
    fallback.download = suggestedName;
    fallback.click();
    return toast("当前浏览器不支持选择完整路径，已使用默认下载。", true);
  }
  try {
    const handle = await window.showSaveFilePicker({ suggestedName, types: [{ description: "MP4 视频", accept: { "video/mp4": [".mp4"] } }] });
    toast("正在保存视频，请稍候……");
    const response = await fetch(url);
    if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`);
    const writable = await handle.createWritable();
    if (response.body) await response.body.pipeTo(writable);
    else { await writable.write(await response.blob()); await writable.close(); }
    toast(`视频已保存：${handle.name}`);
  } catch (error) {
    if (error?.name === "AbortError") return toast("已取消保存。 ");
    toast(error.message || "保存失败。", true);
  }
}

function renderJob(job, depthOnly = false) {
  $("#emptyTask").classList.add("hidden");
  $("#activeTask").classList.remove("hidden");
  const labels = { multi_full: "多人完整流程", multi_generate: "多人成片生成", depth: "深度转换", query: "恢复任务" };
  $("#jobKind").textContent = labels[job.kind] || "多人复刻";
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

  const loadPreview = (selector, url, key) => {
    const video = $(selector);
    if (video.dataset.key === key) return;
    const separator = url.includes("?") ? "&" : "?";
    video.src = `${url}${separator}preview=${encodeURIComponent(key)}`;
    video.dataset.key = key;
    video.load();
    setTimeout(() => {
      if (video.dataset.key !== key || video.readyState !== 0) return;
      video.src = `${url}${separator}preview=${encodeURIComponent(key)}-${Date.now()}`;
      video.load();
    }, 2800);
  };

  $("#depthResultArea").classList.toggle("hidden", !job.has_depth);
  if (job.has_depth) {
    loadPreview("#depthResultVideo", job.depth_url, `${job.id}-depth-${job.created_at}`);
    $("#depthDownloadLink").href = `${job.depth_url}?download=1`;
    $("#depthDownloadLink").dataset.downloadUrl = job.depth_url;
    $("#depthDownloadLink").dataset.suggestedName = `多人深度视频_${job.id}.mp4`;
    state.depthJobId = job.id;
    state.depthSourceDuration = Number(job.depth_duration || job.source_duration || 0);
    state.depthGenerationDuration = Number(job.generation_duration || 0);
    $("#depthState").textContent = "多人深度视频已完成并可预览";
  }

  const seedanceVisible = !depthOnly && job.kind !== "depth" && (job.has_output || job.task_id || job.cloud_status || job.progress >= 48);
  $("#finalResultArea").classList.toggle("hidden", !seedanceVisible);
  $("#finalResultVideo").classList.toggle("hidden", !job.has_output);
  $("#finalResultActions").classList.toggle("hidden", !job.has_output);
  $("#seedanceLivePreview").classList.toggle("hidden", job.has_output || !seedanceVisible);
  if (job.has_output) {
    loadPreview("#finalResultVideo", job.output_url, `${job.id}-output-${job.created_at}`);
    $("#finalDownloadLink").href = `${job.output_url}?download=1`;
    $("#finalDownloadLink").dataset.downloadUrl = job.output_url;
    $("#finalDownloadLink").dataset.suggestedName = `多人复刻成片_${job.id}.mp4`;
    $("#seedancePreviewCaption").textContent = "多人复刻已完成，可预览和下载";
  } else if (seedanceVisible) {
    const statusLabels = { queued: "Seedance 云端排队中", running: "Seedance 2.0 正在生成多人视频", succeeded: "生成完成，正在下载", failed: "Seedance 生成失败" };
    $("#seedanceProgressPercent").textContent = `${job.progress}%`;
    $("#seedanceProgressBar").style.width = `${job.progress}%`;
    $("#seedanceCloudStatus").textContent = statusLabels[job.cloud_status] || job.stage;
    const elapsed = Number(job.cloud_elapsed_seconds || 0);
    $("#seedanceProgressNote").textContent = job.progress_estimated ? `官方状态已同步；百分比为阶段估算 · 已运行 ${elapsed} 秒` : "正在同步任务阶段";
  }
  $("#resultArea").classList.toggle("hidden", !(job.has_depth || seedanceVisible));
}

function monitorJob(jobId, depthOnly = false) {
  state.activeJobId = jobId;
  clearInterval(state.pollTimer);
  const poll = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      renderJob(job, depthOnly);
      if (depthOnly) $("#depthState").textContent = job.stage;
      if (["succeeded", "failed", "submitted"].includes(job.status)) {
        clearInterval(state.pollTimer);
        setBusy(false);
        if (job.status === "succeeded") toast(depthOnly ? "多人深度视频已生成。" : "多人复刻成片已下载到本地。 ");
        else if (job.status === "submitted") toast("Seedance 已开始云端生成，可稍后用任务 ID 恢复。 ");
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
    const job = await api("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: taskId, project: "multi_person_replication", source_job_id: state.activeJobId || "" }) });
    monitorJob(job.id);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    state.config = config;
    $("#model").value = config.model;
    setBadge("#arkBadge", config.ark_ready, "API 已配置", "API 未配置");
    setBadge("#channelBadge", config.temporary_upload_ready, "免费临时通道可用", "临时通道不可用");
    if (config.latest_cloud?.task_id && config.latest_cloud.project === "multi_person_replication") {
      state.activeJobId = config.latest_cloud.id;
      renderJob(config.latest_cloud, false);
      if (!["succeeded", "failed"].includes(config.latest_cloud.status)) {
        monitorJob(config.latest_cloud.id);
      }
      return;
    }
    if (config.latest_depth?.has_depth) {
      state.depthJobId = config.latest_depth.id;
      state.activeJobId = config.latest_depth.id;
      const depthOnlyJob = { ...config.latest_depth, kind: "depth", task_id: "", cloud_status: "", has_output: false, output_url: "" };
      renderJob(depthOnlyJob, true);
      $("#depthState").textContent = "已找到最近生成的深度视频，可直接用于多人复刻";
    }
  } catch (error) { toast(error.message, true); }
}

function initDropzone() {
  const zone = $("#videoDropzone");
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("dragging"); }));
  zone.addEventListener("drop", (event) => {
    if (!event.dataTransfer.files.length) return;
    const transfer = new DataTransfer();
    transfer.items.add(event.dataTransfer.files[0]);
    $("#referenceVideo").files = transfer.files;
    $("#referenceVideo").dispatchEvent(new Event("change"));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  addActor(); addActor();
  loadConfig(); initDropzone();
  bindFile("#referenceVideo", "#videoFileName");
  bindFile("#sceneImage", "#sceneFileName");
  $("#referenceVideo").addEventListener("change", () => videoDurationSuggestion($("#referenceVideo").files[0]));
  $("#addActorBtn").addEventListener("click", addActor);
  $("#removeActorBtn").addEventListener("click", removeActor);
  $("#resetPromptBtn").addEventListener("click", updatePrompt);
  $("#depthBtn").addEventListener("click", createDepth);
  $("#reuseDepthBtn").addEventListener("click", createFromDepth);
  $("#fullBtn").addEventListener("click", createFull);
  $("#recoverBtn").addEventListener("click", recoverTask);
  $("#depthDownloadLink").addEventListener("click", (event) => saveVideoFromLink(event, "#depthDownloadLink", "多人深度视频.mp4"));
  $("#finalDownloadLink").addEventListener("click", (event) => saveVideoFromLink(event, "#finalDownloadLink", "多人复刻成片.mp4"));
  $("#openFolderBtn").addEventListener("click", async () => {
    if (!state.activeJobId) return;
    try { await api(`/api/jobs/${state.activeJobId}/open-folder`, { method: "POST" }); }
    catch (error) { toast(error.message, true); }
  });
});
