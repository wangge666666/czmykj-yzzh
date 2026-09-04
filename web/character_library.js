(function () {
  const path = window.location.pathname.replace(/\/$/, "");
  const configs = {
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
  const wardrobeActorLayout = path === "/projects/wardrobe" || path === "/wardrobe-swap";
  const uploadButtonLabel = wardrobeActorLayout ? "审核并上传此人物到角色库" : "审核并上传角色库";
  const managerMarkup = `
    <div class="shared-character-manager-grid">
      <section><b>创建 AIGC 人像组</b><label><span>人像组名称</span><input type="text" maxlength="64" data-character-group-name placeholder="例如：女主角库"></label><label><span>说明（可选）</span><input type="text" maxlength="300" data-character-group-description placeholder="角色用途或项目说明"></label><button type="button" data-character-create-group>创建人像组</button></section>
      <section><b>上传人物到角色库</b><label><span>目标人像组</span><select data-character-upload-group><option value="">请选择人像组</option></select></label><label><span>素材名称</span><input type="text" maxlength="64" data-character-upload-name placeholder="例如：便利店女店员"></label><label class="shared-character-file"><span>人物图片</span><input type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" data-character-upload-file><small data-character-file-name>JPG / PNG / WEBP，最大 30MB</small></label><label class="shared-character-consent"><input type="checkbox" data-character-consent><span>我确认拥有该人物素材及本次生成用途的合法授权</span></label><button type="button" data-character-upload>${uploadButtonLabel}</button></section>
    </div>`;
  const libraryBody = wardrobeActorLayout ? `
    <div class="shared-character-source-buttons" role="group" aria-label="人物身份来源">
      <button type="button" data-character-source-mode="upload">上传新人物</button>
      <button type="button" data-character-source-mode="existing" class="active">使用角色库已有角色</button>
    </div>
    <div class="shared-character-source-pane hidden" data-character-upload-pane>
      <div class="shared-character-pane-heading"><div><b>上传新人物</b><small>创建或选择 AIGC 人像组，上传图片并等待状态变为 Active。</small></div><span>上传并审核</span></div>
      ${managerMarkup}
    </div>
    <div class="shared-character-source-pane" data-character-existing-pane>
      <div class="shared-character-pane-heading"><div><b>使用角色库已有角色</b><small>选择 Active 角色作为当前模式的人物身份；每张角色卡都可单独删除。</small></div><span>选择 / 删除</span></div>
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
    ${libraryBody}`;
  const heading = host.querySelector(":scope > .panel-heading");
  if (heading) heading.insertAdjacentElement("afterend", panel);
  else host.prepend(panel);

  const state = { library: { configured: false, groups: [], assets: [] }, loading: false, pendingDelete: "", sourceMode: "existing", processingRefreshTimer: 0 };
  const within = (selector) => panel.querySelector(selector);
  const activeAssets = () => (state.library.assets || []).filter((asset) =>
    asset.status === "Active" && (!asset.asset_type || asset.asset_type === "Image")
  );
  const selectedUri = () => personAssetInput.value.trim();

  function setSourceMode(mode) {
    if (!wardrobeActorLayout) return;
    state.sourceMode = mode === "upload" ? "upload" : "existing";
    panel.querySelectorAll("[data-character-source-mode]").forEach((button) => {
      button.classList.toggle("active", button.dataset.characterSourceMode === state.sourceMode);
    });
    within("[data-character-upload-pane]").classList.toggle("hidden", state.sourceMode !== "upload");
    within("[data-character-existing-pane]").classList.toggle("hidden", state.sourceMode !== "existing");
  }

  function setSelected(uri) {
    personAssetInput.value = uri || "";
    try {
      if (uri) localStorage.setItem(storageKey, uri);
      else localStorage.removeItem(storageKey);
    } catch (_error) { /* Browser storage is optional. */ }
    personAssetInput.dispatchEvent(new Event("input", { bubbles: true }));
    personAssetInput.dispatchEvent(new Event("change", { bubbles: true }));
    renderSelected();
    renderAssets();
  }

  function renderSelected() {
    const uri = selectedUri();
    const asset = (state.library.assets || []).find((item) => item.uri === uri);
    const group = (state.library.groups || []).find((item) => item.id === asset?.group_id);
    within("[data-character-selected]").innerHTML = uri
      ? `<div><span>当前生成使用</span><b>${escapeHtml(asset?.name || "已绑定火山角色")}</b><small>${escapeHtml(group?.name || "AIGC 角色库")} · ${escapeHtml(uri)}</small></div><i>已选择</i>`
      : `<div><span>当前生成使用</span><b>未选择角色库人物</b><small>${escapeHtml(config.empty)}</small></div><i class="empty">可选</i>`;
  }

  function groupOptions(includeAll = true) {
    const groups = (state.library.groups || []).filter((group) => group.group_type === "AIGC");
    const first = includeAll ? '<option value="">全部 AIGC 人像组</option>' : '<option value="">请选择 AIGC 人像组</option>';
    return first + groups.map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`).join("");
  }

  function renderAssets() {
    const filter = within("[data-character-group-filter]").value;
    const all = state.library.assets || [];
    const visible = all.filter((asset) => !filter || asset.group_id === filter);
    const groupMap = new Map((state.library.groups || []).map((group) => [group.id, group]));
    within("[data-character-assets]").innerHTML = visible.length ? visible.map((asset) => {
      const active = asset.status === "Active" && (!asset.asset_type || asset.asset_type === "Image");
      const selected = selectedUri() === asset.uri;
      const preview = asset.url
        ? `<img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.name)}" loading="lazy">`
        : '<div class="shared-character-placeholder">ARK</div>';
      return `<article class="shared-character-card${selected ? " selected" : ""}">${preview}<div><b>${escapeHtml(asset.name)}</b><small>${escapeHtml(groupMap.get(asset.group_id)?.name || "未分组")} · ${escapeHtml(asset.status || "未知")}</small><small title="${escapeHtml(asset.uri)}">${escapeHtml(asset.uri)}</small></div><footer><button type="button" data-character-select="${escapeHtml(asset.uri)}" ${active ? "" : `data-character-unavailable="${escapeHtml(asset.status || "未激活")}"`}>${selected ? "正在使用" : active ? "使用此角色" : "尚不可用"}</button><button type="button" data-character-delete="${escapeHtml(asset.id)}">删除</button></footer></article>`;
    }).join("") : '<div class="shared-character-empty">当前筛选下没有人物素材。可在下方创建人像组并上传人物。</div>';
  }

  function renderLibrary() {
    const groups = (state.library.groups || []).filter((group) => group.group_type === "AIGC");
    const previousFilter = within("[data-character-group-filter]").value;
    const previousUploadGroup = within("[data-character-upload-group]").value;
    within("[data-character-group-filter]").innerHTML = groupOptions(true);
    within("[data-character-upload-group]").innerHTML = groupOptions(false);
    if (groups.some((group) => group.id === previousFilter)) within("[data-character-group-filter]").value = previousFilter;
    if (groups.some((group) => group.id === previousUploadGroup)) within("[data-character-upload-group]").value = previousUploadGroup;
    else if (groups.length === 1) within("[data-character-upload-group]").value = groups[0].id;
    const activeCount = activeAssets().length;
    const status = state.library.configured
      ? `${groups.length} 个人像组 · ${activeCount} 个可用角色${state.library.stale ? " · 当前显示缓存" : ""}`
      : "角色库尚未配置";
    const message = state.library.message ? ` · ${state.library.message}` : "";
    within("[data-character-state]").textContent = `${status}${message}`;
    renderSelected();
    renderAssets();
    scheduleProcessingRefresh();
  }

  function scheduleProcessingRefresh() {
    if (!wardrobeActorLayout) return;
    if (state.processingRefreshTimer) window.clearTimeout(state.processingRefreshTimer);
    state.processingRefreshTimer = 0;
    const hasProcessing = (state.library.assets || []).some((asset) => asset.status === "Processing");
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
      renderLibrary();
    } catch (error) {
      state.library = { configured: false, groups: [], assets: [], message: error.message };
      renderLibrary();
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
    const groupId = within("[data-character-upload-group]").value;
    const name = within("[data-character-upload-name]").value.trim();
    const file = within("[data-character-upload-file]").files[0];
    if (!groupId) return notify("请先选择目标 AIGC 人像组。", true);
    if (!name) return notify("请填写人物素材名称。", true);
    if (!file) return notify("请选择需要上传的人物图片。", true);
    if (!within("[data-character-consent]").checked) return notify("请先确认人物素材授权。", true);
    const button = within("[data-character-upload]");
    button.disabled = true;
    button.textContent = "正在提交审核…";
    try {
      const form = new FormData();
      form.append("group_id", groupId);
      form.append("name", name);
      form.append("asset_file", file);
      const result = await request("/api/character-library/assets", { method: "POST", body: form });
      within("[data-character-upload-name]").value = "";
      within("[data-character-upload-file]").value = "";
      within("[data-character-file-name]").textContent = "JPG / PNG / WEBP，最大 30MB";
      await loadLibrary();
      setSourceMode("existing");
      notify(result.message || "人物已提交角色库审核。", false);
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; button.textContent = uploadButtonLabel; }
  }

  function askDelete(assetId) {
    const asset = (state.library.assets || []).find((item) => item.id === assetId);
    if (!asset) return;
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
    if (!asset) return cancelDelete();
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
    if (button.dataset.characterUnavailable && wardrobeActorLayout) {
      notify("正在向火山查询该人物的最新审核状态…", false);
      await loadLibrary();
      const refreshed = (state.library.assets || []).find((asset) => asset.uri === uri);
      const active = refreshed?.status === "Active" && (!refreshed.asset_type || refreshed.asset_type === "Image");
      if (active) {
        setSelected(uri);
        return notify("该人物已经通过审核，现已选择使用。", false);
      }
      return notify(`该人物尚不可用：${refreshed?.status || button.dataset.characterUnavailable}。请等待状态变为 Active。`, true);
    }
    if (button.dataset.characterUnavailable) {
      return notify(`该人物尚不可用：${button.dataset.characterUnavailable}。请等待状态变为 Active。`, true);
    }
    setSelected(uri);
    notify("已选择火山角色；生成时会将该 Asset 作为人物身份参考。", false);
  }

  panel.addEventListener("click", async (event) => {
    const sourceMode = event.target.closest("[data-character-source-mode]");
    if (sourceMode) setSourceMode(sourceMode.dataset.characterSourceMode);
    else if (event.target.closest("[data-character-refresh]")) loadLibrary();
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
  within("[data-character-upload-file]").addEventListener("change", (event) => {
    within("[data-character-file-name]").textContent = event.target.files[0]?.name || "JPG / PNG / WEBP，最大 30MB";
  });
  personAssetInput.addEventListener("input", () => { renderSelected(); renderAssets(); });
  document.querySelector("#personImage")?.addEventListener("change", (event) => {
    if (event.target.files[0] && selectedUri()) setSelected("");
  });
  renderSelected();
  setSourceMode("existing");
  loadLibrary();
  window.DepthFlowCharacterLibrary = { refresh: loadLibrary, selected: selectedUri };
})();
