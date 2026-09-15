(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const wardrobeModes = ["person", "scene", "clothing", "custom", "dynamic", "dynamic_object", "dynamic_scene"];
  const rememberedMode = localStorage.getItem("depthflow.wardrobe.mode") || "person";
  const state = {
    mode: wardrobeModes.includes(location.hash.slice(1))
      ? location.hash.slice(1)
      : (wardrobeModes.includes(rememberedMode) ? rememberedMode : "person"),
    sourceJobId: "",
    sourceReady: false,
    mosaicReady: false,
    whiteReady: false,
    materialsReady: false,
    prepared: { person: false, personCurrent: false, clothing: false, scene: false },
    busy: false,
    mosaicSettingsJob: "", mosaicSource: "", mosaicThresholdActual: null, mosaicSettingsDirty: false,
    dynamicDirty: false, dynamicLoadedJob: "", dynamicSource: "", savedReplacement: "",
    config: null,
    timer: 0,
    objectUrls: [],
    terminalNoticeKeys: new Set(),
    characterGroupsLoaded: false,
    preparedPersonSubmissionStatus: "",
    customPersonPath: ["extract", "library", "upload"].includes(localStorage.getItem("depthflow.wardrobe.customPersonPath"))
      ? localStorage.getItem("depthflow.wardrobe.customPersonPath")
      : "library",
  };
  const modeLabels = { person: "只更换人物", scene: "只更换场景", clothing: "只更换服装", custom: "随心换", dynamic: "动态场景更换人物", dynamic_object: "动态物品替换", dynamic_scene: "动态背景替换" };
  if (["dynamic_object", "dynamic_scene"].includes(state.mode) && window.startWardrobeObject) { window.startWardrobeObject(state.mode); return; }
  const isDynamic = (mode = state.mode) => ["dynamic", "dynamic_object", "dynamic_scene"].includes(mode);
  const isEnvironment = () => ["dynamic_object", "dynamic_scene"].includes(state.mode);
  const dynamicProfile = () => state.config?.wardrobe_dynamic_modes?.[state.mode] || {};
  const toast = (message, error = false) => {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("error", error);
    node.classList.add("show");
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.classList.remove("show"), 5200);
  };
  window.toast = toast;
  const api = async (url, options = {}) => {
    const response = await fetch(url, options);
    let body = {};
    try { body = await response.json(); } catch (_error) { /* handled below */ }
    if (!response.ok) throw new Error(body.error || `请求失败（HTTP ${response.status}）`);
    return body;
  };

  let paidConfirmResolver = null;
  let paidConfirmTrigger = null;

  function closePaidConfirm(confirmed) {
    if (!paidConfirmResolver) return;
    const resolve = paidConfirmResolver;
    paidConfirmResolver = null;
    const modal = $("#paidConfirmModal");
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("workspace-dialog-open");
    paidConfirmTrigger?.focus?.();
    paidConfirmTrigger = null;
    resolve(Boolean(confirmed));
  }

  function paidConfirm(title, lines) {
    if (paidConfirmResolver) return Promise.resolve(false);
    paidConfirmTrigger = document.activeElement;
    $("#paidConfirmTitle").textContent = title;
    $("#paidConfirmLines").replaceChildren(...lines.map((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      return item;
    }));
    const modal = $("#paidConfirmModal");
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("workspace-dialog-open");
    requestAnimationFrame(() => $("#paidConfirmSubmit").focus());
    return new Promise((resolve) => { paidConfirmResolver = resolve; });
  }

  const storageKey = (mode) => `depthflow.wardrobe.prepare.${mode}`;
  const selectedAsset = () => $("#personAsset")?.value?.trim() || "";
  const file = (id) => $(id).files[0];
  const modeFromKind = (kind = "") => kind.match(/wardrobe_(?:prepare|generate)_([a-z_]+)$/)?.[1] || "";
  const customChoice = (name) => $(`input[name="${name}"]:checked`)?.value || "original";

  function renderCustomPersonSelection() {
    $$('[data-custom-person-path]').forEach((button) => {
      button.classList.toggle("active", button.dataset.customPersonPath === state.customPersonPath);
    });
    const status = $("#customPersonAssetStatus");
    if (!status) return;
    const asset = selectedAsset();
    status.classList.toggle("ready", Boolean(asset));
    status.textContent = asset
      ? `最终人物：已选择 Active 角色 ${asset}`
      : "最终人物：尚未选择 Active 角色；请选择下方角色库中的人物";
  }

  function setCustomPersonPath(path, navigate = false) {
    if (!["extract", "library", "upload"].includes(path)) return;
    state.customPersonPath = path;
    localStorage.setItem("depthflow.wardrobe.customPersonPath", path);
    renderCustomPersonSelection();
    renderReadiness();
    if (!navigate) return;
    if (path === "extract") {
      const target = state.prepared.person ? $("#preparedPersonCard") : $("#prepare");
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
      toast(state.prepared.person
        ? "请预览提取人物，提交角色库审核；状态变为 Active 后在下方选择。"
        : "请先完成白膜，然后在步骤 03 提取原片人物。", false);
      return;
    }
    const libraryMode = path === "upload" ? "upload" : "existing";
    $(`#characters [data-character-source-mode="${libraryMode}"]`)?.click();
    $("#characters")?.scrollIntoView({ behavior: "smooth", block: "start" });
    toast(path === "upload"
      ? "请在角色库上传区选择人像组并上传新人物；审核 Active 后再选择使用。"
      : "请从角色库中选择一个状态为 Active 的人物。", false);
  }

  function revokeObjectUrls() {
    state.objectUrls.splice(0).forEach((url) => URL.revokeObjectURL(url));
  }

  function setFilePreview(inputSelector, fieldSelector) {
    const input = $(inputSelector);
    const field = $(fieldSelector);
    if (!input || !field) return;
    const current = input.files[0];
    field.classList.toggle("has-file", !!current);
    const title = field.querySelector("b");
    if (current && title) title.textContent = current.name;
  }

  function updateMode() {
    window.DepthFlowInlineCast?.clear();
    const custom = state.mode === "custom";
    const dynamic = isDynamic();
    if ($("#modeInput").value !== state.mode && (dynamic || isDynamic($("#modeInput").value))) {
      $("#referenceVideo").value = "";
      $("#sourcePreview").removeAttribute("src"); $("#sourcePreview").classList.add("hidden");
      $("#videoName").textContent = "MP4 / MOV · 超过15秒分段制作，不足5秒自动补黑场";
      revokeObjectUrls();
    }
    clearTimeout(state.timer);
    $("#modeInput").value = state.mode;
    localStorage.setItem("depthflow.wardrobe.mode", state.mode);
    location.hash = state.mode;
    $$("[data-mode]").forEach((node) => node.classList.toggle("active", node.dataset.mode === state.mode));
    $("#originalSceneField").classList.toggle("hidden", dynamic || custom || state.mode === "scene");
    $("#originalClothingField").classList.toggle("hidden", dynamic || custom || state.mode === "clothing");
    $("#newSceneField").classList.toggle("hidden", state.mode !== "scene");
    $("#newClothingField").classList.toggle("hidden", !dynamic && state.mode !== "clothing");
    $("#modeInput").form.classList.toggle("dynamic-mode", dynamic);
    ["#dynamicMosaicNote", "#dynamicWhiteControls", "#dynamicFinalControls", "#dynamicFinalBase"].forEach(id => $(id).classList.toggle("hidden", !dynamic));
    $("#referenceExtractionStage").classList.toggle("hidden", dynamic);
    $("#dynamicWhiteBase").textContent = state.config?.wardrobe_dynamic_white_prompt || "服务更新后显示主角白模规则。";
    $("#dynamicFinalBase").textContent = state.config?.wardrobe_swap_prompts?.dynamic || "";
    $("#prompt").maxLength = dynamic ? 500 : 2600;
    $("#prompt").placeholder = dynamic ? "补充要求（最多 500 字）。输入 @ 选择实际素材；上方固定规则会一并提交。" : "";
    $("#finalPromptDetails").open = dynamic;
    $("#dynamicMentionMenu").classList.add("hidden");
    state.dynamicDirty = false; state.dynamicLoadedJob = ""; state.dynamicSource = "";
    state.savedReplacement = "";
    $("#dynamicReplacementImage").value = "";
    $("#savedDynamicReference").classList.add("hidden");
    state.mosaicSettingsJob = ""; state.mosaicSource = "";
    state.mosaicThresholdActual = null; state.mosaicSettingsDirty = false;
    $("#faceScoreThreshold").value = "0.55";
    $("#dynamicMosaicReviewed").checked = false; $("#dynamicWhiteReviewed").checked = false;
    $("#dynamicWhiteExtra").value = "";
    $("#mosaicDescription").textContent = dynamic ? "自动检测并遮挡视频中出现的全部人脸，可免费重新打码。" : "上传原片后，本地生成人脸打码视频，先预览检查。";
    $("#whiteDescription").textContent = dynamic ? "主角转换为无服装白模，保留动态背景、道具与其他人物，去除原片叠加字幕。" : "参考打码视频生成无服装、无字幕的白膜动作母版。";
    $("#referenceHeading").textContent = dynamic ? "选择新人物与服装" : "准备替换素材";
    $("#generationReferenceNote").textContent = dynamic ? "白模视频 + 新人物角色 + 服装图；背景与运镜均跟随视频。" : "按当前模式准备参考素材，生成最终成片。";
    $("#customReferencePicker").classList.toggle("hidden", !custom);
    // All modes keep the character library visible.  Scene/clothing/custom can
    // submit the extracted original person and then manage/select it here.
    $("#characters").classList.remove("hidden");
    $("#extractMissing").checked = true;
    $("#extractMissing").disabled = custom;
    $("#extractMissing").nextElementSibling.textContent = custom
      ? "随心换固定提取服装与场景；选择“提取原片人物并入库”时才额外提取人物"
      : "没有上传的原片素材，自动使用 Seedream 5.0 提取";
    $("#extractionCostNote").textContent = custom ? "预计 2–3 次图片任务" : "按缺失项计算 0–3 次";
    $("#referenceStepNote").textContent = custom
      ? "人物只使用火山 Active 角色；服装和场景可选原片提取图或自行上传"
      : "上传已有素材可避免对应的 Seedream 提取费用";
    if (dynamic) $("#referenceStepNote").textContent = "每人可选上传服装；未上传时使用人物形象图中的服装";
    $("#newClothingField small").textContent = dynamic ? "可选；未上传时使用人物形象图中的服装" : "成片只更换服装";
    $("#prepare .panel-heading small").textContent = dynamic ? "打码和白模独立执行，可预览后再继续" : "三个任务独立执行、独立失败、独立重试";
    $("#characterTitle").textContent = dynamic || state.mode === "person"
      ? "选择要替换的新人物"
      : state.mode === "custom"
        ? "选择随心换最终人物角色"
        : "选择需要保留的原片人物";
    $("#characterNote").textContent = dynamic ? "可选择已有角色，或在下方上传新人物入库；审核为 Active 后选择使用。每人的服装可选上传，未上传时沿用该人物形象图中的服装。" : state.mode === "person"
      ? "最终人物身份只取自所选 Active 火山角色 Asset"
      : state.mode === "custom"
        ? "可提取原片人物后入库、直接选择已有角色，或上传新人物入库；最终成片不会直接提交本地真人图"
        : "先提取原片人物并送审；状态变为 Active 后在此选择，成片保持原人物身份";
    if (state.config?.wardrobe_swap_prompts) $("#prompt").value = state.config.wardrobe_swap_prompts[state.mode] || "";
    if (dynamic) $("#prompt").value = "";
    configureDynamicMode();
    const saved = localStorage.getItem(storageKey(state.mode)) || "";
    state.sourceJobId = saved;
    state.sourceReady = false;
    state.mosaicReady = false;
    state.whiteReady = false;
    state.materialsReady = false;
    state.prepared = { person: false, personCurrent: false, clothing: false, scene: false };
    state.characterGroupsLoaded = false;
    state.preparedPersonSubmissionStatus = "";
    $("#preparedReferences").classList.add("hidden");
    ["Person", "Clothing", "Scene"].forEach((field) => {
      const box = $(`#custom${field}OriginalBox`);
      const image = $(`#custom${field}OriginalPreview`);
      box.classList.remove("has-reference");
      image.classList.add("hidden");
      image.removeAttribute("src");
      box.querySelector("small").textContent = "完成步骤 03 后显示";
    });
    $("#progressArea").classList.add("hidden");
    $("#mosaicCard").classList.add("hidden");
    $("#whiteCard").classList.add("hidden");
    $("#outputCard").classList.add("hidden");
    renderCustomPersonSelection();
    $("#jobState").textContent = "正在恢复该模式存档";
    $("#jobLogs").textContent = saved ? "正在读取已记住的项目…" : "正在查找本机最近存档…";
    if (saved) fetchJob(saved, true, true);
    else restoreLatestMode(state.mode);
  }

  function extractionTaskCount() {
    if (state.mode === "custom") {
      return 2 + (state.customPersonPath === "extract" && !state.prepared.personCurrent ? 1 : 0);
    }
    let count = 0;
    if (["person", "clothing"].includes(state.mode) && !state.prepared.scene && !file("#originalSceneImage")) count += 1;
    if (["person", "scene"].includes(state.mode) && !state.prepared.clothing && !file("#originalClothingImage")) count += 1;
    if (["scene", "clothing"].includes(state.mode) && !state.prepared.personCurrent) count += 1;
    return count;
  }

  function configureDynamicMode() {
    const environment = isEnvironment(), profile = dynamicProfile();
    $("#dynamicMosaicNote").textContent = "自动检测并打码视频中出现的全部人脸，无需框选或补定位点。预览确认后，再生成仅主要人物为白模的视频。";
    $("#dynamicTargetField").classList.toggle("hidden", !environment);
    $("#dynamicReplacementField").classList.toggle("hidden", !environment);
    $("#faceScoreThreshold").closest(".mosaic-setting").classList.toggle("hidden", environment);
    $("#characters").classList.toggle("hidden", environment);
    $("#prepare .local-stage header b").textContent = environment ? "准备原片母版" : "生成人脸打码视频";
    $("#prepare .panel-heading h2").textContent = environment ? "原片母版与目标白模" : "打码、白膜与素材提取";
    $("#dynamicWhiteControls summary").textContent = environment ? "目标白模提示词与补充要求" : "主角白模提示词与补充要求";
    $("#stepNav a[href='#prepare'] span").textContent = environment ? "原片/目标白模" : "打码/白膜/提取";
    $("#stepNav a[href='#references'] span").textContent = environment ? "替换参考图" : "人物与素材";
    $$(".hero-flow b").forEach((node,index)=>{node.textContent=(environment ? ["原片母版","目标白模","替换参考图","Seedance 2.5"] : ["原片打码","白膜母版","三类素材","Seedance 2.5"])[index];});
    $("#mosaicCard header b").textContent = environment ? "原片母版" : "人脸打码视频";
    $("#dynamicWhiteControls details small").textContent = environment ? "@视频1 已绑定原片母版；只白模化指定目标，人物及非目标区域保持原样。" : "@视频1 已绑定全部人脸打码视频；白模只转换主要人物，可在补充要求中说明主角。";
    $("#dynamicMosaicReviewed").nextElementSibling.textContent = environment ? "已预览原片母版，替换目标、时长和画面正确" : "已预览打码：视频中出现的人脸已遮挡，没有明显漏码";
    $("#dynamicWhiteReviewed").nextElementSibling.textContent = environment ? "已预览白模：只有目标物品或场景变白，人物、非目标区域和运镜正确" : "已预览白模：只有指定主角变白，动态背景、道具和其他人物保留完整";
    $("#dynamicFinalControls p").textContent = environment ? "最终参考：目标白模视频 + 一张替换图。先检查非目标区域是否保留正确，再继续生成。" : "最终使用以下三项素材，去除原片叠加字幕，不新增文字。";
    $("#dynamicFinalControls small").textContent = environment ? "点击素材标签或输入 @ 引用。参考图只提供外观，动作、占位、透视和运镜跟随白模视频。" : "点击素材标签插入引用，或在下方提示词中输入 @ 选择。人物图可从角色库上传新图并审核后使用。";
    $("#dynamicWhiteBase").textContent = profile.white_prompt || state.config?.wardrobe_dynamic_white_prompt || "服务更新后显示白模规则。";
    $("#dynamicFinalBase").textContent = profile.final_prompt || state.config?.wardrobe_swap_prompts?.[state.mode] || "";
    $("#dynamicWhiteExtra").placeholder = environment ? "可选：补充需要保留的细节和不能修改的区域；目标描述在上方填写。" : "可选补充，例如：主角始终是攀岩者，地面保护员保持原貌。";
    if (!environment) return;
    $("#newClothingField").classList.add("hidden");
    $("#dynamicTargetLabel").textContent = profile.target_label || "需要替换的目标";
    $("#dynamicTargetDescription").value = profile.target_default || "";
    $("#dynamicTargetDescription").placeholder = state.mode === "dynamic_object" ? "例如：人物右手握着的蓝色杯子；保留左侧桌上的其他杯子" : "例如：整间房间及固定陈设，保留人物、背包和手中的杯子";
    $("#dynamicTargetHelp").textContent = state.mode === "dynamic_object" ? "描述原片中的位置、颜色和互动关系，让模型分清要换哪一件。建议先选一件轮廓清晰的物品；复杂遮挡与形状差异仍需检查。" : "场景图适合提供新环境外观。空间结构、落脚点与运镜沿用原片；结构差异很大的场景可能无法自然匹配。";
    $("#dynamicReplacementField b").textContent = `上传${profile.image_label || "替换参考图"}`;
    $("#dynamicMosaicNote").textContent = "本模式保留原片人物与服装，不做人脸打码。先确认原片，再仅将替换目标白模化；如生成服务不接受素材，会保留原片并显示错误。";
    $("#mosaicDescription").textContent = "本地按每段最多15秒准备原片，保留完整时长，不调用付费模型。";
    $("#whiteDescription").textContent = state.mode === "dynamic_object" ? "只将指定物品变成白模，保留轮廓、运动、握持和遮挡；人物与背景保持原貌。" : "仅把场景变成保留空间结构的白模，保持人物、服装、手持物、运镜与视差。";
    $("#referenceHeading").textContent = `上传${profile.image_label || "替换参考图"}`;
    $("#referenceStepNote").textContent = "一张参考图绑定 @图片1；原片人物继续从视频保留，无需选择新角色。";
    $("#generationReferenceNote").textContent = "@视频1 提供动态与空间依据，@图片1 提供替换外观。";
    $("#prepare .panel-heading small").textContent = "准备原片 → 目标白模 → 参考图重绘；两次生成分别确认";
  }

  function mosaicThresholdProblem() {
    if (isEnvironment()) return "";
    if (!state.config?.wardrobe_mosaic_threshold_supported) return "后台尚未加载灵敏度设置，请更新服务后刷新页面。";
    const input = $("#faceScoreThreshold");
    const value = Number(input.value);
    if (!input.value.trim() || !Number.isFinite(value) || value < 0.30 || value > 0.90) return "人脸检测阈值请输入 0.30–0.90 之间的数字。";
    return "";
  }

  function renderReadiness() {
    window.DepthFlowInlineCast?.configure({base:(isDynamic()?1:2)+Number(window.DepthFlowInlineCast?.hasClothing()??!!file("#newClothingImage")),enabled:true,busy:state.busy,prompts:[$('#prompt')],getPreview:()=>({mode:state.mode,custom:$('#prompt').value,white_extra:$('#dynamicWhiteExtra').value})});
    const dynamic = isDynamic();
    const castCount=window.DepthFlowInlineCast?.count()||1;
    $('#whiteModelBtn').textContent=castCount>1?`生成 ${castCount} 人分色母版`:'生成白膜视频';
    $('#whiteDescription').textContent=castCount>1?`将 ${castCount} 个主要人物按${['红','白','黄','蓝'].slice(0,castCount).join('、')}分色，模型不保留衣服、鞋帽或衣物轮廓，并去除原片叠加字幕。`:dynamic?'主角转换为无服装白模，保留动态背景、道具与其他人物，去除原片叠加字幕。':'参考打码视频生成无服装、无字幕的白膜动作母版。';
    $('#dynamicWhiteControls details > summary').textContent=castCount>1?`${castCount} 人分色母版提示词与补充要求`:'无服装白模提示词与补充要求';
    $('#dynamicFinalControls > p').textContent=castCount>1?'逐人提交人物与服装参考，按实际素材编号绑定；最终成片去除原片叠加字幕。':'最终使用以下三项素材，去除原片叠加字幕，不新增文字。';
    $('#dynamicWhiteReviewed').nextElementSibling.textContent=castCount>1?`已预览母版：${castCount} 个人物的对应、动作与遮挡正确，模型没有服装，画面没有叠加字幕`:'已预览白模：主角没有服装或衣物轮廓，画面没有叠加字幕，背景与其他人物保留完整';
    const videoReady = !!file("#referenceVideo") || !!state.mosaicSource;
    const thresholdProblem = mosaicThresholdProblem();
    $("#faceScoreThreshold").disabled = state.busy || !state.config?.wardrobe_mosaic_threshold_supported;
    $("#faceThresholdStatus").textContent = thresholdProblem || (state.mosaicSettingsDirty
      ? "阈值已修改，请重新打码后预览；当前视频仍是旧结果。"
      : state.mosaicThresholdActual !== null
        ? `本任务检测阈值：${state.mosaicThresholdActual.toFixed(2)}`
        : state.mosaicReady ? "旧任务未记录阈值，重新打码后将保存本次设置。" : "");
    $("#mosaicBtn").textContent = isEnvironment() ? (state.mosaicReady ? "重新准备母版（免费）" : "准备原片母版（免费）") : state.mosaicReady ? "重新打码（免费）" : "生成打码视频";
    const mosaicDisabled = state.busy || !videoReady || !!thresholdProblem;
    $("#mosaicBtn").disabled = mosaicDisabled;
    const mosaicReason = state.busy
      ? "任务正在运行，请等待。"
      : thresholdProblem || (videoReady
        ? "原片已就绪；本步骤只进行本地打码，不调用付费模型。"
        : state.mosaicReady
          ? "已有打码视频；如需重新打码，请重新选择原片。"
          : "请先上传原片视频。");
    $("#mosaicReason").textContent = mosaicReason;
    $("#mosaicBtn").dataset.blockers = mosaicReason;

    const whiteReason = state.busy
      ? "任务正在运行，请等待。"
      : state.mosaicSettingsDirty ? "阈值已修改，请先重新打码并预览。" : state.mosaicReady
        ? (state.whiteReady ? "白膜已经生成；再次点击将重新提交一次付费白膜任务。" : "打码视频已就绪，可以单独提交白膜任务。")
        : "请先生成并预览打码视频。";
    $("#whiteModelBtn").disabled = state.busy || !state.mosaicReady || state.mosaicSettingsDirty;
    $("#whiteModelReason").textContent = whiteReason;
    $("#whiteModelBtn").dataset.blockers = whiteReason;

    const extractCount = extractionTaskCount();
    $("#extractionCostNote").textContent = `预计 ${extractCount} 次图片任务`;
    const extractReason = state.busy
      ? "任务正在运行，请等待。"
      : state.whiteReady
        ? (state.materialsReady ? "所需素材已经就绪；再次点击可重新提取或确认新上传素材。" : "白膜已就绪，可以单独提取/确认素材。")
        : "请先生成并预览白膜视频。";
    $("#extractReferencesBtn").disabled = state.busy || !state.whiteReady;
    $("#extractReason").textContent = extractReason;
    $("#extractReferencesBtn").dataset.blockers = extractReason;
    const personRetry = $("#retryPersonExtraction");
    if (personRetry) {
      personRetry.disabled = state.busy || !state.whiteReady;
      personRetry.dataset.blockers = state.busy ? "任务正在运行，请等待。" : state.whiteReady ? "只提交 1 次人物图片提取任务。" : "请先完成白膜视频。";
    }

    const reasons = [];
    if (state.mosaicSettingsDirty) reasons.push("阈值已修改，请先重新打码并生成白膜");
    if (!state.mosaicReady) reasons.push("尚未生成打码视频");
    if (!state.whiteReady) reasons.push("尚未生成白膜视频");
    if (!state.materialsReady) reasons.push("尚未提取或确认当前模式所需素材");
    if (!selectedAsset()) {
      reasons.push(state.mode === "person"
        ? "未选择新人物 Active 角色"
        : state.mode === "custom"
          ? "未从火山角色库选择随心换最终人物 Active 角色"
          : "未选择原片人物 Active 角色");
    }
    if (state.mode === "scene" && !file("#newSceneImage")) reasons.push("未上传新场景图");
    if (state.mode === "custom" && state.materialsReady) {
      [
        ["Clothing", "服装", "clothing"], ["Scene", "场景", "scene"],
      ].forEach(([field, label, key]) => {
        const choice = customChoice(`custom${field}Choice`);
        if (key !== "clothing" && choice === "original" && !state.prepared[key]) reasons.push(`没有可用的原片${label}提取图`);
        if (key !== "clothing" && choice === "upload" && !file(`#custom${field}Image`)) reasons.push(`已选择自定义${label}但未上传图片`);
      });
    }
    const disabled = state.busy || reasons.length > 0;
    $("#generateBtn").disabled = disabled;
    const generateReason = state.busy ? "任务正在运行，请等待。" : reasons.length ? `还缺：${reasons.join("；")}。` : "材料齐全，可以提交最终成片。";
    $("#generateReason").textContent = generateReason;
    $("#generateBtn").dataset.blockers = generateReason;
    $("#finalCost").textContent = `1 次 Seedance 2.5 · ${$("#resolution").value}`;
    if (dynamic) renderDynamicReadiness();
    $("#savedDynamicReference").classList.toggle("hidden", !isEnvironment() || !state.savedReplacement || !!file("#dynamicReplacementImage"));
    if (state.savedReplacement) $("#savedDynamicImage").src = state.savedReplacement;
    renderCustomPersonSelection();
  }

  function setMedia(element, url, revision = "") {
    if (!url) return;
    const next = `${url}${url.includes("?") ? "&" : "?"}v=${encodeURIComponent(revision || Date.now())}`;
    if (element.src !== next) { element.src = next; element.load(); }
  }

  function renderPrepared(job) {
    if (isDynamic()) { $("#preparedReferences").classList.add("hidden"); return; }
    const any = job.has_scene || job.has_clothing_reference || job.has_person_reference;
    const personExtractionMode = ["scene", "clothing", "custom"].includes(state.mode);
    state.prepared = {
      scene: !!job.has_scene,
      clothing: !!job.has_clothing_reference,
      person: !!job.has_person_reference,
      personCurrent: !!job.person_reference_current,
    };
    $("#preparedReferences").classList.toggle("hidden", !any && !personExtractionMode);
    $("#preparedSceneCard").classList.toggle("hidden", !job.has_scene);
    $("#preparedClothingCard").classList.toggle("hidden", !job.has_clothing_reference);
    $("#preparedPersonCard").classList.toggle("hidden", !personExtractionMode);
    $("#preparedPersonPlaceholder").classList.toggle("hidden", !!job.has_person_reference);
    $("#preparedPersonImage").classList.toggle("hidden", !job.has_person_reference);
    $("#preparedPersonDownload").classList.toggle("hidden", !job.has_person_reference);
    $("#preparedPersonSubmitPanel").classList.toggle("hidden", !job.has_person_reference);
    $("#retryPersonExtraction").disabled = state.busy || !job.has_white_model;
    $("#retryPersonExtraction").textContent = job.has_person_reference ? "重新提取人物" : "单独提取人物";
    $("#preparedPersonPreviewStatus").textContent = job.has_person_reference
      ? job.person_reference_current
        ? "人物提取成功，已使用白色短袖 T 恤和白色短裤规则。确认预览正确后，可提交火山角色库审核。"
        : "这是旧版人物图，仍保留原服装；请点击“提取/确认素材”或“重新提取人物”生成白 T 短裤新版。"
      : job.error
        ? `人物预览尚未生成：${job.error}`
        : job.has_white_model
          ? "白膜已就绪，可单独提取人物预览。"
          : "请先完成白膜视频，再单独提取人物。";
    if (job.has_scene) $("#preparedSceneImage").src = `${job.scene_url}?v=${job.scene_revision || job.created_at}`;
    if (job.has_clothing_reference) $("#preparedClothingImage").src = `${job.clothing_url}?v=${job.clothing_revision || job.created_at}`;
    if (job.has_person_reference) {
      $("#preparedPersonImage").src = `${job.person_url}?v=${job.person_revision || Date.now()}`;
      $("#preparedPersonDownload").href = `${job.person_url}?download=1&v=${job.person_revision || Date.now()}`;
      if (!$("#preparedPersonName").value.trim()) {
        $("#preparedPersonName").value = `${modeLabels[state.mode]}-原片人物`;
      }
      const actor = (job.actors || []).find((item) => item.id === 1);
      if (actor?.ark_library_upload_status) {
        state.preparedPersonSubmissionStatus = actor.ark_library_asset_name
          ? `${actor.ark_library_asset_name} · ${actor.ark_library_upload_status}；状态变为 Active 后请在下方选择。`
          : `角色库状态：${actor.ark_library_upload_status}`;
        $("#preparedPersonSubmitStatus").textContent = state.preparedPersonSubmissionStatus;
      }
      if (!state.characterGroupsLoaded) loadPreparedPersonGroups();
    }
    [
      ["Person", job.has_person_reference, job.person_url, "人物"],
      ["Clothing", job.has_clothing_reference, job.clothing_url, "服装"],
      ["Scene", job.has_scene, job.scene_url, "场景"],
    ].forEach(([field, ready, url, label]) => {
      const box = $(`#custom${field}OriginalBox`);
      const image = $(`#custom${field}OriginalPreview`);
      box.classList.toggle("has-reference", !!ready);
      image.classList.toggle("hidden", !ready);
      const revision = field === "Person" ? job.person_revision : field === "Clothing" ? job.clothing_revision : job.scene_revision;
      if (ready) image.src = `${url}?v=${revision || job.created_at}`;
      else image.removeAttribute("src");
      box.querySelector("small").textContent = ready ? `原片${label}提取图` : "完成步骤 03 后显示";
    });
  }

  function renderJob(job, restoring = false) {
    const isPrepare = job.kind?.startsWith("wardrobe_prepare_");
    if(isPrepare)window.DepthFlowInlineCast?.restore(job,restoring);
    const running = ["queued", "running", "submitted"].includes(job.status);
    const terminalNoticeKey = [
      job.id,
      job.status,
      job.task_id || "",
      job.error || job.stage || "",
      (job.logs || []).at(-1) || "",
    ].join("|");
    state.busy = running;
    window.DepthFlowSegments?.render(job,{anchor:'#source',busy:running,onSelect:id=>{
      $('#referenceVideo').value='';clearTimeout(state.timer);state.dynamicDirty=false;state.mosaicSettingsDirty=false;
      window.DepthFlowInlineCast?.clear();fetchJob(id,true);
    }});
    $("#progressArea").classList.toggle("hidden", !job.id);
    $("#progressPercent").textContent = `${job.progress || 0}%`;
    $("#progressBar").style.width = `${job.progress || 0}%`;
    $("#progressStage").textContent = job.stage || "等待任务";
    $("#jobState").textContent = job.error_category
      ? `${job.error_category.label}错误${restoring ? "（历史任务）" : ""}`
      : job.stage || job.status;
    $("#jobLogs").textContent = (job.logs || []).join("\n") || "等待日志…";
    const canRecoverWhite = isPrepare && job.status === "failed" && !job.has_white_model && !!job.task_id === false && (
      job.recovery_action === "resume_wardrobe_white_download"
      || (job.has_mosaic && job.error_category?.id === "network")
      || (state.config?.plugin_service_mode === "platform" && job.has_mosaic && /平台回执|平台任务编号|成片转存|账单暂未/.test(job.error || ""))
    );
    $("#recoverWhiteBtn").classList.toggle("hidden", !canRecoverWhite);
    $("#recoverWhiteBtn").disabled = running;
    $("#mosaicCard").classList.toggle("hidden", !job.has_mosaic);
    $("#whiteCard").classList.toggle("hidden", !job.has_white_model);
    $("#outputCard").classList.toggle("hidden", !job.has_output);
    if (job.has_mosaic) {
      setMedia($("#mosaicVideo"), job.mosaic_url, job.created_at);
      $("#mosaicDownload").href = `${job.mosaic_url}?download=1`;
    }
    if (job.has_white_model) {
      setMedia($("#whiteVideo"), job.white_model_url, job.white_model_revision || job.created_at);
      $("#whiteDownload").href = `${job.white_model_url}?download=1`;
    }
    if (job.has_output) {
      setMedia($("#outputVideo"), job.output_url, job.task_id || job.created_at);
      $("#outputDownload").href = `${job.output_url}?download=1`;
    }
    if (isEnvironment() && job.replacement_url) state.savedReplacement = job.replacement_url;
    if (isPrepare) {
      if (state.mosaicSettingsJob !== job.id) {
        state.mosaicSettingsJob = job.id;
        state.mosaicSource = job.source_url || "";
        const savedThreshold = job.wardrobe_mosaic?.face_score_threshold;
        state.mosaicThresholdActual = typeof savedThreshold === "number" && Number.isFinite(savedThreshold) ? savedThreshold : null;
        $("#faceScoreThreshold").value = String(state.mosaicThresholdActual ?? 0.55);
        state.mosaicSettingsDirty = false;
        if (!file("#referenceVideo") && state.mosaicSource) {
          $("#sourcePreview").src = state.mosaicSource;
          $("#sourcePreview").classList.remove("hidden");
          $("#videoName").textContent = "已恢复原片，可调整阈值后免费重新打码";
        }
      }
      if (isDynamic() && state.dynamicLoadedJob !== job.id) {
        state.dynamicLoadedJob = job.id; state.dynamicSource = job.source_url || "";
        state.dynamicDirty = false;
        if (isEnvironment()) $("#dynamicTargetDescription").value = job.wardrobe_dynamic?.description || dynamicProfile().target_default || "";
        state.savedReplacement = job.replacement_url || "";
        $("#dynamicMosaicReviewed").checked = false; $("#dynamicWhiteReviewed").checked = false;
        if (!file("#referenceVideo") && state.dynamicSource) {
          $("#sourcePreview").src = state.dynamicSource;
          $("#sourcePreview").classList.remove("hidden");
          $("#videoName").textContent = "已恢复原片，可直接重新检测全部人脸并打码";
        }
      }
      if (isDynamic() && state.dynamicWhiteRevision !== job.white_model_revision) {
        state.dynamicWhiteRevision = job.white_model_revision;
        $("#dynamicWhiteReviewed").checked = false;
      }
      renderPrepared(job);
      state.sourceJobId = job.id;
      state.mosaicReady = !!job.has_mosaic;
      state.whiteReady = !!job.has_white_model;
      state.materialsReady = isDynamic() ? true : state.mode === "custom"
        ? !!job.has_scene
        : state.mode === "person"
          ? !!job.has_scene
          : state.mode === "scene"
            ? !!job.person_reference_current
            : !!(job.person_reference_current && job.has_scene);
      state.sourceReady = state.whiteReady && state.materialsReady;
      localStorage.setItem(storageKey(state.mode), job.id);
    }
    renderReadiness();
    window.DepthFlowAudio?.render(job,{anchor:'#whiteVideo',busy:running,onRestore:id=>fetchJob(id,true)});
    if (!running) {
      clearTimeout(state.timer);
      // Restoring a saved job must never replay an old API/billing error as if a
      // new request had just failed.  Remember its terminal revision silently;
      // only a newly observed terminal revision may produce one notification.
      if (restoring) state.terminalNoticeKeys.add(terminalNoticeKey);
      else if (!state.terminalNoticeKeys.has(terminalNoticeKey)) {
        state.terminalNoticeKeys.add(terminalNoticeKey);
        if (job.status === "failed") toast(`${job.error_category?.label || "系统"}错误：${job.error || "任务失败"}`, true);
        else toast(job.has_output ? "最终成片已生成。" : (job.stage || "当前独立步骤已完成。"), false);
      }
    }
  }

  async function restoreLatestMode(mode) {
    try {
      const job = await api(`/api/wardrobe-swap/latest?mode=${encodeURIComponent(mode)}`);
      if (state.mode !== mode) return;
      if (!job.id) {
        $("#jobState").textContent = "尚未开始";
        $("#jobLogs").textContent = "当前功能还没有本地存档。";
        renderReadiness();
        return;
      }
      renderJob(job, true);
      if (isDynamic(mode)) await restoreDynamicGeneration(job);
      if (["queued", "running", "submitted"].includes(job.status)) {
        clearTimeout(state.timer);
        state.timer = setTimeout(() => fetchJob(job.id), 3000);
      }
    } catch (_error) {
      if (state.mode !== mode) return;
      $("#jobState").textContent = "存档恢复失败";
      $("#jobLogs").textContent = "本地文件不会被删除，请刷新页面重试恢复。";
      state.busy = false;
      renderReadiness();
    }
  }

  async function fetchJob(id, restoring = false, fallbackToLatest = false) {
    if (!id) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      const jobMode = modeFromKind(job.kind);
      if (jobMode && jobMode !== state.mode) return;
      renderJob(job, restoring);
      if (restoring && isDynamic() && job.kind?.startsWith("wardrobe_prepare_")) await restoreDynamicGeneration(job);
      if (["queued", "running", "submitted"].includes(job.status)) {
        clearTimeout(state.timer);
        state.timer = setTimeout(() => fetchJob(id), 3000);
      }
    } catch (error) {
      if (!restoring) toast(error.message, true);
      localStorage.removeItem(storageKey(state.mode));
      state.sourceJobId = "";
      state.sourceReady = false;
      state.mosaicReady = false;
      state.whiteReady = false;
      state.materialsReady = false;
      state.busy = false;
      if (fallbackToLatest) restoreLatestMode(state.mode);
      else renderReadiness();
    }
  }

  async function restoreDynamicGeneration(source) {
    const entry = Object.values(source.wardrobe_dynamic?.requests || {}).filter(item => item.action === "generate").at(-1);
    if (!entry?.job_id || state.dynamicDirty) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(entry.job_id)}`);
      if (!isDynamic() || state.sourceJobId !== source.id || state.dynamicDirty) return;
      if (source.white_model_revision !== job.white_model_revision) return;
      if (source.has_output && job.status === 'succeeded') return;
      renderJob(job, true);
      if (["queued", "running", "submitted"].includes(job.status)) {
        clearTimeout(state.timer); state.timer = setTimeout(() => fetchJob(job.id), 3000);
      }
    } catch (_error) { /* Keep the restored source and references available. */ }
  }

  function appendExtractionInputs(form) {
    form.append("source_job_id", state.sourceJobId);
    form.append("extract_missing", $("#extractMissing").checked ? "true" : "false");
    form.append("image_model", state.config?.image_model || "doubao-seedream-5-0-260128");
    form.append("scene_prompt", state.config?.scene_prompt || "");
    form.append("clothing_prompt", state.config?.clothing_triview_prompt || "");
    form.append("person_prompt", state.config?.wardrobe_person_triview_prompt || state.config?.person_triview_prompt || "");
    if (state.mode === "custom") form.append("custom_person_source", state.customPersonPath);
    if (state.mode !== "custom" && state.mode !== "scene" && file("#originalSceneImage")) form.append("original_scene_image", file("#originalSceneImage"));
    if (state.mode !== "custom" && state.mode !== "clothing" && file("#originalClothingImage")) form.append("original_clothing_image", file("#originalClothingImage"));
    // Do not let an already selected role Asset suppress extraction of the
    // current source video's person.  The extracted board is independently
    // previewed and can be submitted to the library below.
    if (state.mode !== "custom" && state.mode !== "person" && selectedAsset()) form.append("person_asset", selectedAsset());
  }

  async function loadPreparedPersonGroups(force = false) {
    if (state.characterGroupsLoaded && !force) return;
    const select = $("#preparedPersonGroup");
    const refresh = $("#refreshPreparedPersonGroups");
    refresh.disabled = true;
    select.innerHTML = '<option value="">正在读取 AIGC 人像组…</option>';
    try {
      const library = await api("/api/character-library");
      const groups = (library.groups || []).filter((group) => group.group_type === "AIGC");
      select.replaceChildren(new Option(groups.length ? "请选择人像组" : "暂无 AIGC 人像组，请在下方创建", ""));
      groups.forEach((group) => select.add(new Option(group.name, group.id)));
      if (groups.length === 1) select.value = groups[0].id;
      state.characterGroupsLoaded = true;
      if (!state.preparedPersonSubmissionStatus) {
        $("#preparedPersonSubmitStatus").textContent = groups.length
          ? "确认预览和授权后，可直接提交审核；Active 后在下方选择。"
          : "角色库还没有 AIGC 人像组，请先在下方创建，再点刷新人像组。";
      }
    } catch (error) {
      select.innerHTML = '<option value="">人像组读取失败</option>';
      $("#preparedPersonSubmitStatus").textContent = error.message;
      toast(error.message, true);
    } finally {
      refresh.disabled = false;
    }
  }

  async function submitPreparedPerson() {
    if (!state.sourceJobId || !state.prepared.person) return toast("请先提取并预览原片人物。", true);
    const groupId = $("#preparedPersonGroup").value;
    const name = $("#preparedPersonName").value.trim();
    if (!groupId) return toast("请选择提交目标 AIGC 人像组；没有人像组时请先在下方创建。", true);
    if (!name) return toast("请填写人物素材名称。", true);
    if (!$("#preparedPersonConsent").checked) return toast("请先确认拥有人物素材及用途的合法授权。", true);
    if (!(await paidConfirm("提交提取人物到火山角色库", [
      `素材名称：${name}`,
      "本操作提交火山人物素材审核，不会重新提取图片，也不会生成视频。",
      "审核状态变为 Active 后，才可以在成片中选择使用。",
    ]))) return;
    const button = $("#submitPreparedPerson");
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    form.append("group_id", groupId);
    form.append("name", name);
    form.append("authorization_confirmed", "true");
    button.disabled = true;
    button.textContent = "正在提交审核…";
    $("#preparedPersonSubmitStatus").textContent = "正在建立安全上传地址并提交火山审核…";
    try {
      const result = await api("/api/wardrobe-swap/extracted-person/character-library", { method: "POST", body: form });
      state.preparedPersonSubmissionStatus = `${name} · Processing；请稍后刷新角色库，Active 后即可选择。`;
      $("#preparedPersonSubmitStatus").textContent = state.preparedPersonSubmissionStatus;
      toast(result.message || "提取人物已提交角色库审核。", false);
      await window.DepthFlowCharacterLibrary?.refresh?.();
      fetchJob(state.sourceJobId, true);
    } catch (error) {
      $("#preparedPersonSubmitStatus").textContent = error.message;
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "提交审核到角色库";
    }
  }

  async function retryPersonExtraction() {
    if (!state.sourceJobId) return toast("找不到当前衣装智换任务，请先上传并处理原片。", true);
    if (!state.whiteReady) return toast("请先生成并预览白膜视频。", true);
    if (!(await paidConfirm(state.prepared.person ? "重新提取原片人物" : "单独提取原片人物", [
      `模型：${state.config?.image_model || "Seedream 5.0"}`,
      "预计提交：1 次付费图片任务。",
      "只提取人物预览，不会重新打码、生成白膜、提取场景或提取服装。",
    ]))) return;
    const button = $("#retryPersonExtraction");
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    form.append("image_model", state.config?.image_model || "doubao-seedream-5-0-260128");
    form.append("person_prompt", state.config?.wardrobe_person_triview_prompt || state.config?.person_triview_prompt || "");
    state.busy = true;
    button.disabled = true;
    button.textContent = "正在提取人物…";
    renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/extract-person", { method: "POST", body: form });
      renderJob(job);
      fetchJob(job.id);
      location.hash = "result";
    } catch (error) {
      state.busy = false;
      renderReadiness();
      toast(error.message, true);
    }
  }

  function dynamicSourceProblem() {
    if (isEnvironment() ? !dynamicProfile().white_prompt : state.config?.wardrobe_dynamic_mosaic_scope !== "all_faces") return "新模式的后台尚未加载，请更新服务后刷新页面。";
    if (!file("#referenceVideo") && !state.dynamicSource) return "请先上传原片视频。";
    if (isEnvironment()) {
      const target = $("#dynamicTargetDescription").value.trim();
      if (!target || target.length > 160 || target.includes("@")) return "请用 1–160 字描述原片中的替换目标，不要在目标描述中插入 @。";
    }
    return "";
  }

  function renderDynamicReadiness() {
    const problem = dynamicSourceProblem();
    const pending = state.busy ? "任务正在运行，请等待。" : "";
    const changed = state.dynamicDirty || state.mosaicSettingsDirty;
    const environment = isEnvironment();
    const whiteProblem = pending || (changed ? (environment ? "原片或目标已修改，请重新准备母版。" : "原片或阈值已修改，请先重新打码。") : "") || (!state.mosaicReady ? "请先完成原片准备。" : "") || (!$("#dynamicMosaicReviewed").checked ? "请预览上一步视频，并勾选确认画面正确。" : "");
    const materialProblem = environment ? (!file("#dynamicReplacementImage") && !state.savedReplacement ? `请上传${dynamicProfile().image_label || "替换参考图"}。` : "") : (!selectedAsset() ? "请选择新人物 Active 角色，也可上传新图入库。" : "");
    const finalProblem = pending || (changed ? "原片或目标设置已修改，请重新准备并生成白模。" : "") || (!state.whiteReady ? "请先生成目标白模视频。" : "") || (!$("#dynamicWhiteReviewed").checked ? "请预览白模视频，并勾选确认替换目标及其余画面正确。" : "") || materialProblem;
    const capabilityProblem = environment ? (!dynamicProfile().white_prompt ? problem : "") : (state.config?.wardrobe_dynamic_mosaic_scope !== "all_faces" ? problem : "");
    [["mosaic", pending || problem || mosaicThresholdProblem(), environment ? "原片与目标已就绪，可免费准备母版。" : "原片已就绪，可自动检测全部人脸并免费打码。"], ["whiteModel", whiteProblem || capabilityProblem, "确认预览后可提交 1 次付费白模任务。"], ["generate", finalProblem || capabilityProblem, environment ? "目标白模与替换图已齐全，可以生成成片。" : "素材已齐全：白模视频 + 人物 + 服装，可以生成成片。"]].forEach(([name, reason, ready]) => {
      $(`#${name}Btn`).disabled = !!reason;
      $(`#${name}Reason`).textContent = reason || ready;
      $(`#${name}Btn`).dataset.blockers = reason || ready;
    });
    $("#extractReferencesBtn").disabled = true;
    $("#referenceVideo").disabled = state.busy;
    $("#dynamicTargetDescription").disabled = state.busy;
    renderDynamicReferences();
  }

  const dynamicMedia = () => isEnvironment() ? [
    {token:"@视频1",label:"目标白模视频",ready:state.whiteReady},
    {token:"@图片1",label:file("#dynamicReplacementImage")?.name || dynamicProfile().image_label || "替换图",ready:!!file("#dynamicReplacementImage") || !!state.savedReplacement},
  ] : [
    {token:"@视频1", label:"主角白模视频", ready:state.whiteReady},
    {token:"@图片1", label: selectedAsset() ? `人物 ${selectedAsset()}` : "新人物（待选择）", ready:!!selectedAsset()},
    ...((window.DepthFlowInlineCast?.hasClothing()??!!file("#newClothingImage"))? [{token:"@图片2", label: file("#newClothingImage")?.name || "人物1服装", ready:true}]:[]),
    ...(window.DepthFlowInlineCast?.bindings()||[]),
  ];

  function insertDynamicMention(token) {
    const input = $("#prompt"), start = input.selectionStart, before = input.value.slice(0, start);
    const match = before.match(/@[^\s@，。；]*$/);
    const from = match ? start-match[0].length : start;
    input.setRangeText(`${token} `, from, input.selectionEnd, "end"); input.focus();
    $("#dynamicMentionMenu").classList.add("hidden");
  }

  function renderDynamicReferences() {
    $("#dynamicReferences").replaceChildren(...dynamicMedia().map(ref => {
      const button = document.createElement("button"); button.type = "button"; button.disabled = !ref.ready;
      button.textContent = `${ref.token} · ${ref.label}`;
      button.addEventListener("click", () => insertDynamicMention(ref.token)); return button;
    }));
  }

  function dynamicRequest(form, action) {
    if (!isDynamic()) return "";
    const key = `depthflow.wardrobe.dynamic.${state.sourceJobId}.${action}`;
    let id = localStorage.getItem(key);
    if (!id) { id = crypto.randomUUID(); localStorage.setItem(key, id); }
    form.append("request_id", id); form.append("paid_confirmed", "true");
    return key;
  }

  async function generateMosaic() {
    if (state.busy) return;
    if (mosaicThresholdProblem()) return toast(mosaicThresholdProblem(), true);
    if (isDynamic() && dynamicSourceProblem()) return toast(dynamicSourceProblem(), true);
    if (!file("#referenceVideo") && !state.mosaicSource) return toast("请先上传原片视频。", true);
    const form = new FormData();
    form.append("mode", state.mode);
    form.append("face_score_threshold", $("#faceScoreThreshold").value);
    if (isEnvironment()) form.append("target_description", $("#dynamicTargetDescription").value.trim());
    if (file("#referenceVideo")) form.append("reference_video", file("#referenceVideo"));
    if (state.sourceJobId) {
      form.append("source_job_id", state.sourceJobId);
    }
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/mosaic", { method: "POST", body: form });
      state.sourceJobId = job.id;
      localStorage.setItem(storageKey(state.mode), job.id);
      renderJob(job); fetchJob(job.id); location.hash = "result";
    } catch (error) { state.busy = false; renderReadiness(); toast(error.message, true); }
  }

  async function generateWhiteModel() {
    renderReadiness();
    if ($("#whiteModelBtn").disabled) return toast($("#whiteModelReason").textContent, true);
    if (!state.sourceJobId || !state.mosaicReady) return toast("请先生成并预览打码视频。", true);
    if (!(await paidConfirm("生成白膜动作母版", [
      "模型：Seedance 2.0", "分辨率：480p", "预计提交：1 次付费视频任务。",
      "本步骤只生成白膜，不会重新打码，也不会提取图片素材。",
    ]))) return;
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    if(window.DepthFlowInlineCast)window.DepthFlowInlineCast.appendWhite(form);else form.append("inline_cast_count",1);
    const requestKey = dynamicRequest(form, "white");
    if (isDynamic()) {
      form.append("mosaic_reviewed", $("#dynamicMosaicReviewed").checked ? "true" : "false");
      form.append("white_prompt", $("#dynamicWhiteExtra").value.trim());
      $("#dynamicWhiteReviewed").checked = false;
    }
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/white-model", { method: "POST", body: form });
      if (requestKey) localStorage.removeItem(requestKey);
      renderJob(job); fetchJob(job.id); location.hash = "result";
    } catch (error) { state.busy = false; renderReadiness(); toast(error.message, true); }
  }

  async function extractReferences() {
    if (!state.sourceJobId || !state.whiteReady) return toast("请先生成并预览白膜视频。", true);
    const missing = [];
    if (state.mode === "custom") {
      missing.push("原服装、原场景");
      if (state.customPersonPath === "extract" && !state.prepared.personCurrent) missing.unshift("原人物");
    }
    else {
      if (state.mode !== "scene" && !file("#originalSceneImage")) missing.push("原场景");
      if (state.mode !== "clothing" && !file("#originalClothingImage")) missing.push("原服装");
      if (["scene", "clothing", "custom"].includes(state.mode) && !state.prepared.personCurrent) {
        missing.push(state.prepared.person ? "原人物图（旧版需按白 T 短裤规则重绘）" : "原人物图");
      }
    }
    const lines = ["本步骤不会重新打码，也不会重新生成白膜。"];
    if (missing.length) lines.push(`缺失的 ${missing.join("、")} 将分别调用 Seedream 5.0 提取。`);
    else lines.push("当前所需原片素材均已上传/选择，本次预计不会提交 Seedream 提取任务。")
    lines.push("生成最终成片仍会在下一步单独确认。")
    if (!(await paidConfirm(`提取/确认“${modeLabels[state.mode]}”素材`, lines))) return;
    const form = new FormData();
    appendExtractionInputs(form);
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/extract-references", { method: "POST", body: form });
      renderJob(job); fetchJob(job.id); location.hash = "result";
    } catch (error) { state.busy = false; renderReadiness(); toast(error.message, true); }
  }

  const inlineTags=document.createElement('div');inlineTags.id='inlineCastMentions';$('#prompt').after(inlineTags);
  $('#dynamicWhiteExtra').addEventListener('input',renderReadiness);
  document.addEventListener('inline-cast-change',()=>{if(window.DepthFlowInlineCast?.count()>1&&$('#prompt').value===state.config?.wardrobe_swap_prompts?.[state.mode])$('#prompt').value='';renderReadiness();});
  async function generate() {
    renderReadiness();
    if ($("#generateBtn").disabled) return toast($("#generateReason").textContent, true);
    if (!(await paidConfirm(`生成“${modeLabels[state.mode]}”成片`, [
      `模型：Seedance 2.5`, `分辨率：${$("#resolution").value}`, "预计提交：1 次付费视频任务。",
    ]))) return;
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    form.append("mode", state.mode);
    if (!isEnvironment()) form.append("person_asset", selectedAsset());
    if (isEnvironment() && file("#dynamicReplacementImage")) form.append("replacement_image", file("#dynamicReplacementImage"));
    form.append("resolution", $("#resolution").value);
    form.append("generate_audio", $("#generateAudio").checked ? "true" : "false");
    form.append("prompt", $("#prompt").value.trim());
    if (state.mode === "scene") form.append("new_scene_image", file("#newSceneImage"));
    if (["clothing", "dynamic"].includes(state.mode) && file("#newClothingImage")) form.append("new_clothing_image", file("#newClothingImage"));
    window.DepthFlowInlineCast?.append(form);
    const requestKey = dynamicRequest(form, "generate");
    if (isDynamic()) form.append("white_reviewed", $("#dynamicWhiteReviewed").checked ? "true" : "false");
    if (state.mode === "custom") {
      [
        ["Clothing", "clothing"], ["Scene", "scene"],
      ].forEach(([field, key]) => {
        const choice = customChoice(`custom${field}Choice`);
        form.append(`custom_${key}_choice`, choice);
        if (choice === "upload") form.append(`custom_${key}_image`, file(`#custom${field}Image`));
      });
    }
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/generate", { method: "POST", body: form });
      if (requestKey) localStorage.removeItem(requestKey);
      renderJob(job); fetchJob(job.id); location.hash = "result";
    } catch (error) { state.busy = false; renderReadiness(); toast(error.message, true); }
  }

  async function recoverWhiteModel() {
    if (!state.sourceJobId) return toast("找不到需要恢复的衣装智换任务。", true);
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    state.busy = true;
    renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/recover-white-model", { method: "POST", body: form });
      toast("正在恢复原 Seedance 任务的结果下载，不会重新提交或重复计费。", false);
      renderJob(job);
      fetchJob(job.id);
    } catch (error) {
      state.busy = false;
      renderReadiness();
      toast(error.message, true);
    }
  }

  async function load() {
    try {
      state.config = await api("/api/config");
      $("#arkBadge").textContent = state.config.plugin_service_mode === "platform" ? (state.config.ark_ready ? "平台服务已就绪" : "平台服务待配置") : (state.config.ark_ready ? "API 已配置" : "API 未配置");
      $("#arkBadge").classList.toggle("ready", !!state.config.ark_ready);
      updateMode();
    } catch (error) { toast(error.message, true); updateMode(); }
  }

  $$("[data-mode]").forEach((button) => button.addEventListener("click", () => {
    if (state.busy || paidConfirmResolver) return toast("请先完成或关闭当前操作，再切换模式。", true);
    state.mode = button.dataset.mode;
    if (["dynamic_object", "dynamic_scene"].includes(state.mode) && window.startWardrobeObject) {
      localStorage.setItem("depthflow.wardrobe.mode", state.mode); location.hash = state.mode; location.reload(); return;
    }
    updateMode();
  }));
  $("#referenceVideo").addEventListener("change", (event) => {
    state.mosaicSource = "";
    revokeObjectUrls();
    const current = event.target.files[0];
    $("#videoName").textContent = current ? `${current.name} · ${(current.size / 1048576).toFixed(1)} MB` : "MP4 / MOV · 上传后本地生成打码视频";
    $("#sourcePreview").classList.toggle("hidden", !current);
    if (current) { const url = URL.createObjectURL(current); state.objectUrls.push(url); $("#sourcePreview").src = url; }
    if (isDynamic()) {
      state.dynamicSource = ""; state.dynamicDirty = true;
      $("#dynamicMosaicReviewed").checked = false; $("#dynamicWhiteReviewed").checked = false;
    }
    renderReadiness();
  });
  ["#dynamicMosaicReviewed", "#dynamicWhiteReviewed"].forEach(id => $(id).addEventListener("change", renderReadiness));
  $("#dynamicTargetDescription").addEventListener("input", () => {
    if (state.mosaicReady) state.dynamicDirty = true;
    $("#dynamicMosaicReviewed").checked = false; $("#dynamicWhiteReviewed").checked = false;
    renderReadiness();
  });
  $("#faceScoreThreshold").addEventListener("input", () => {
    state.mosaicSettingsDirty = state.mosaicReady && (state.mosaicThresholdActual === null || Number($("#faceScoreThreshold").value) !== state.mosaicThresholdActual);
    $("#dynamicMosaicReviewed").checked = false; $("#dynamicWhiteReviewed").checked = false;
    renderReadiness();
  });
  $("#prompt").addEventListener("input", () => {
    if (!isDynamic()) return;
    const input = $("#prompt"), menu = $("#dynamicMentionMenu");
    const match = input.value.slice(0,input.selectionStart).match(/@([^\s@，。；]*)$/);
    const refs = match ? dynamicMedia().filter(ref => ref.ready && ref.token.includes(match[1])) : [];
    menu.classList.toggle("hidden", !refs.length);
    menu.replaceChildren(...refs.map(ref => {
      const button = document.createElement("button"); button.type = "button"; button.setAttribute("role", "option");
      button.textContent = `${ref.token} · ${ref.label}`;
      button.addEventListener("click", () => insertDynamicMention(ref.token)); return button;
    }));
  });
  $("#prompt").addEventListener("keydown", event => {
    const menu = $("#dynamicMentionMenu");
    if (event.key === "Escape") menu.classList.add("hidden");
    if (event.key === "ArrowDown" && !menu.classList.contains("hidden")) { event.preventDefault(); menu.querySelector("button")?.focus(); }
  });
  [
    ["#dynamicReplacementImage", "#dynamicReplacementField"],
    ["#originalSceneImage", "#originalSceneField"], ["#originalClothingImage", "#originalClothingField"],
    ["#newSceneImage", "#newSceneField"], ["#newClothingImage", "#newClothingField"],
    ["#customClothingImage", "#customClothingUploadField"], ["#customSceneImage", "#customSceneUploadField"],
  ].forEach(([input, field]) => $(input).addEventListener("change", () => { setFilePreview(input, field); renderReadiness(); }));
  $$('input[name="customClothingChoice"], input[name="customSceneChoice"]')
    .forEach((input) => input.addEventListener("change", renderReadiness));
  $$('[data-custom-person-path]').forEach((button) => button.addEventListener("click", () => {
    setCustomPersonPath(button.dataset.customPersonPath, true);
  }));
  $("#resolution").addEventListener("change", renderReadiness);
  $("#mosaicBtn").addEventListener("click", generateMosaic);
  $("#whiteModelBtn").addEventListener("click", generateWhiteModel);
  $("#paidConfirmForm").addEventListener("submit", (event) => { event.preventDefault(); closePaidConfirm(true); });
  $("#paidConfirmCancel").addEventListener("click", () => closePaidConfirm(false));
  $("#paidConfirmBackdrop").addEventListener("click", () => closePaidConfirm(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && paidConfirmResolver) closePaidConfirm(false);
  });
  $("#extractReferencesBtn").addEventListener("click", extractReferences);
  $("#refreshPreparedPersonGroups").addEventListener("click", () => loadPreparedPersonGroups(true));
  $("#submitPreparedPerson").addEventListener("click", submitPreparedPerson);
  $("#retryPersonExtraction").addEventListener("click", retryPersonExtraction);
  $("#generateBtn").addEventListener("click", generate);
  $("#recoverWhiteBtn").addEventListener("click", recoverWhiteModel);
  document.addEventListener("change", (event) => { if (event.target.id === "personAsset") renderReadiness(); });
  document.addEventListener("input", (event) => { if (event.target.id === "personAsset") renderReadiness(); });
  $("#stepNav").addEventListener("click", (event) => {
    const link = event.target.closest("a"); if (!link) return;
    $$("#stepNav a").forEach((node) => node.classList.toggle("active", node === link));
  });
  window.addEventListener("beforeunload", revokeObjectUrls);
  load();
})();
