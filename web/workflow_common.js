(function () {
  const registry = window.DepthFlowRegistry;
  const path = window.location.pathname.replace(/\/$/, "");
  const project = registry && Object.values(registry.projects).find((item) => item.path === path);

  function commonToast(message, isError = false) {
    if (typeof window.toast === "function") return window.toast(message, isError);
    let node = document.querySelector(".common-toast");
    if (!node) {
      node = document.createElement("div");
      node.className = "common-toast";
      node.setAttribute("role", "status");
      document.body.append(node);
    }
    node.textContent = message;
    node.classList.toggle("error", isError);
    node.classList.add("show");
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.classList.remove("show"), 5200);
  }

  function formatApiError(payload, response) {
    const message = payload?.error || payload?.message || `请求失败（HTTP ${response?.status || "未知"}）`;
    const category = payload?.error_category;
    if (!category?.label || String(message).startsWith(`[${category.label}]`)) return String(message);
    return `[${category.label}] ${message}`;
  }
  function formatJobError(job, fallback = "任务失败。") {
    const message = job?.error || fallback;
    const label = job?.error_category?.label;
    return label && !String(message).startsWith(`[${label}]`) ? `[${label}] ${message}` : String(message);
  }
  window.DepthFlowUI = { ...(window.DepthFlowUI || {}), toast: commonToast, formatApiError, formatJobError };

  const workspaceEnabled = ["long-video", "real-long-video"].includes(
    Object.entries(registry?.projects || {}).find(([, item]) => item === project)?.[0] || "",
  );

  function escapeMarkup(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  function createWorkspaceContext() {
    if (!workspaceEnabled) return null;
    const apiBase = project.path.includes("real-long-video") ? "/api/real-long-video" : "/api/long-video";
    const storageKey = `depthflow.activeWorkspace.${project.path}`;
    const context = {
      apiBase,
      items: [],
      activeId: "",
      listeners: new Set(),
      active() { return this.items.find((item) => item.id === this.activeId) || this.items[0] || null; },
      setActive(id) {
        if (!this.items.some((item) => item.id === id)) return false;
        this.activeId = id;
        try { localStorage.setItem(storageKey, id); } catch (_error) { /* Browser storage is optional. */ }
        this.emit();
        return true;
      },
      appendToForm(formData) {
        const active = this.active();
        if (active?.id) formData.set("workspace_id", active.id);
      },
      onChange(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); },
      emit() { this.listeners.forEach((listener) => listener(this)); },
      async request(url, options = {}) {
        const response = await fetch(url, options);
        let payload = {};
        try { payload = await response.json(); } catch (_error) { /* handled below */ }
        if (!response.ok) throw new Error(formatApiError(payload, response));
        return payload;
      },
      async refresh() {
        const result = await this.request(`${apiBase}/workspaces`);
        this.items = Array.isArray(result.workspaces) ? result.workspaces : [];
        let saved = "";
        try { saved = localStorage.getItem(storageKey) || ""; } catch (_error) { /* ignore */ }
        if (!this.items.some((item) => item.id === this.activeId)) {
          this.activeId = this.items.some((item) => item.id === saved) ? saved : (this.items[0]?.id || "");
        }
        if (this.activeId) {
          try { localStorage.setItem(storageKey, this.activeId); } catch (_error) { /* ignore */ }
        }
        this.emit();
        return this.items;
      },
    };
    context.ready = context.refresh().catch((error) => {
      commonToast(`重绘项目列表读取失败：${error.message}`, true);
      return [];
    });
    return context;
  }

  const workspaceContext = createWorkspaceContext();
  if (workspaceContext) window.DepthFlowWorkspaces = workspaceContext;

  const fallbackReasons = {
    mosaicBtn: "请先完成分镜分析，且等待当前任务结束。",
    performanceBtn: "请先完成分镜分析、勾选云端分析同意项，并等待当前任务结束。",
    whiteModelBtn: "请先完成打码视频、人工核验及台词表演校订。",
    generateBtn: "请先完成当前项目要求的人物、服装、场景和前置分析。",
    regenerateAllBtn: "请先完成分镜生成准备；整段生成模式下不能逐镜重生。",
    reuseDepthBtn: "请先生成或恢复可用的动作参考视频，并补齐替换素材。",
    prepareBtn: "当前任务正在运行，请完成后再继续。",
    fullBtn: "当前任务正在运行，或必需素材尚未补齐。",
    addActorBtn: "当前已达到人物数量上限，或当前任务正在运行。",
    renameCharacterGroupBtn: "请先选择一个人像组，并等待当前任务结束。",
    deleteCharacterGroupBtn: "请先选择一个可删除的人像组，并等待当前任务结束。",
    pauseGenerateBtn: "当前没有正在运行的成片生成任务。",
    restoreArchiveBtn: "请先选择一个已有存档，且当前不能有运行中的任务。",
    deleteArchiveBtn: "请先选择一个已有存档，且当前不能有运行中的任务。",
  };

  function disabledReason(button) {
    const text = button.textContent.trim();
    const contextual = text.includes("正在加载") ? "当前媒体预览正在加载，请等待加载完成。"
      : text === "删除" ? "当前素材不可删除，或当前任务正在使用该素材。"
      : text.includes("重新生成此镜") ? "请先完成本镜所需的白膜、人物、服装、场景与人工确认。"
      : text.includes("上传此人物") ? "请切换为“上传新人物”、选择图片并确认授权。"
      : "";
    return String(button.dataset.blockers || button.dataset.disabledReason || button.title || fallbackReasons[button.id] || contextual || "当前按钮缺少前置条件；请检查本步骤上方标红或未完成项目。").trim();
  }

  function syncDisabledReasons() {
    let anonymousIndex = 0;
    for (const button of document.querySelectorAll("button")) {
      if (!button.id && !button.dataset.reasonKey) button.dataset.reasonKey = `anonymous-${anonymousIndex++}`;
      const key = button.id || button.dataset.reasonKey;
      let note = button.parentElement?.querySelector(`:scope > .disabled-reason[data-for="${CSS.escape(key)}"]`);
      if (button.disabled) {
        if (!note) {
          note = document.createElement("small");
          note.className = "disabled-reason";
          note.dataset.for = key;
          button.insertAdjacentElement("afterend", note);
        }
        const reason = disabledReason(button);
        if (note.textContent !== reason) note.textContent = reason;
      } else if (note) note.remove();
    }
  }

  document.addEventListener("pointerdown", (event) => {
    const button = event.target.closest?.("button:disabled");
    if (!button) return;
    commonToast(`还不能执行：${disabledReason(button)}`, true);
  }, true);

  function readableModel(value) {
    const text = String(value || "");
    if (text.includes("seedance-2-5")) return "Seedance 2.5";
    if (text.includes("seedance-2-0")) return "Seedance 2.0";
    if (text.includes("seedream-5")) return "Seedream 5.0";
    return text || "按页面设置";
  }

  function syncPaidActionMeta() {
    if (!project?.paidActions) return;
    for (const [buttonId, spec] of Object.entries(project.paidActions)) {
      const button = document.getElementById(buttonId);
      if (!button) continue;
      let note = button.parentElement?.querySelector(`:scope > .paid-action-meta[data-for="${buttonId}"]`);
      if (!note) {
        note = document.createElement("small");
        note.className = "paid-action-meta";
        note.dataset.for = buttonId;
        button.insertAdjacentElement("afterend", note);
      }
      const model = document.querySelector("#model")?.value;
      const resolution = document.querySelector("#resolution")?.value;
      const output = spec.fixedOutput || (spec.usePageSettings === false ? "" : [readableModel(model), resolution].filter(Boolean).join(" · "));
      const html = `<strong>${spec.calls}</strong> · ${spec.kind}${output ? ` · ${output}` : ""}`;
      if (note.innerHTML !== html) note.innerHTML = html;
    }
  }

  function installShell() {
    if (!project || project.ownShell) return;
    const main = document.querySelector("main");
    const form = document.querySelector(project.form);
    if (!main || !form || document.querySelector(".unified-workflow-shell")) return;
    const shell = document.createElement("div");
    shell.className = "unified-workflow-shell";
    const sidebar = document.createElement("aside");
    sidebar.className = "unified-workflow-sidebar";
    sidebar.innerHTML = `<a href="/">← 返回项目首页</a><header><span>PROJECT ${project.number}</span><b>${project.title}</b><small>${project.summary}</small></header><nav class="unified-workflow-nav"></nav>`;
    const stage = document.createElement("div");
    stage.className = "unified-workflow-stage";
    form.parentNode.insertBefore(shell, form);
    shell.append(sidebar, stage);
    stage.append(form);
    for (const step of project.steps.filter((item) => item.outsideForm)) {
      for (const selector of step.selectors) {
        const node = main.querySelector(selector);
        if (node) stage.append(node);
      }
    }
    const footer = document.createElement("div");
    footer.className = "unified-workflow-footer";
    footer.innerHTML = `<button type="button" data-workflow-prev>上一步</button><span data-workflow-position></span><button type="button" data-workflow-next>下一步</button>`;
    stage.append(footer);
    const nav = sidebar.querySelector("nav");
    project.steps.forEach((step, index) => nav.insertAdjacentHTML("beforeend", `<button type="button" data-workflow-step="${step.id}"><i>${String(index + 1).padStart(2, "0")}</i><span><b>${step.label}</b><small>${step.hint}</small></span></button>`));

    function showStep(stepId, updateHash = true) {
      const selected = project.steps.find((item) => item.id === stepId) || project.steps[0];
      for (const step of project.steps) {
        for (const selector of step.selectors) document.querySelectorAll(selector).forEach((node) => node.classList.toggle("unified-step-hidden", step.id !== selected.id));
      }
      nav.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.workflowStep === selected.id));
      const index = project.steps.indexOf(selected);
      footer.querySelector("[data-workflow-prev]").disabled = index <= 0;
      footer.querySelector("[data-workflow-next]").disabled = index >= project.steps.length - 1;
      footer.querySelector("[data-workflow-position]").textContent = `${index + 1} / ${project.steps.length} · ${selected.label}`;
      footer.dataset.activeStep = selected.id;
      if (updateHash) history.replaceState(null, "", `#${selected.id}`);
      window.scrollTo({ top: Math.max(0, shell.offsetTop - 12), behavior: "smooth" });
    }
    nav.addEventListener("click", (event) => {
      const button = event.target.closest("[data-workflow-step]");
      if (button) showStep(button.dataset.workflowStep);
    });
    footer.querySelector("[data-workflow-prev]").addEventListener("click", () => {
      const index = project.steps.findIndex((item) => item.id === footer.dataset.activeStep);
      if (index > 0) showStep(project.steps[index - 1].id);
    });
    footer.querySelector("[data-workflow-next]").addEventListener("click", () => {
      const index = project.steps.findIndex((item) => item.id === footer.dataset.activeStep);
      if (index >= 0 && index < project.steps.length - 1) showStep(project.steps[index + 1].id);
    });
    const initial = location.hash.slice(1);
    showStep(project.steps.some((item) => item.id === initial) ? initial : project.steps[0].id, false);
    const activeTask = stage.querySelector(".active-task");
    if (activeTask) new MutationObserver(() => {
      if (!activeTask.classList.contains("hidden") && ["queued", "running"].some((value) => activeTask.textContent.includes(value))) showStep("progress");
    }).observe(activeTask, { attributes: true, childList: true, subtree: true });
  }

  function installWorkspaceManager() {
    if (!workspaceContext || document.querySelector(".long-workspace-manager")) return Boolean(document.querySelector(".long-workspace-manager"));
    const sidebar = document.querySelector(".workflow-sidebar, .unified-workflow-sidebar");
    const nav = sidebar?.querySelector(".workflow-step-nav, .unified-workflow-nav");
    if (!sidebar || !nav) return false;
    const manager = document.createElement("section");
    manager.className = "long-workspace-manager";
    manager.innerHTML = `
      <div class="long-workspace-heading"><span><b>重绘项目</b><small data-workspace-summary>正在读取…</small></span><button type="button" data-workspace-create>＋ 新建</button></div>
      <div class="long-workspace-list" data-workspace-list></div>
      <div class="long-workspace-actions"><button type="button" data-workspace-rename>重命名</button><button type="button" data-workspace-delete>删除项目</button></div>`;
    sidebar.insertBefore(manager, nav);

    const statusLabel = (status) => ({
      new: "未开始", queued: "排队中", running: "运行中", succeeded: "已完成",
      failed: "失败", paused: "已暂停", awaiting_approval: "待确认", saved: "已保存",
    })[status] || "待继续";
    const renderManager = () => {
      const running = workspaceContext.items.filter((item) => ["queued", "running"].includes(item.status)).length;
      manager.querySelector("[data-workspace-summary]").textContent = `${workspaceContext.items.length} 个项目${running ? ` · ${running} 个运行中` : ""}`;
      manager.querySelector("[data-workspace-list]").innerHTML = workspaceContext.items.map((item) => {
        const detail = item.job_id
          ? `${statusLabel(item.status)} · ${Number(item.shot_count || 0)}镜 · ${Number(item.progress || 0)}%`
          : "未上传原片";
        return `<button type="button" class="long-workspace-item${item.id === workspaceContext.activeId ? " active" : ""}" data-workspace-select="${escapeMarkup(item.id)}"><i class="workspace-status ${escapeMarkup(item.status || "new")}"></i><span><b>${escapeMarkup(item.name)}</b><small>${escapeMarkup(detail)}</small></span></button>`;
      }).join("");
      const active = workspaceContext.active();
      manager.querySelector("[data-workspace-rename]").disabled = !active;
      manager.querySelector("[data-workspace-delete]").disabled = !active;
    };
    workspaceContext.onChange(renderManager);
    workspaceContext.ready.then(renderManager);
    renderManager();

    const dialog = document.createElement("div");
    dialog.className = "workspace-operation-modal";
    dialog.hidden = true;
    dialog.innerHTML = `
      <button type="button" class="workspace-operation-backdrop" data-workspace-dialog-cancel aria-label="关闭对话框"></button>
      <section class="workspace-operation-panel" role="dialog" aria-modal="true" aria-labelledby="workspace-operation-title" aria-describedby="workspace-operation-description">
        <form data-workspace-dialog-form>
          <header><span>重绘项目</span><h3 id="workspace-operation-title"></h3><p id="workspace-operation-description"></p></header>
          <label data-workspace-dialog-field><span data-workspace-dialog-label>项目名称</span><input type="text" maxlength="80" autocomplete="off" data-workspace-dialog-input></label>
          <p class="workspace-operation-error" role="alert" data-workspace-dialog-error></p>
          <footer><button type="button" data-workspace-dialog-cancel>取消</button><button type="submit" data-workspace-dialog-confirm>确认</button></footer>
        </form>
      </section>`;
    document.body.append(dialog);
    const dialogForm = dialog.querySelector("[data-workspace-dialog-form]");
    const dialogField = dialog.querySelector("[data-workspace-dialog-field]");
    const dialogInput = dialog.querySelector("[data-workspace-dialog-input]");
    const dialogError = dialog.querySelector("[data-workspace-dialog-error]");
    const dialogConfirm = dialog.querySelector("[data-workspace-dialog-confirm]");
    let dialogAction = null;

    const closeWorkspaceDialog = () => {
      if (dialogConfirm.disabled) return;
      dialog.hidden = true;
      dialogAction = null;
      document.body.classList.remove("workspace-dialog-open");
    };
    const openWorkspaceDialog = ({ title, description, value = "", inputLabel = "", confirmLabel = "确认", danger = false, onConfirm }) => {
      dialog.querySelector("#workspace-operation-title").textContent = title;
      dialog.querySelector("#workspace-operation-description").textContent = description;
      dialogField.hidden = !inputLabel;
      dialog.querySelector("[data-workspace-dialog-label]").textContent = inputLabel || "项目名称";
      dialogInput.value = value;
      dialogError.textContent = "";
      dialogConfirm.textContent = confirmLabel;
      dialogConfirm.classList.toggle("danger", danger);
      dialogConfirm.disabled = false;
      dialogAction = onConfirm;
      dialog.hidden = false;
      document.body.classList.add("workspace-dialog-open");
      window.setTimeout(() => (inputLabel ? dialogInput : dialogConfirm).focus(), 0);
      if (inputLabel) window.setTimeout(() => dialogInput.select(), 0);
    };
    dialog.addEventListener("click", (event) => {
      if (event.target.closest("[data-workspace-dialog-cancel]")) closeWorkspaceDialog();
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeWorkspaceDialog();
    });
    dialogForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!dialogAction || dialogConfirm.disabled) return;
      const value = dialogField.hidden ? true : dialogInput.value.trim();
      if (!dialogField.hidden && !value) {
        dialogError.textContent = "请输入项目名称。";
        dialogInput.focus();
        return;
      }
      const originalLabel = dialogConfirm.textContent;
      dialogError.textContent = "";
      dialogConfirm.disabled = true;
      dialogConfirm.textContent = "处理中…";
      try {
        await dialogAction(value);
        dialogConfirm.disabled = false;
        dialogConfirm.textContent = originalLabel;
        closeWorkspaceDialog();
      } catch (error) {
        dialogError.textContent = error.message || "操作失败，请重试。";
        dialogConfirm.disabled = false;
        dialogConfirm.textContent = originalLabel;
      }
    });

    manager.addEventListener("click", (event) => {
      const selected = event.target.closest("[data-workspace-select]");
      if (selected) {
        if (selected.dataset.workspaceSelect !== workspaceContext.activeId) {
          workspaceContext.setActive(selected.dataset.workspaceSelect);
          location.reload();
        }
        return;
      }
      if (event.target.closest("[data-workspace-create]")) {
        const suggested = `重绘项目 ${workspaceContext.items.length + 1}`;
        openWorkspaceDialog({
          title: "新建重绘项目",
          description: "新项目拥有独立的原片、分镜、人物配置、生成进度和成片，不会覆盖当前项目。",
          inputLabel: "项目名称",
          value: suggested,
          confirmLabel: "创建并进入",
          onConfirm: async (name) => {
            const result = await workspaceContext.request(`${workspaceContext.apiBase}/workspaces`, {
              method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
            });
            workspaceContext.items = result.workspaces || [];
            workspaceContext.setActive(result.workspace.id);
            commonToast(`已创建“${name}”，正在进入。`);
            window.setTimeout(() => location.reload(), 120);
          },
        });
        return;
      }
      if (event.target.closest("[data-workspace-rename]")) {
        const active = workspaceContext.active();
        if (!active) return;
        openWorkspaceDialog({
          title: "重命名重绘项目",
          description: "只修改项目显示名称，不会修改或删除已生成内容。",
          inputLabel: "新名称",
          value: active.name,
          confirmLabel: "保存名称",
          onConfirm: async (name) => {
            if (name === active.name) return;
            const result = await workspaceContext.request(`${workspaceContext.apiBase}/workspaces/${active.id}`, {
              method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
            });
            workspaceContext.items = result.workspaces || [];
            workspaceContext.emit();
            commonToast(`项目已重命名为“${name}”。`);
          },
        });
        return;
      }
      if (event.target.closest("[data-workspace-delete]")) {
        const active = workspaceContext.active();
        if (!active) return;
        openWorkspaceDialog({
          title: `删除“${active.name}”？`,
          description: "项目将从重绘项目列表移除；原任务目录和已生成文件仍会保留。运行中的项目不能删除。",
          confirmLabel: "确认删除",
          danger: true,
          onConfirm: async () => {
            const result = await workspaceContext.request(`${workspaceContext.apiBase}/workspaces/${active.id}`, { method: "DELETE" });
            workspaceContext.items = result.workspaces || [];
            workspaceContext.activeId = "";
            if (workspaceContext.items[0]?.id) workspaceContext.setActive(workspaceContext.items[0].id);
            commonToast(`已从列表移除“${active.name}”。`);
            window.setTimeout(() => location.reload(), 120);
          },
        });
      }
    });
    window.setInterval(() => {
      if (document.visibilityState === "visible") workspaceContext.refresh().catch(() => {});
    }, 5000);
    return true;
  }

  function boot() {
    installShell();
    if (!installWorkspaceManager() && workspaceContext) {
      const observer = new MutationObserver(() => {
        if (installWorkspaceManager()) observer.disconnect();
      });
      observer.observe(document.body, { childList: true, subtree: true });
    }
    syncPaidActionMeta();
    syncDisabledReasons();
    new MutationObserver(() => {
      syncDisabledReasons();
      syncPaidActionMeta();
    }).observe(document.body, { attributes: true, childList: true, subtree: true, attributeFilter: ["disabled", "title", "data-blockers", "class"] });
    document.addEventListener("change", syncPaidActionMeta);
    document.addEventListener("input", syncPaidActionMeta);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
