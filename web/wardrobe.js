(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const wardrobeModes = ["person", "scene", "clothing", "custom"];
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
    config: null,
    timer: 0,
    objectUrls: [],
    terminalNoticeKeys: new Set(),
    characterGroupsLoaded: false,
    preparedPersonSubmissionStatus: "",
  };
  const modeLabels = { person: "只更换人物", scene: "只更换场景", clothing: "只更换服装", custom: "随心换" };
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

  function paidConfirm(title, lines) {
    // Native <dialog> did not reliably open in every embedded-browser build,
    // leaving paid buttons looking unresponsive. window.confirm is synchronous
    // with the click and therefore cannot silently lose the submit action.
    return Promise.resolve(window.confirm(`${title}\n\n${lines.join("\n")}\n\n确认并提交？`));
  }

  const storageKey = (mode) => `depthflow.wardrobe.prepare.${mode}`;
  const selectedAsset = () => $("#personAsset")?.value?.trim() || "";
  const file = (id) => $(id).files[0];
  const modeFromKind = (kind = "") => kind.match(/wardrobe_(?:prepare|generate)_([a-z]+)/)?.[1] || "";
  const customChoice = (name) => $(`input[name="${name}"]:checked`)?.value || "original";

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
    const custom = state.mode === "custom";
    clearTimeout(state.timer);
    $("#modeInput").value = state.mode;
    localStorage.setItem("depthflow.wardrobe.mode", state.mode);
    location.hash = state.mode;
    $$("[data-mode]").forEach((node) => node.classList.toggle("active", node.dataset.mode === state.mode));
    $("#originalSceneField").classList.toggle("hidden", custom || state.mode === "scene");
    $("#originalClothingField").classList.toggle("hidden", custom || state.mode === "clothing");
    $("#newSceneField").classList.toggle("hidden", state.mode !== "scene");
    $("#newClothingField").classList.toggle("hidden", state.mode !== "clothing");
    $("#customReferencePicker").classList.toggle("hidden", !custom);
    // All modes keep the character library visible.  Scene/clothing/custom can
    // submit the extracted original person and then manage/select it here.
    $("#characters").classList.remove("hidden");
    $("#extractMissing").checked = true;
    $("#extractMissing").disabled = custom;
    $("#extractMissing").nextElementSibling.textContent = custom
      ? "随心换固定从本次原片提取人物、服装和场景三项"
      : "没有上传的原片素材，自动使用 Seedream 5.0 提取";
    $("#extractionCostNote").textContent = custom ? "固定 3 次图片任务" : "按缺失项计算 0–3 次";
    $("#referenceStepNote").textContent = custom
      ? "先预览三类原片提取图，再分别选择保留原片或自行上传替换图"
      : "上传已有素材可避免对应的 Seedream 提取费用";
    $("#characterTitle").textContent = state.mode === "person"
      ? "选择要替换的新人物"
      : state.mode === "custom"
        ? "管理原片人物角色"
        : "选择需要保留的原片人物";
    $("#characterNote").textContent = state.mode === "person"
      ? "最终人物身份只取自所选 Active 火山角色 Asset"
      : state.mode === "custom"
        ? "可将上方提取人物直接送审、预览、复用或删除；随心换参考来源仍由上方三项选择决定"
        : "先提取原片人物并送审；状态变为 Active 后在此选择，成片保持原人物身份";
    if (state.config?.wardrobe_swap_prompts) $("#prompt").value = state.config.wardrobe_swap_prompts[state.mode] || "";
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
    $("#jobState").textContent = "正在恢复该模式存档";
    $("#jobLogs").textContent = saved ? "正在读取已记住的项目…" : "正在查找本机最近存档…";
    if (saved) fetchJob(saved, true, true);
    else restoreLatestMode(state.mode);
  }

  function extractionTaskCount() {
    if (state.mode === "custom") return 3;
    let count = 0;
    if (["person", "clothing"].includes(state.mode) && !state.prepared.scene && !file("#originalSceneImage")) count += 1;
    if (["person", "scene"].includes(state.mode) && !state.prepared.clothing && !file("#originalClothingImage")) count += 1;
    if (["scene", "clothing"].includes(state.mode) && !state.prepared.personCurrent) count += 1;
    return count;
  }

  function renderReadiness() {
    const videoReady = !!file("#referenceVideo");
    const mosaicDisabled = state.busy || !videoReady;
    $("#mosaicBtn").disabled = mosaicDisabled;
    const mosaicReason = state.busy
      ? "任务正在运行，请等待。"
      : videoReady
        ? "原片已就绪；本步骤只进行本地打码，不调用付费模型。"
        : state.mosaicReady
          ? "已有打码视频；如需重新打码，请重新选择原片。"
          : "请先上传原片视频。";
    $("#mosaicReason").textContent = mosaicReason;
    $("#mosaicBtn").dataset.blockers = mosaicReason;

    const whiteReason = state.busy
      ? "任务正在运行，请等待。"
      : state.mosaicReady
        ? (state.whiteReady ? "白膜已经生成；再次点击将重新提交一次付费白膜任务。" : "打码视频已就绪，可以单独提交白膜任务。")
        : "请先生成并预览打码视频。";
    $("#whiteModelBtn").disabled = state.busy || !state.mosaicReady;
    $("#whiteModelReason").textContent = whiteReason;
    $("#whiteModelBtn").dataset.blockers = whiteReason;

    const extractCount = extractionTaskCount();
    $("#extractionCostNote").textContent = state.mode === "custom"
      ? "固定 3 次图片任务"
      : `预计 ${extractCount} 次图片任务`;
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
    if (!state.mosaicReady) reasons.push("尚未生成打码视频");
    if (!state.whiteReady) reasons.push("尚未生成白膜视频");
    if (!state.materialsReady) reasons.push("尚未提取或确认当前模式所需素材");
    if (state.mode !== "custom" && !selectedAsset()) reasons.push(state.mode === "person" ? "未选择新人物 Active 角色" : "未选择原片人物 Active 角色");
    if (state.mode === "scene" && !file("#newSceneImage")) reasons.push("未上传新场景图");
    if (state.mode === "clothing" && !file("#newClothingImage")) reasons.push("未上传新服装图");
    if (state.mode === "custom" && state.materialsReady) {
      [
        ["Person", "人物", "person"], ["Clothing", "服装", "clothing"], ["Scene", "场景", "scene"],
      ].forEach(([field, label, key]) => {
        const choice = customChoice(`custom${field}Choice`);
        if (choice === "original" && !state.prepared[key]) reasons.push(`没有可用的原片${label}提取图`);
        if (choice === "upload" && !file(`#custom${field}Image`)) reasons.push(`已选择自定义${label}但未上传图片`);
      });
    }
    const disabled = state.busy || reasons.length > 0;
    $("#generateBtn").disabled = disabled;
    const generateReason = state.busy ? "任务正在运行，请等待。" : reasons.length ? `还缺：${reasons.join("；")}。` : "材料齐全，可以提交最终成片。";
    $("#generateReason").textContent = generateReason;
    $("#generateBtn").dataset.blockers = generateReason;
    $("#finalCost").textContent = `1 次 Seedance 2.5 · ${$("#resolution").value}`;
  }

  function setMedia(element, url, revision = "") {
    if (!url) return;
    const next = `${url}${url.includes("?") ? "&" : "?"}v=${encodeURIComponent(revision || Date.now())}`;
    if (element.src !== next) { element.src = next; element.load(); }
  }

  function renderPrepared(job) {
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
    const running = ["queued", "running", "submitted"].includes(job.status);
    const terminalNoticeKey = [
      job.id,
      job.status,
      job.task_id || "",
      job.error || job.stage || "",
      (job.logs || []).at(-1) || "",
    ].join("|");
    state.busy = running;
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
      setMedia($("#whiteVideo"), job.white_model_url, job.created_at);
      $("#whiteDownload").href = `${job.white_model_url}?download=1`;
    }
    if (job.has_output) {
      setMedia($("#outputVideo"), job.output_url, job.task_id || job.created_at);
      $("#outputDownload").href = `${job.output_url}?download=1`;
    }
    if (isPrepare) {
      renderPrepared(job);
      state.sourceJobId = job.id;
      state.mosaicReady = !!job.has_mosaic;
      state.whiteReady = !!job.has_white_model;
      state.materialsReady = state.mode === "custom"
        ? !!(job.person_reference_current && job.has_clothing_reference && job.has_scene)
        : state.mode === "person"
          ? !!(job.has_clothing_reference && job.has_scene)
          : state.mode === "scene"
            ? !!(job.person_reference_current && job.has_clothing_reference)
            : !!(job.person_reference_current && job.has_scene);
      state.sourceReady = state.whiteReady && state.materialsReady;
      localStorage.setItem(storageKey(state.mode), job.id);
    }
    renderReadiness();
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

  function appendExtractionInputs(form) {
    form.append("source_job_id", state.sourceJobId);
    form.append("extract_missing", $("#extractMissing").checked ? "true" : "false");
    form.append("image_model", state.config?.image_model || "doubao-seedream-5-0-260128");
    form.append("scene_prompt", state.config?.scene_prompt || "");
    form.append("clothing_prompt", state.config?.clothing_triview_prompt || "");
    form.append("person_prompt", state.config?.wardrobe_person_triview_prompt || state.config?.person_triview_prompt || "");
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

  async function generateMosaic() {
    if (!file("#referenceVideo")) return toast("请先上传原片视频。", true);
    const form = new FormData();
    form.append("mode", state.mode);
    form.append("reference_video", file("#referenceVideo"));
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/mosaic", { method: "POST", body: form });
      state.sourceJobId = job.id;
      localStorage.setItem(storageKey(state.mode), job.id);
      renderJob(job); fetchJob(job.id); location.hash = "result";
    } catch (error) { state.busy = false; renderReadiness(); toast(error.message, true); }
  }

  async function generateWhiteModel() {
    if (!state.sourceJobId || !state.mosaicReady) return toast("请先生成并预览打码视频。", true);
    if (!(await paidConfirm("生成白膜动作母版", [
      "模型：Seedance 2.0", "分辨率：480p", "预计提交：1 次付费视频任务。",
      "本步骤只生成白膜，不会重新打码，也不会提取图片素材。",
    ]))) return;
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/white-model", { method: "POST", body: form });
      renderJob(job); fetchJob(job.id); location.hash = "result";
    } catch (error) { state.busy = false; renderReadiness(); toast(error.message, true); }
  }

  async function extractReferences() {
    if (!state.sourceJobId || !state.whiteReady) return toast("请先生成并预览白膜视频。", true);
    const missing = [];
    if (state.mode === "custom") missing.push("原人物、原服装、原场景");
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

  async function generate() {
    renderReadiness();
    if ($("#generateBtn").disabled) return toast($("#generateReason").textContent, true);
    if (!(await paidConfirm(`生成“${modeLabels[state.mode]}”成片`, [
      `模型：Seedance 2.5`, `分辨率：${$("#resolution").value}`, "预计提交：1 次付费视频任务。",
    ]))) return;
    const form = new FormData();
    form.append("source_job_id", state.sourceJobId);
    form.append("mode", state.mode);
    if (state.mode !== "custom") form.append("person_asset", selectedAsset());
    form.append("resolution", $("#resolution").value);
    form.append("generate_audio", $("#generateAudio").checked ? "true" : "false");
    form.append("prompt", $("#prompt").value.trim());
    if (state.mode === "scene") form.append("new_scene_image", file("#newSceneImage"));
    if (state.mode === "clothing") form.append("new_clothing_image", file("#newClothingImage"));
    if (state.mode === "custom") {
      [
        ["Person", "person"], ["Clothing", "clothing"], ["Scene", "scene"],
      ].forEach(([field, key]) => {
        const choice = customChoice(`custom${field}Choice`);
        form.append(`custom_${key}_choice`, choice);
        if (choice === "upload") form.append(`custom_${key}_image`, file(`#custom${field}Image`));
      });
    }
    state.busy = true; renderReadiness();
    try {
      const job = await api("/api/wardrobe-swap/generate", { method: "POST", body: form });
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
      $("#arkBadge").textContent = state.config.ark_ready ? "API 已配置" : "API 未配置";
      $("#arkBadge").classList.toggle("ready", !!state.config.ark_ready);
      updateMode();
    } catch (error) { toast(error.message, true); updateMode(); }
  }

  $$("[data-mode]").forEach((button) => button.addEventListener("click", () => {
    if (state.busy) return toast("当前任务正在运行，请完成后再切换模式。", true);
    state.mode = button.dataset.mode;
    updateMode();
  }));
  $("#referenceVideo").addEventListener("change", (event) => {
    revokeObjectUrls();
    const current = event.target.files[0];
    $("#videoName").textContent = current ? `${current.name} · ${(current.size / 1048576).toFixed(1)} MB` : "MP4 / MOV · 上传后本地生成打码视频";
    $("#sourcePreview").classList.toggle("hidden", !current);
    if (current) { const url = URL.createObjectURL(current); state.objectUrls.push(url); $("#sourcePreview").src = url; }
    renderReadiness();
  });
  [
    ["#originalSceneImage", "#originalSceneField"], ["#originalClothingImage", "#originalClothingField"],
    ["#newSceneImage", "#newSceneField"], ["#newClothingImage", "#newClothingField"],
    ["#customPersonImage", "#customPersonUploadField"], ["#customClothingImage", "#customClothingUploadField"],
    ["#customSceneImage", "#customSceneUploadField"],
  ].forEach(([input, field]) => $(input).addEventListener("change", () => { setFilePreview(input, field); renderReadiness(); }));
  $$('input[name="customPersonChoice"], input[name="customClothingChoice"], input[name="customSceneChoice"]')
    .forEach((input) => input.addEventListener("change", renderReadiness));
  $("#resolution").addEventListener("change", renderReadiness);
  $("#mosaicBtn").addEventListener("click", generateMosaic);
  $("#whiteModelBtn").addEventListener("click", generateWhiteModel);
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
