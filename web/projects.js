(function () {
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

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", buildCategories);
  else buildCategories();
})();
