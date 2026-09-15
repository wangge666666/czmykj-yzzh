(function () {
  const path = window.location.pathname.replace(/\/$/, "");
  const configs = {
    "/projects/wardrobe-continuation": {
      host: "#characters",
      title: "选择改写视频中的人物",
      note: "上传新人物并审核入库，或选择已有角色。@图片1 提供人物身份，服装在下方单独上传。",
      empty: "请选择已审核通过的人物，或上传新人物到角色库。",
    },
    "/projects/wardrobe": {
      host: "#characters",
      title: "选择当前模式使用的人物角色",
      note: "只更换人物时选择新人物；只更换场景或服装时选择需要保留的原片人物。必须为 Active 火山角色 Asset。",
      empty: "尚未选择角色。准备白膜不受影响，但生成最终成片前必须选择 Active 角色。",
    },
    "/wardrobe-swap": {
      host: "#characters",
      title: "选择当前模式使用的人物角色",
      note: "只更换人物时选择新人物；只更换场景或服装时选择需要保留的原片人物。必须为 Active 火山角色 Asset。",
      empty: "尚未选择角色。准备白膜不受影响，但生成最终成片前必须选择 Active 角色。",
    },
    "/projects/person": {
      host: "#replacement",
      title: "选择需要替换的新人物",
      note: "所选 Active 角色将作为 @图片1 的人物身份，直接替换原片人物。",
      empty: "必须选择角色库人物，或在下方上传人物图 / 手动填写 Asset ID。",
    },
    "/person-only": {
      host: "#replacement",
      title: "选择需要替换的新人物",
      note: "所选 Active 角色将作为 @图片1 的人物身份，直接替换原片人物。",
      empty: "必须选择角色库人物，或在下方上传人物图 / 手动填写 Asset ID。",
    },
    "/projects/scene": {
      host: "#target",
      title: "可选：用角色库锁定人物身份",
      note: "选择后，@图片1 改用角色库人物；不选择则继续使用原片自动提取的人物三视图。",
      empty: "当前沿用原片自动提取的人物，不改变人物身份。",
    },
    "/scene-only": {
      host: "#target",
      title: "可选：用角色库锁定人物身份",
      note: "选择后，@图片1 改用角色库人物；不选择则继续使用原片自动提取的人物三视图。",
      empty: "当前沿用原片自动提取的人物，不改变人物身份。",
    },
    "/projects/clothing": {
      host: "#replacement",
      title: "可选：用角色库锁定换装人物",
      note: "选择后，@图片1 改用角色库人物身份与体型；@图片2 仍是唯一新服装依据。",
      empty: "当前沿用原片自动提取的人物，只更换服装。",
    },
    "/clothing-only": {
      host: "#replacement",
      title: "可选：用角色库锁定换装人物",
      note: "选择后，@图片1 改用角色库人物身份与体型；@图片2 仍是唯一新服装依据。",
      empty: "当前沿用原片自动提取的人物，只更换服装。",
    },
  };
  const config = configs[path];
  const host = config && document.querySelector(config.host);
  const workflowForm = host?.closest("form");
  if (!config || !host || !workflowForm || document.querySelector(".shared-character-library")) return;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
  const notify = (message, isError = false) => {
    if (typeof window.toast === "function") return window.toast(message, isError);
    if (window.DepthFlowUI?.toast) return window.DepthFlowUI.toast(message, isError);
    const node = document.querySelector("#toast");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("error", isError);
    node.classList.add("show");
    window.setTimeout(() => node.classList.remove("show"), 4800);
  };
  const request = async (url, options = {}) => {
    const response = await fetch(url, options);
    let payload = {};
    try { payload = await response.json(); } catch (_error) { /* handled below */ }
    if (!response.ok) {
      throw new Error(window.DepthFlowUI?.formatApiError?.(payload, response) || payload.error || `请求失败（HTTP ${response.status}）`);
    }
    return payload;
  };

  let personAssetInput = document.querySelector("#personAsset");
  if (!personAssetInput) {
    personAssetInput = document.createElement("input");
    personAssetInput.type = "hidden";
    personAssetInput.id = "personAsset";
    personAssetInput.name = "person_asset";
    workflowForm.append(personAssetInput);
  }
  const storageKey = `depthflow.characterAsset.${path}`;
  if (!personAssetInput.value.trim()) {
    try {
      const saved = localStorage.getItem(storageKey) || "";
      if (/^asset:\/\/asset-[A-Za-z0-9_-]{6,120}$/.test(saved)) personAssetInput.value = saved;
    } catch (_error) { /* Browser storage is optional. */ }
  }

  const panel = document.createElement("section");
  panel.className = "shared-character-library";
  const stylesheet = document.createElement('link'); stylesheet.rel = 'stylesheet';
  stylesheet.href = '/static/character_library.css?v=20260915-upload1'; document.head.append(stylesheet);
  const wardrobeActorLayout = path === "/projects/wardrobe" || path === "/wardrobe-swap" || path === "/projects/wardrobe-continuation";
  const uploadButtonLabel = wardrobeActorLayout ? "上传素材并用于当前人物" : "审核并上传角色库";
  const managerMarkup = `
    <div class="shared-character-uploader">
      <section data-character-upload-section>
        <b>从电脑上传新的图片素材</b>
        <div class="shared-character-file" data-character-file-area>
          <input id="characterUploadFile" name="asset_file" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" data-character-upload-file hidden>
          <button type="button" data-character-pick>＋ 选择图片素材</button>
          <small data-character-file-name>选择电脑中的 JPG / PNG / WEBP 图片，最大 30MB；也可以拖入图片。</small>
        </div>
        <div class="shared-character-upload-fields">
          <label><span>保存到素材组</span><select data-character-upload-group><option value="">请选择素材组</option></select></label>
          <label><span>素材名称</span><input type="text" maxlength="64" data-character-upload-name placeholder="选择图片后自动填写，可修改"></label>
        </div>
        <label class="shared-character-consent"><input type="checkbox" data-character-consent><span>我确认拥有该人物素材及本次生成用途的合法授权</span></label>
        <button type="button" data-character-upload>${uploadButtonLabel}</button>
        <p data-character-upload-status role="status" aria-live="polite">选择图片并确认预览后，点击上方按钮上传。审核通过后自动用于当前人物。</p>
      </section>
      <details data-character-new-group><summary>需要新的素材组？点此创建</summary><label><span>人像组名称</span><input type="text" maxlength="64" data-character-group-name placeholder="例如：女主角库"></label><label><span>说明（可选）</span><input type="text" maxlength="300" data-character-group-description placeholder="角色用途或项目说明"></label><button type="button" data-character-create-group>创建人像组</button></details>
    </div>`;
  const libraryBody = wardrobeActorLayout ? `
    <div class="shared-character-source-buttons" role="group" aria-label="人物身份来源">
      <button type="button" data-character-source-mode="upload">上传新人物</button>
      <button type="button" data-character-source-mode="existing" class="active">使用角色库已有角色</button>
    </div>
    <div class="shared-character-source-pane hidden" data-character-upload-pane>
      <div class="shared-character-pane-heading"><div><b>上传新人物</b><small>新图片无需事先在角色库中：选择本地图片，预览后上传即可。</small></div><span>选择图片 → 预览 → 上传使用</span></div>
      ${managerMarkup}
    </div>
    <div class="shared-character-source-pane" data-character-existing-pane>
      <div class="shared-character-pane-heading"><div><b>使用角色库已有角色</b><small>选择 Active 角色作为当前模式的人物身份；公司共享角色可直接使用，本账号角色可单独管理。</small></div><span>选择 / 管理</span></div>
      <div class="shared-character-selected" data-character-selected></div>
      <div class="shared-character-toolbar">
        <label><span>按人像组筛选</span><select data-character-group-filter><option value="">全部 AIGC 人像组</option></select></label>
        <button type="button" data-character-clear>不使用角色库人物</button>
      </div>
      <div class="shared-character-assets" data-character-assets aria-live="polite"></div>
      <div class="shared-character-delete-confirm" data-character-delete-confirm hidden></div>
    </div>` : `
    <div class="shared-character-selected" data-character-selected></div>
    <div class="shared-character-toolbar">
      <label><span>按人像组筛选</span><select data-character-group-filter><option value="">全部 AIGC 人像组</option></select></label>
      <button type="button" data-character-clear>不使用角色库人物</button>
    </div>
    <div class="shared-character-assets" data-character-assets aria-live="polite"></div>
    <div class="shared-character-delete-confirm" data-character-delete-confirm hidden></div>
    <details class="shared-character-manager">
      <summary>管理角色库：创建人像组、上传或删除人物</summary>
      ${managerMarkup}
    </details>`;
  panel.innerHTML = `
    <header class="shared-character-library-heading">
      <div><span>VOLCENGINE · AIGC CHARACTER ASSET</span><h3>${escapeHtml(config.title)}</h3><p>${escapeHtml(config.note)}</p></div>
      <button type="button" data-character-refresh>刷新角色库</button>
    </header>
    <div class="shared-character-library-state" data-character-state>正在读取火山角色库…</div>
    <div class="shared-character-library-state" data-character-preview-state role="status" hidden></div>
    ${libraryBody}`;
  const heading = host.querySelector(":scope > .panel-heading");
  if (heading) heading.insertAdjacentElement("afterend", panel);
  else host.prepend(panel);

  const state = { library: { configured: false, groups: [], assets: [] }, loading: false, pendingDelete: "", sourceMode: "existing", processingRefreshTimer: 0, previewFailures: new Set() };
  const pendingUploads = new Map(), uploadMessages = new Map();
  let uploading = false, generatedName = '';
  const within = (selector) => panel.querySelector(selector);
  const activeAssets = () => (state.library.assets || []).filter((asset) =>
    asset.status === "Active" && (!asset.asset_type || asset.asset_type === "Image")
  );
  let selectionTarget = null;
  const selectedUri = () => selectionTarget?.getUri ? selectionTarget.getUri() : personAssetInput.value.trim();
  const targetKey = target => target?.key || 'primary';
  const targetUri = target => target?.getUri ? target.getUri() : personAssetInput.value.trim();
  const changed = () => document.dispatchEvent(new Event('inline-cast-change'));
  function uploadMessage(key, text, error = false) {
    uploadMessages.set(key, {text, error}); renderUploadState();
  }
  function renderUploadState() {
    const key = targetKey(selectionTarget), pending = pendingUploads.get(key), message = uploadMessages.get(key);
    const button = within('[data-character-upload]');
    button.disabled = uploading || !!pending;
    button.textContent = uploading ? '正在上传图片…' : pending ? '图片已上传，等待审核…' : selectionTarget?.label ? `上传素材并用于${selectionTarget.label}` : uploadButtonLabel;
    const status = within('[data-character-upload-status]');
    status.textContent = message?.text || '选择图片并确认预览后，点击上方按钮上传。审核通过后自动用于当前人物。';
    status.classList.toggle('error', !!message?.error);
  }
  function applyUploadedAssets() {
    if (panel.inert) return;
    for (const [key, pending] of pendingUploads) {
      const asset = state.library.assets.find(item => item.uri === pending.uri);
      if (!asset) continue;
      if (asset.status === 'Active') {
        pendingUploads.delete(key);
        const applied = targetUri(pending.target) === pending.previousUri && setSelected(asset.uri, pending.target);
        uploadMessage(key, applied ? `“${pending.name}”已上传并用于${pending.target?.label || '当前人物'}。` : `“${pending.name}”已入库，可从角色库中选择。`);
        changed();
      } else if (asset.status && asset.status !== 'Processing') {
        pendingUploads.delete(key);
        uploadMessage(key, `“${pending.name}”未通过审核（${asset.status}），请更换图片后重新上传。`, true);
        changed();
      }
    }
    renderUploadState();
  }

  function setSourceMode(mode) {
    if (!wardrobeActorLayout) return;
    state.sourceMode = mode === "upload" ? "upload" : "existing";
    panel.querySelectorAll("[data-character-source-mode]").forEach((button) => {
      button.classList.toggle("active", button.dataset.characterSourceMode === state.sourceMode);
    });
    within("[data-character-upload-pane]").classList.toggle("hidden", state.sourceMode !== "upload");
    within("[data-character-existing-pane]").classList.toggle("hidden", state.sourceMode !== "existing");
  }

  function setSelected(uri, target = selectionTarget) {
    pendingUploads.delete(targetKey(target));
    if (target?.onSelect) {
      if (target.onSelect(uri || "") === false) return false;
      renderSelected(); renderAssets(); renderUploadState(); target.onSelected?.(uri || ""); return true;
    }
    personAssetInput.value = uri || "";
    try {
      if (uri) localStorage.setItem(storageKey, uri);
      else localStorage.removeItem(storageKey);
    } catch (_error) { /* Browser storage is optional. */ }
    personAssetInput.dispatchEvent(new Event("input", { bubbles: true }));
    personAssetInput.dispatchEvent(new Event("change", { bubbles: true }));
    renderSelected();
    renderAssets();
    renderUploadState();
    target?.onSelected?.(uri || "");
    return true;
  }

  function renderSelected() {
    const uri = selectedUri();
    const asset = (state.library.assets || []).find((item) => item.uri === uri);
    const group = (state.library.groups || []).find((item) => item.id === asset?.group_id);
    const label = escapeHtml(selectionTarget?.label ? `${selectionTarget.label}当前形象` : "当前生成使用");
    within("[data-character-selected]").innerHTML = uri
      ? `<div><span>${label}</span><b>${escapeHtml(asset?.name || "已绑定火山角色")}</b><small>${escapeHtml(group?.name || "AIGC 角色库")} · ${escapeHtml(uri)}</small></div><i>已选择</i>`
      : `<div><span>${label}</span><b>未选择角色库人物</b><small>${escapeHtml(config.empty)}</small></div><i class="empty">可选</i>`;
  }

  function groupOptions(includeAll = true) {
    const groups = (state.library.groups || []).filter((group) => group.group_type === "AIGC" && (includeAll || group.can_upload !== false));
    const first = includeAll ? '<option value="">全部 AIGC 人像组</option>' : '<option value="">请选择 AIGC 人像组</option>';
    return first + groups.map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`).join("");
  }

  function unavailablePreview(message = "图片未加载，可刷新角色库重试。") {
    return `<div class="shared-character-placeholder" title="${escapeHtml(message)}"><span>预览不可用</span><button type="button" data-character-preview-retry aria-label="重新读取角色库并重试预览">重试预览</button></div>`;
  }

  function renderPreviewState() {
    const count = state.previewFailures.size;
    const node = within("[data-character-preview-state]");
    node.hidden = count === 0;
    node.textContent = count ? `${count} 张角色预览未加载。点击“重试预览”可重新读取角色库；当前角色选择和本地素材会保留。` : "";
  }

  function renderAssets() {
    const filter = within("[data-character-group-filter]").value;
    const all = (state.library.assets || []).filter(asset => !asset.asset_type || asset.asset_type === "Image");
    const visible = all.filter((asset) => !filter || asset.group_id === filter);
    const groupMap = new Map((state.library.groups || []).map((group) => [group.id, group]));
    within("[data-character-assets]").innerHTML = visible.length ? visible.map((asset) => {
      const active = asset.status === "Active" && (!asset.asset_type || asset.asset_type === "Image");
      const selected = selectedUri() === asset.uri;
      const preview = asset.url && !state.previewFailures.has(asset.uri)
        ? `<img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.name)}" loading="lazy" data-character-preview="${escapeHtml(asset.uri)}">`
        : unavailablePreview(asset.preview_error || undefined);
      return `<article class="shared-character-card${selected ? " selected" : ""}">${preview}<div><b>${escapeHtml(asset.name)}</b><small>${escapeHtml(groupMap.get(asset.group_id)?.name || "未分组")} · ${escapeHtml(asset.status || "未知")}</small><small title="${escapeHtml(asset.uri)}">${escapeHtml(asset.uri)}</small></div><footer><button type="button" data-character-select="${escapeHtml(asset.uri)}" ${active ? "" : `data-character-unavailable="${escapeHtml(asset.status || "未激活")}"`}>${selected ? "正在使用" : active ? "使用此角色" : "尚不可用"}</button>${asset.can_delete === false ? `<small>公司共享角色</small>` : `<button type="button" data-character-delete="${escapeHtml(asset.id)}">删除</button>`}</footer></article>`;
    }).join("") : '<div class="shared-character-empty">当前筛选下没有人物素材。可在下方创建人像组并上传人物。</div>';
  }

  function renderLibrary() {
    const groups = (state.library.groups || []).filter((group) => group.group_type === "AIGC");
    const previousFilter = within("[data-character-group-filter]").value;
    const previousUploadGroup = within("[data-character-upload-group]").value;
    within("[data-character-group-filter]").innerHTML = groupOptions(true);
    within("[data-character-upload-group]").innerHTML = groupOptions(false);
    if (groups.some((group) => group.id === previousFilter)) within("[data-character-group-filter]").value = previousFilter;
    const uploadGroups = groups.filter((group) => group.can_upload !== false);
    if (uploadGroups.some((group) => group.id === previousUploadGroup)) within("[data-character-upload-group]").value = previousUploadGroup;
    else if (uploadGroups.length === 1) within("[data-character-upload-group]").value = uploadGroups[0].id;
    else {
      const selectedGroup = state.library.assets.find(asset => asset.uri === selectedUri())?.group_id;
      within('[data-character-upload-group]').value = uploadGroups.some(group => group.id === selectedGroup) ? selectedGroup : '';
    }
    within('[data-character-new-group]').open = uploadGroups.length === 0;
    const activeCount = activeAssets().length;
    const status = state.library.configured
      ? `${groups.length} 个人像组 · ${activeCount} 个可用角色${state.library.stale ? " · 当前显示缓存" : ""}`
      : "角色库尚未配置";
    const message = state.library.message ? ` · ${state.library.message}` : "";
    within("[data-character-state]").textContent = `${status}${message}`;
    applyUploadedAssets();
    renderSelected();
    renderAssets();
    scheduleProcessingRefresh();
    document.dispatchEvent(new Event("character-library-loaded"));
  }

  function scheduleProcessingRefresh() {
    if (!wardrobeActorLayout) return;
    if (state.processingRefreshTimer) window.clearTimeout(state.processingRefreshTimer);
    state.processingRefreshTimer = 0;
    const hasProcessing = pendingUploads.size || (state.library.assets || []).some((asset) => asset.status === "Processing");
    if (!hasProcessing) return;
    state.processingRefreshTimer = window.setTimeout(async () => {
      state.processingRefreshTimer = 0;
      if (document.hidden) return scheduleProcessingRefresh();
      await loadLibrary();
    }, 8000);
  }

  async function loadLibrary() {
    if (state.loading) return;
    state.loading = true;
    const button = within("[data-character-refresh]");
    button.disabled = true;
    button.textContent = "正在刷新…";
    try {
      state.library = await request(`/api/character-library?_=${Date.now()}`, { cache: "no-store" });
      state.previewFailures.clear();
      renderPreviewState();
      renderLibrary();
    } catch (error) {
      // A failed read must not replace existing cards, filters, selections or files.
      within("[data-character-state]").textContent = `角色库刷新失败：${error.message}。现有选择和素材已保留，请稍后重试。`;
      notify(error.message, true);
    } finally {
      state.loading = false;
      button.disabled = false;
      button.textContent = "刷新角色库";
    }
  }

  async function createGroup() {
    const name = within("[data-character-group-name]").value.trim();
    const description = within("[data-character-group-description]").value.trim();
    if (!name) return notify("请填写新 AIGC 人像组名称。", true);
    const button = within("[data-character-create-group]");
    button.disabled = true;
    try {
      const result = await request("/api/character-library/groups", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, description }),
      });
      within("[data-character-group-name]").value = "";
      within("[data-character-group-description]").value = "";
      await loadLibrary();
      within("[data-character-upload-group]").value = result.group_id || "";
      notify(result.message || "AIGC 人像组已创建。", false);
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
  }

  async function uploadAsset() {
    const target = selectionTarget, key = targetKey(target);
    if (uploading || pendingUploads.has(key)) return;
    const groupId = within("[data-character-upload-group]").value;
    const name = within("[data-character-upload-name]").value.trim();
    const file = within("[data-character-upload-file]").files[0];
    const invalid = (message, selector) => { uploadMessage(key, message, true); within(selector).focus(); };
    if (!file) return invalid('请先点击“选择图片素材”，选择电脑中的新人物图片。', '[data-character-pick]');
    if (!groupId) return invalid('请选择保存到哪个素材组；已有素材组可以直接使用，无需重新创建。', '[data-character-upload-group]');
    if (!name) return invalid('请填写人物素材名称。', '[data-character-upload-name]');
    if (!within("[data-character-consent]").checked) return invalid('请勾选人物素材授权确认。', '[data-character-consent]');
    uploading = true;
    const pending = {target, previousUri: targetUri(target), uri: '', name};
    pendingUploads.set(key, pending);
    uploadMessage(key, `正在上传“${name}”…`); changed();
    try {
      const form = new FormData();
      form.append("group_id", groupId);
      form.append("name", name);
      form.append("asset_file", file);
      const result = await request("/api/character-library/assets", { method: "POST", body: form });
      pending.uri = result.uri || (result.asset_id ? `asset://${result.asset_id}` : '');
      if (!pending.uri) throw Error('未收到素材入库结果，请刷新角色库核对后再试。');
      if (within('[data-character-upload-file]').files[0] === file) within('[data-character-upload-file]').value = '';
      uploadMessage(key, `“${name}”已上传，正在审核；通过后自动用于${target?.label || '当前人物'}。`);
      await loadLibrary();
      scheduleProcessingRefresh();
    } catch (error) {
      if (pendingUploads.get(key) === pending) pendingUploads.delete(key);
      uploadMessage(key, error.message, true);
    } finally { uploading = false; renderUploadState(); changed(); }
  }

  function fileChosen() {
    const input = within('[data-character-upload-file]'), file = input.files[0];
    if (!file) return;
    const key = targetKey(selectionTarget);
    if (!/\.(jpg|jpeg|png|webp)$/i.test(file.name) || !file.size || file.size > 30 * 1024 * 1024) {
      input.value = ''; window.DepthFlowUploadPreview?.refresh(input);
      return uploadMessage(key, '请选择不超过 30MB 的 JPG、PNG 或 WEBP 图片。', true);
    }
    within('[data-character-file-name]').textContent = `已选择：${file.name}（${(file.size / 1024 / 1024).toFixed(2)} MB）`;
    within('[data-character-pick]').textContent = '重新选择图片素材';
    const nameInput = within('[data-character-upload-name]');
    if (!nameInput.value.trim() || nameInput.value === generatedName) {
      generatedName = file.name.replace(/\.[^.]+$/, '').slice(0, 64); nameInput.value = generatedName;
    }
    window.DepthFlowUploadPreview?.refresh(input);
    if (!pendingUploads.has(key)) uploadMessage(key, '图片已选好，请确认预览和保存的素材组，再点击“上传素材并用于人物”按钮。');
  }

  function askDelete(assetId) {
    const asset = (state.library.assets || []).find((item) => item.id === assetId);
    if (!asset || asset.can_delete === false) return;
    state.pendingDelete = assetId;
    const node = within("[data-character-delete-confirm]");
    node.hidden = false;
    node.innerHTML = `<div><b>永久删除“${escapeHtml(asset.name)}”？</b><small>${escapeHtml(asset.uri)} · 删除后旧任务可能无法再次生成。</small></div><button type="button" data-character-delete-cancel>取消</button><button type="button" data-character-delete-confirmed>确认永久删除</button>`;
    node.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function cancelDelete() {
    state.pendingDelete = "";
    const node = within("[data-character-delete-confirm]");
    node.hidden = true;
    node.innerHTML = "";
  }

  async function confirmDelete() {
    const asset = (state.library.assets || []).find((item) => item.id === state.pendingDelete);
    if (!asset || asset.can_delete === false) return cancelDelete();
    const button = within("[data-character-delete-confirmed]");
    button.disabled = true;
    button.textContent = "正在删除…";
    try {
      await request(`/api/character-library/assets/${encodeURIComponent(asset.id)}`, { method: "DELETE" });
      if (selectedUri() === asset.uri) setSelected("");
      cancelDelete();
      await loadLibrary();
      notify(`已从角色库删除：${asset.name}`, false);
    } catch (error) { button.disabled = false; button.textContent = "确认永久删除"; notify(error.message, true); }
  }

  async function selectAsset(button) {
    const uri = button.dataset.characterSelect;
    const target = selectionTarget;
    if (button.dataset.characterUnavailable && wardrobeActorLayout) {
      notify("正在向火山查询该人物的最新审核状态…", false);
      await loadLibrary();
      const refreshed = (state.library.assets || []).find((asset) => asset.uri === uri);
      const active = refreshed?.status === "Active" && (!refreshed.asset_type || refreshed.asset_type === "Image");
      if (active) {
        if (!setSelected(uri, target)) return;
        return notify("该人物已经通过审核，现已选择使用。", false);
      }
      return notify(`该人物尚不可用：${refreshed?.status || button.dataset.characterUnavailable}。请等待状态变为 Active。`, true);
    }
    if (button.dataset.characterUnavailable) {
      return notify(`该人物尚不可用：${button.dataset.characterUnavailable}。请等待状态变为 Active。`, true);
    }
    if (!setSelected(uri, target)) return;
    const name = state.library.assets.find(asset => asset.uri === uri)?.name || "所选角色";
    notify(target?.label ? `${target.label}已更新为：${name}` : "已选择火山角色；生成时会将该 Asset 作为人物身份参考。", false);
  }

  // Image errors do not bubble; capture them without an automatic retry loop.
  panel.addEventListener("error", (event) => {
    const image = event.target;
    if (!image.matches?.("img[data-character-preview]")) return;
    state.previewFailures.add(image.dataset.characterPreview);
    const placeholder = document.createElement("div");
    placeholder.innerHTML = unavailablePreview();
    image.replaceWith(placeholder.firstElementChild);
    renderPreviewState();
  }, true);

  panel.addEventListener("click", async (event) => {
    const sourceMode = event.target.closest("[data-character-source-mode]");
    if (sourceMode) setSourceMode(sourceMode.dataset.characterSourceMode);
    else if (event.target.closest('[data-character-pick]')) within('[data-character-upload-file]').click();
    else if (event.target.closest("[data-character-refresh]") || event.target.closest("[data-character-preview-retry]")) loadLibrary();
    else if (event.target.closest("[data-character-clear]")) { setSelected(""); notify("已取消角色库人物绑定。", false); }
    else if (event.target.closest("[data-character-create-group]")) createGroup();
    else if (event.target.closest("[data-character-upload]")) uploadAsset();
    else if (event.target.closest("[data-character-delete-cancel]")) cancelDelete();
    else if (event.target.closest("[data-character-delete-confirmed]")) confirmDelete();
    else {
      const select = event.target.closest("[data-character-select]");
      const remove = event.target.closest("[data-character-delete]");
      if (select) {
        await selectAsset(select);
      } else if (remove) askDelete(remove.dataset.characterDelete);
    }
  });
  within("[data-character-group-filter]").addEventListener("change", renderAssets);
  within('[data-character-upload-file]').addEventListener('change', fileChosen);
  const fileArea = within('[data-character-file-area]');
  fileArea.addEventListener('dragover', event => { event.preventDefault(); });
  fileArea.addEventListener('drop', event => {
    event.preventDefault();
    if (uploading || !event.dataTransfer?.files.length) return;
    const input = within('[data-character-upload-file]'); input.files = event.dataTransfer.files;
    input.dispatchEvent(new Event('change', {bubbles:true}));
  });
  personAssetInput.addEventListener("input", () => { renderSelected(); renderAssets(); });
  document.querySelector("#personImage")?.addEventListener("change", (event) => {
    if (event.target.files[0] && selectedUri()) setSelected("");
  });
  renderSelected();
  setSourceMode("existing");
  loadLibrary();
  window.DepthFlowCharacterLibrary = { refresh: loadLibrary, selected: () => personAssetInput.value.trim(), assets: () => state.library.assets || [],
    lock(value) { panel.inert = !!value; },
    pickFile() { within('[data-character-upload-file]').click(); },
    uploadProblem() { return uploading ? '人物图片正在上传，请等待上传完成。' : pendingUploads.size ? '新人物图片正在审核，通过后自动绑定，请稍候或刷新角色库。' : ''; },
    choose(target, options = {}) {
      selectionTarget = target;
      panel.querySelector('h3').textContent = target ? `为${target.label}上传或选择形象` : config.title;
      if (options.sourceMode) setSourceMode(options.sourceMode);
      if (options.sourceMode === 'upload' && !within('[data-character-upload-group]').value) {
        const group = state.library.assets.find(asset => asset.uri === selectedUri())?.group_id;
        if (state.library.groups.some(item => item.id === group && item.group_type === 'AIGC')) within('[data-character-upload-group]').value = group;
      }
      renderSelected(); renderAssets(); renderUploadState();
    }
  };
})();
