(() => {
  "use strict";

  const previewUrls = new WeakMap();
  let modal = null;
  let lastFocused = null;

  function mediaKind(file) {
    const type = String(file?.type || "").toLowerCase();
    const name = String(file?.name || "").toLowerCase();
    if (type.startsWith("video/") || /\.(mp4|mov|m4v|webm|avi|mkv)$/.test(name)) return "video";
    return "image";
  }

  function ensureModal() {
    if (modal) return modal;
    modal = document.createElement("div");
    modal.className = "upload-preview-modal hidden";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-label", "素材大图预览");
    modal.innerHTML = `
      <div class="upload-preview-dialog">
        <div class="upload-preview-modal-head">
          <div><b>素材预览</b><small data-preview-caption></small></div>
          <button type="button" data-preview-close aria-label="关闭预览">×</button>
        </div>
        <div class="upload-preview-stage"></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector("[data-preview-close]").addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
    return modal;
  }

  function closeModal() {
    if (!modal || modal.classList.contains("hidden")) return;
    const video = modal.querySelector("video");
    if (video) video.pause();
    modal.classList.add("hidden");
    modal.removeAttribute("data-preview-url");
    document.body.classList.remove("upload-preview-open");
    if (lastFocused?.isConnected) lastFocused.focus({ preventScroll: true });
  }

  function openPreview(url, kind, caption = "") {
    if (!url) return;
    const viewer = ensureModal();
    const stage = viewer.querySelector(".upload-preview-stage");
    const title = viewer.querySelector("[data-preview-caption]");
    stage.replaceChildren();
    title.textContent = caption;
    lastFocused = document.activeElement;
    viewer.dataset.previewUrl = url;

    if (kind === "video") {
      const video = document.createElement("video");
      video.src = url;
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = "auto";
      stage.appendChild(video);
    } else {
      const image = document.createElement("img");
      image.src = url;
      image.alt = caption || "上传图片预览";
      stage.appendChild(image);
    }

    viewer.classList.remove("hidden");
    document.body.classList.add("upload-preview-open");
    viewer.querySelector("[data-preview-close]").focus({ preventScroll: true });
  }

  function releaseInputUrls(input) {
    const urls = previewUrls.get(input) || [];
    if (modal?.dataset.previewUrl && urls.includes(modal.dataset.previewUrl)) closeModal();
    urls.forEach((url) => URL.revokeObjectURL(url));
    previewUrls.delete(input);
  }

  function makeFallback(item, label) {
    item.classList.add("preview-unavailable");
    const fallback = document.createElement("span");
    fallback.className = "upload-preview-fallback";
    fallback.textContent = label;
    item.prepend(fallback);
  }

  function buildItem(file, url) {
    const kind = mediaKind(file);
    const item = document.createElement("button");
    item.type = "button";
    item.className = `upload-preview-item ${kind}`;
    item.setAttribute("aria-label", `打开查看 ${file.name}`);
    item.title = "点击放大查看";

    let media;
    if (kind === "video") {
      media = document.createElement("video");
      media.src = url;
      media.muted = true;
      media.playsInline = true;
      media.preload = "auto";
      media.addEventListener("loadedmetadata", () => {
        const target = Number.isFinite(media.duration) ? Math.min(0.2, Math.max(0, media.duration / 3)) : 0;
        if (target > 0) {
          try { media.currentTime = target; } catch (_error) { /* keep the first frame */ }
        }
      }, { once: true });
      media.addEventListener("loadeddata", () => item.classList.add("media-ready"), { once: true });
      media.addEventListener("error", () => makeFallback(item, "视频"), { once: true });
      const play = document.createElement("i");
      play.className = "upload-preview-play";
      play.textContent = "▶";
      item.append(media, play);
    } else {
      media = document.createElement("img");
      media.src = url;
      media.alt = file.name;
      media.addEventListener("load", () => item.classList.add("media-ready"), { once: true });
      media.addEventListener("error", () => makeFallback(item, "图片"), { once: true });
      item.appendChild(media);
    }

    const hint = document.createElement("span");
    hint.className = "upload-preview-hint";
    hint.textContent = "点击查看";
    const name = document.createElement("small");
    name.className = "upload-preview-name";
    name.textContent = file.name;
    item.append(hint, name);
    item.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openPreview(url, kind, file.name);
    });
    return item;
  }

  function renderInputPreview(input) {
    if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
    const host = input.closest("label") || input.parentElement;
    if (!host) return;
    const old = Array.from(host.children).find((node) => node.classList?.contains("upload-file-preview"));
    if (old) old.remove();
    releaseInputUrls(input);

    const files = Array.from(input.files || []);
    host.classList.toggle("has-upload-preview", files.length > 0);
    if (!files.length) return;

    const gallery = document.createElement("div");
    gallery.className = `upload-file-preview${files.length > 1 ? " multiple" : ""}`;
    gallery.setAttribute("aria-label", "已选择素材预览");
    const urls = files.map((file) => URL.createObjectURL(file));
    previewUrls.set(input, urls);
    files.forEach((file, index) => gallery.appendChild(buildItem(file, urls[index])));
    host.appendChild(gallery);
  }

  function decorateSceneLibrary(root = document) {
    root.querySelectorAll?.(".scene-library-image img").forEach((image) => {
      if (image.dataset.clickPreviewReady) return;
      image.dataset.clickPreviewReady = "1";
      image.tabIndex = 0;
      image.setAttribute("role", "button");
      image.title = "点击放大查看";
      const show = (event) => {
        if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        openPreview(image.currentSrc || image.src, "image", image.alt || "场景参考图");
      };
      image.addEventListener("click", show);
      image.addEventListener("keydown", show);
    });
  }

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (input instanceof HTMLInputElement && input.type === "file") renderInputPreview(input);
  }, true);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });

  function initialize() {
    document.querySelectorAll('input[type="file"]').forEach(renderInputPreview);
    decorateSceneLibrary();
    const observer = new MutationObserver((records) => {
      records.forEach((record) => record.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        decorateSceneLibrary(node);
      }));
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  window.DepthFlowUploadPreview = { open: openPreview, close: closeModal, refresh: renderInputPreview };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})();
