(function () {
  async function loadAccount() {
    try {
      const response = await fetch("/api/account", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      if (!payload.enabled || !payload.account) return;
      const account = payload.account;
      const state = document.querySelector("#accountState");
      const localState = document.querySelector("#localState");
      document.querySelector("#accountName").textContent = account.username || "CZMIYOU 用户";
      document.querySelector("#accountMeta").textContent = `产品 4 · 余额 ¥${Number(account.balance || 0).toFixed(2)} · 剩余 ${Number(account.days_remaining || 0)} 天`;
      state.hidden = false;
      localState.hidden = true;
      state.dataset.loginUrl = payload.login_url || "";
    } catch (_) {
      // 账号摘要只是展示增强；真正的权限仍由服务端统一校验。
    }
  }

  async function logout() {
    const response = await fetch("/api/logout", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    window.location.href = payload.login_url || document.querySelector("#accountState")?.dataset.loginUrl || "/";
  }

  function buildCategories() {
    const registry = window.DepthFlowRegistry;
    const source = document.querySelector(".project-grid");
    if (!registry || !source || source.dataset.grouped === "true") return;
    const cards = new Map(
      [...source.querySelectorAll(".project-card")].map((card) => [card.dataset.projectId, card]),
    );
    const wrapper = document.createElement("div");
    wrapper.className = "project-categories";
    for (const category of registry.categories) {
      const section = document.createElement("section");
      section.className = `project-category project-category-${category.id}`;
      section.innerHTML = `<header class="project-category-heading"><span class="project-category-number">${category.number}</span><div><h3>${category.title}</h3><p>${category.subtitle}</p></div></header><div class="project-category-list"></div>`;
      const list = section.querySelector(".project-category-list");
      for (const projectId of category.projects) {
        const card = cards.get(projectId);
        const project = registry.projects[projectId];
        if (!card || !project) continue;
        const description = card.querySelector(".card-description");
        if (description) description.textContent = project.summary;
        const number = card.querySelector(".project-number");
        if (number) number.textContent = project.number;
        list.append(card);
      }
      wrapper.append(section);
    }
    source.replaceChildren(wrapper);
    source.dataset.grouped = "true";
  }

  function boot() {
    buildCategories();
    loadAccount();
    document.querySelector("#logoutButton")?.addEventListener("click", logout);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
