const state = {
  config: null,
  jobId: null,
  activeJobId: null,
  shotCount: 0,
  actorCount: 1,
  pollTimer: null,
  lastJob: null,
  shotCasts: new Map(),
  manualCasts: new Set(),
  sceneGroups: [],
  shotScenes: new Map(),
  manualSceneShots: new Set(),
  nextSceneGroupId: 1,
  preserveSceneLibraryOnNextJob: false,
  castConfirmed: false,
  performanceDrafts: new Map(),
  positionDrafts: new Map(),
  busy: false,
};

const $ = (selector) => document.querySelector(selector);
const defaultRoles = ["画面主要人物", "画面第二人物", "画面第三人物", "画面第四人物"];
let shotPreviewLoadToken = 0;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

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

function badge(selector, ready, yes, no) {
  const node = $(selector);
  node.className = `badge ${ready ? "ready" : "missing"}`;
  node.innerHTML = `<i></i>${ready ? yes : no}`;
}

function bind(inputSelector, labelSelector) {
  $(inputSelector).addEventListener("change", () => {
    const file = $(inputSelector).files[0];
    if (file) $(labelSelector).textContent = file.name;
  });
}

function existingActor(index) {
  return (state.lastJob?.actors || []).find((actor) => Number(actor.id) === index) || null;
}

function actorTemplate(index, existing = null) {
  const role = existing?.role || defaultRoles[index - 1];
  const personNote = existing?.has_person ? "已保存人物素材，可直接复用" : "虚构 / AI角色图片";
  const clothingNote = existing?.has_clothing ? "已保存服装素材，可直接复用" : "建议白底服装参考";
  return `
    <article class="long-actor-card" data-actor-index="${index}">
      <div class="long-actor-head"><div><span class="long-actor-index">${String(index).padStart(2, "0")}</span><b>人物 ${index}</b><small>跨分镜固定身份</small></div><small>每个分镜按需选择</small></div>
      <div class="long-actor-body">
        <div class="field role-field"><label for="roleDescription${index}">原片角色定位</label><input id="roleDescription${index}" type="text" maxlength="200" value="${escapeHtml(role)}"><small>例如：画面左侧短发人物</small></div>
        <div class="long-actor-asset"><b>人物形象</b><label class="mini-drop" for="personImage${index}"><input id="personImage${index}" type="file" accept="image/*,.heic,.heif"><span>选择人物图片</span><small id="personFileName${index}" class="${existing?.has_person ? "saved-reference" : ""}">${personNote}</small></label><input id="personAsset${index}" type="text" placeholder="或填写 asset://asset-..."></div>
        <div class="long-actor-asset"><b>对应服装</b><label class="mini-drop" for="clothingImage${index}"><input id="clothingImage${index}" type="file" accept="image/*,.heic,.heif"><span>选择服装图片</span><small id="clothingFileName${index}" class="${existing?.has_clothing ? "saved-reference" : ""}">${clothingNote}</small></label></div>
      </div>
    </article>`;
}

function bindActor(index) {
  $(`#roleDescription${index}`).addEventListener("input", () => {
    document.querySelectorAll(`[data-person-mapping] option[value="${index}"]`).forEach((node) => {
      node.textContent = `人物${index} · ${$(`#roleDescription${index}`).value.trim() || `人物${index}`}`;
    });
    invalidateCastConfirmation();
  });
  bind(`#personImage${index}`, `#personFileName${index}`);
  bind(`#clothingImage${index}`, `#clothingFileName${index}`);
}

function renderActorList(existingActors = []) {
  $("#actorList").innerHTML = "";
  for (let index = 1; index <= state.actorCount; index += 1) {
    const existing = existingActors.find((actor) => Number(actor.id) === index) || null;
    $("#actorList").insertAdjacentHTML("beforeend", actorTemplate(index, existing));
    bindActor(index);
  }
  updateActorControls();
}

function updateActorControls() {
  $("#actorCountLabel").textContent = `${state.actorCount} 位角色`;
  $("#addActorBtn").disabled = state.actorCount >= 4 || state.lastJob?.status === "running";
  $("#removeActorBtn").disabled = state.actorCount <= 1 || state.lastJob?.status === "running";
}

function addActor() {
  if (state.actorCount >= 4) return toast("角色库最多支持4位人物。", true);
  state.actorCount += 1;
  $("#actorList").insertAdjacentHTML("beforeend", actorTemplate(state.actorCount, existingActor(state.actorCount)));
  bindActor(state.actorCount);
  updateActorControls();
  applyAutomaticCastSuggestions();
  renderShotGrid();
  invalidateCastConfirmation();
}

function removeActor() {
  if (state.actorCount <= 1) return;
  const removed = state.actorCount;
  $(`[data-actor-index="${removed}"]`)?.remove();
  state.actorCount -= 1;
  for (const [shotIndex, actorIds] of state.shotCasts) {
    state.shotCasts.set(shotIndex, actorIds.filter((value) => value !== removed));
  }
  updateActorControls();
  renderShotGrid();
  invalidateCastConfirmation();
}

function stableDetectorPeopleCount(shot) {
  const counts = (shot?.sample_actor_counts || []).map(Number).filter((value) => Number.isFinite(value) && value >= 0 && value <= 4);
  if (counts.length) {
    const repeated = [...new Set(counts.filter((value) => counts.filter((item) => item === value).length >= 2))].sort((a, b) => a - b);
    if (repeated.length) return repeated[repeated.length - 1];
    return [...counts].sort((a, b) => a - b)[Math.floor(counts.length / 2)];
  }
  return Math.max(Number(shot?.suggested_actor_count || 0), Array.isArray(shot?.person_slots) ? shot.person_slots.length : 0);
}

function detectedPeopleCount(shot) {
  const performanceSlots = [
    ...(shot?.performance?.performance || []).map((item) => Number(item.actor_slot || 0)),
    ...(shot?.performance?.dialogue || []).map((item) => Number(item.speaker_slot || 0)),
  ].filter((value) => Number.isFinite(value) && value > 0);
  const performanceCount = performanceSlots.length ? Math.max(...performanceSlots) : 0;
  return Math.max(0, performanceCount, stableDetectorPeopleCount(shot));
}

function performanceSlotAnchor(shot, slot) {
  if (shot?.position_binding_manual) {
    const mapping = (shot.actor_mappings || []).find((item) => Number(item.slot) === Number(slot));
    if (mapping?.source_position) return String(mapping.source_position);
  }
  const semanticSlots = [...new Set((shot?.performance?.performance || []).map((item) => Number(item.actor_slot || 0)).filter((value) => value > 0))];
  const detectorCount = stableDetectorPeopleCount(shot);
  const performanceIsComplete = semanticSlots.length >= detectorCount;
  const performances = performanceIsComplete
    ? (shot?.performance?.performance || []).filter((item) => Number(item.actor_slot) === Number(slot))
    : [];
  const evidence = performances.map((item) => String(item.visible_evidence || "").trim()).filter(Boolean).slice(0, 2).join("；");
  const intent = performances.map((item) => String(item.core_intent || "").trim()).filter(Boolean).slice(0, 2).join("；");
  if (evidence || intent) return evidence || intent;
  const detected = (shot?.person_slots || [])[Number(slot) - 1] || {};
  return detected.position || `表演槽位 P${slot}`;
}

function positionLockValue(shot, slot) {
  const key = `${Number(shot.index)}:${Number(slot)}`;
  return state.positionDrafts.has(key) ? state.positionDrafts.get(key) : performanceSlotAnchor(shot, slot);
}

function continuityCharacterFor(shot, slot) {
  const assignments = state.lastJob?.cast_continuity?.assignments || [];
  const match = assignments.find((item) => Number(item.shot_index) === Number(shot.index) && Number(item.slot) === Number(slot));
  return match ? Number(match.character_id || 0) : 0;
}

function continuityActorMap() {
  const map = new Map();
  for (const shot of state.lastJob?.shots || []) {
    castFor(shot.index).forEach((actorId, offset) => {
      const characterId = continuityCharacterFor(shot, offset + 1);
      if (characterId && !map.has(characterId)) map.set(characterId, Number(actorId));
    });
  }
  return map;
}

function nextSceneGroupId() {
  let candidate;
  do {
    candidate = `scene_${state.nextSceneGroupId}`;
    state.nextSceneGroupId += 1;
  } while (state.sceneGroups.some((group) => group.id === candidate));
  return candidate;
}

function normalizeSceneGroup(group, position) {
  const id = group?.id || nextSceneGroupId();
  const images = (group?.images || []).map((image, offset) => ({
    index: Number(image.index || offset + 1),
    name: image.name || `场景图 ${offset + 1}`,
    url: image.url || "",
    file: image.file || null,
  }));
  return { id, name: group?.name || `新场景 ${position}`, description: group?.description || "", images };
}

function sceneGroupById(groupId) {
  return state.sceneGroups.find((group) => group.id === groupId) || null;
}

function sceneAssignmentFor(index) {
  return state.shotScenes.get(Number(index)) || null;
}

function sceneGroupTemplate(group, position) {
  const imageItems = group.images.map((image) => `
    <div class="scene-library-image">
      ${image.url || image.file ? `<img src="${image.file ? URL.createObjectURL(image.file) : image.url}" alt="${escapeHtml(image.name)}">` : ""}
      <span><b>A${position}-${image.index}</b><small>${escapeHtml(image.name)}</small></span>
    </div>`).join("");
  return `<article class="scene-group-card" data-scene-group="${group.id}">
    <div class="scene-group-head"><span>${String(position).padStart(2, "0")}</span><div><input data-scene-name="${group.id}" maxlength="60" value="${escapeHtml(group.name)}" aria-label="场景组名称"><small>同一地点可上传1–6个角度</small></div><button type="button" data-remove-scene-group="${group.id}" ${state.sceneGroups.length <= 1 ? "disabled" : ""}>删除</button></div>
    <textarea data-scene-description="${group.id}" rows="2" maxlength="240" placeholder="可选：例如现代客厅、暖色木质、夜景">${escapeHtml(group.description)}</textarea>
    <div class="scene-library-images">${imageItems || `<div class="scene-library-empty">尚未上传场景图片</div>`}</div>
    <label class="mini-drop scene-multi-drop" for="sceneFiles${group.id}"><input id="sceneFiles${group.id}" data-scene-files="${group.id}" type="file" accept="image/*" multiple><span>选择该场景的多角度图片</span><small>${group.images.length ? `已准备 ${group.images.length} 张，可再次选择进行替换` : "最多6张；系统会为每个分镜选择具体角度"}</small></label>
  </article>`;
}

function renderSceneLibrary() {
  const container = $("#sceneGroupList");
  if (!container) return;
  container.innerHTML = state.sceneGroups.map((group, offset) => sceneGroupTemplate(group, offset + 1)).join("");
  $("#sceneGroupCount").textContent = `${state.sceneGroups.length} 个场景组 · ${state.sceneGroups.reduce((sum, group) => sum + group.images.length, 0)} 张图`;
}

function initializeSceneLibrary(job, preserveLocal = false) {
  const savedGroups = job?.scene_groups || [];
  if (!preserveLocal || savedGroups.length) {
    state.sceneGroups = savedGroups.map((group, offset) => normalizeSceneGroup(group, offset + 1));
  }
  state.nextSceneGroupId = 1;
  for (const group of state.sceneGroups) {
    const match = /^scene_(\d+)$/.exec(group.id);
    if (match) state.nextSceneGroupId = Math.max(state.nextSceneGroupId, Number(match[1]) + 1);
  }
  if (!state.sceneGroups.length) state.sceneGroups.push(normalizeSceneGroup(null, 1));
  state.shotScenes.clear(); state.manualSceneShots.clear();
  for (const shot of job?.shots || []) {
    if (shot.scene_group_id && Number(shot.scene_image_index) > 0 && sceneGroupById(shot.scene_group_id)) {
      state.shotScenes.set(Number(shot.index), { groupId: shot.scene_group_id, imageIndex: Number(shot.scene_image_index) });
    }
  }
  renderSceneLibrary();
}

function autoMatchScenes(showToast = true) {
  const groups = state.sceneGroups.filter((group) => group.images.length);
  const shots = state.lastJob?.shots || [];
  if (!shots.length) return showToast && toast("请先完成分镜分析。", true);
  if (!groups.length) return showToast && toast("请先在场景库上传至少一组新场景图。", true);
  const usage = new Map(groups.map((group) => [group.id, 0]));
  const clusters = [...new Set(shots.map((shot) => Number(shot.scene_cluster || shot.index)))];
  const groupForCluster = new Map(clusters.map((cluster, offset) => {
    const groupIndex = groups.length === 1 ? 0 : Math.min(groups.length - 1, Math.floor(offset * groups.length / clusters.length));
    return [cluster, groups[groupIndex]];
  }));
  shots.forEach((shot) => {
    const cluster = Number(shot.scene_cluster || shot.index);
    const group = groupForCluster.get(cluster) || groups[0];
    const used = usage.get(group.id) || 0;
    state.shotScenes.set(Number(shot.index), { groupId: group.id, imageIndex: used % group.images.length + 1 });
    usage.set(group.id, used + 1);
  });
  state.manualSceneShots.clear();
  renderShotGrid(); invalidateCastConfirmation();
  if (showToast) toast(`已按 ${clusters.length} 个原片场景组匹配 ${shots.length} 个镜头，请核对每镜场景和角度。`);
}

function sceneMappingsComplete() {
  return (state.lastJob?.shots || []).every((shot) => {
    const assignment = sceneAssignmentFor(shot.index);
    const group = assignment ? sceneGroupById(assignment.groupId) : null;
    return Boolean(group && group.images.some((image) => image.index === Number(assignment.imageIndex)));
  });
}

function applyAutomaticCastSuggestions() {
  const continuityMap = new Map();
  for (const shot of state.lastJob?.shots || []) {
    const savedMappings = Array.isArray(shot.actor_mappings)
      ? [...shot.actor_mappings].sort((a, b) => Number(a.slot) - Number(b.slot))
      : [];
    for (const mapping of savedMappings) {
      const characterId = continuityCharacterFor(shot, Number(mapping.slot));
      if (characterId && !continuityMap.has(characterId)) continuityMap.set(characterId, Number(mapping.actor_id));
    }
  }
  const usedActors = new Set([...continuityMap.values()].filter((actorId) => actorId > 0 && actorId <= state.actorCount));
  const continuityAssignments = [...(state.lastJob?.cast_continuity?.assignments || [])]
    .sort((a, b) => Number(a.shot_index) - Number(b.shot_index) || Number(a.slot) - Number(b.slot));
  for (const assignment of continuityAssignments) {
    const characterId = Number(assignment.character_id || 0);
    if (!characterId || continuityMap.has(characterId)) continue;
    const nextActorId = Array.from({ length: state.actorCount }, (_value, offset) => offset + 1)
      .find((actorId) => !usedActors.has(actorId));
    if (!nextActorId) continue;
    continuityMap.set(characterId, nextActorId);
    usedActors.add(nextActorId);
  }
  for (const shot of state.lastJob?.shots || []) {
    const index = Number(shot.index);
    if (state.manualCasts.has(index)) continue;
    const continuitySuggested = Array.from({ length: detectedPeopleCount(shot) }, (_value, offset) => {
      const characterId = continuityCharacterFor(shot, offset + 1);
      return continuityMap.get(characterId) || 0;
    });
    if (continuitySuggested.length && continuitySuggested.every((value) => value > 0 && value <= state.actorCount)) {
      state.shotCasts.set(index, continuitySuggested);
      continue;
    }
    if (shot.cast_confirmed === true) {
      const savedMappings = Array.isArray(shot.actor_mappings)
        ? [...shot.actor_mappings].sort((a, b) => Number(a.slot) - Number(b.slot)).map((value) => Number(value.actor_id))
        : (shot.actor_ids || []).map(Number);
      const validSavedMappings = savedMappings.filter((value) => value <= state.actorCount);
      if (validSavedMappings.length >= detectedPeopleCount(shot)) {
        state.shotCasts.set(index, validSavedMappings);
        continue;
      }
    }
    const detected = detectedPeopleCount(shot);
    const suggested = Math.min(state.actorCount, Math.max(0, detected));
    state.shotCasts.set(index, Array.from({ length: suggested }, (_value, offset) => offset + 1));
  }
}

function castFor(index) { return state.shotCasts.get(Number(index)) || []; }

function actorRole(index) {
  return $(`#roleDescription${index}`)?.value.trim() || existingActor(index)?.role || `人物${index}`;
}

function routeText(actorIds) {
  if (!actorIds.length) return "无人镜头：使用深度视频和新场景板进行场景重绘";
  if (actorIds.length === 1) return `单人路由：使用人物 ${actorIds[0]}，调用项目1生成流程`;
  return `多人路由：使用人物 ${actorIds.join("、")}，调用项目2多人生成流程`;
}

function confidenceLabel(value) {
  return value === "high" ? "较高" : value === "medium" ? "中等" : "较低，请重点核对";
}

function extractedDialogueText(performance) {
  return (performance?.dialogue || []).map((item) => {
    const delivery = item.delivery ? `；语气：${item.delivery}` : "";
    const mouth = item.mouth_motion ? `；口型：${item.mouth_motion}` : "";
    const source = item.source_type || "character";
    const sourceLabel = source === "character" ? `P${Number(item.speaker_slot || 1)}` : source === "offscreen" ? "画外声" : source === "bgm_vocal" ? "BGM人声（不生成）" : source === "ignore" ? "忽略声音" : "来源待确认";
    return `[${Number(item.start || 0).toFixed(2)}–${Number(item.end || 0).toFixed(2)}秒] ${sourceLabel}：${item.text || ""}${delivery}${mouth}`;
  }).join("\n");
}

function extractedPerformanceText(performance) {
  return (performance?.performance || []).map((item) => {
    const evidence = item.visible_evidence ? `；可见证据：${item.visible_evidence}` : "";
    const exclude = item.exclude ? `；避免误读：${item.exclude}` : "";
    return `[${Number(item.start || 0).toFixed(2)}–${Number(item.end || 0).toFixed(2)}秒] P${Number(item.actor_slot || 1)}：${item.core_intent || ""}${evidence}${exclude}`;
  }).join("\n");
}

function performanceEditorValue(shot, field) {
  const index = Number(shot.index);
  const draft = state.performanceDrafts.get(index);
  if (draft && Object.prototype.hasOwnProperty.call(draft, field)) return draft[field];
  const performance = shot.performance || {};
  const manualField = field === "dialogue" ? "manual_dialogue_text" : "manual_performance_text";
  if (Object.prototype.hasOwnProperty.call(performance, manualField)) return String(performance[manualField] || "");
  return field === "dialogue" ? extractedDialogueText(performance) : extractedPerformanceText(performance);
}

function dialogueSourceEditor(shot, disabled) {
  const dialogue = shot?.performance?.dialogue || [];
  if (!dialogue.length) return `<div class="dialogue-source-empty">本镜未提取到语言声；如有遗漏，可直接在下方台词框补写。</div>`;
  const people = Math.max(1, Math.min(4, detectedPeopleCount(shot) || 1));
  return `<div class="dialogue-source-editor"><div class="dialogue-source-title"><b>逐句声音来源与说话人</b><small>嘴部不可见、人物背对或被遮挡，不代表不是说话人；请按剧情和人物位置锁定。</small></div>${dialogue.map((item, itemIndex) => {
    const sourceType = item.source_type || "character";
    const current = sourceType === "character" ? `p${Number(item.speaker_slot || 1)}` : sourceType;
    const characterOptions = Array.from({ length: people }, (_value, offset) => offset + 1).map((slot) =>
      `<option value="p${slot}" ${current === `p${slot}` ? "selected" : ""}>画内人物 P${slot}</option>`
    ).join("");
    const options = `${characterOptions}<option value="offscreen" ${current === "offscreen" ? "selected" : ""}>画外人物台词</option><option value="bgm_vocal" ${current === "bgm_vocal" ? "selected" : ""}>BGM 歌声/人声（不生成）</option><option value="ignore" ${current === "ignore" ? "selected" : ""}>其他声音（忽略）</option><option value="uncertain" ${current === "uncertain" ? "selected" : ""}>来源待确认（禁止生成）</option>`;
    return `<div class="dialogue-source-row"><span><b>${Number(item.start || 0).toFixed(2)}–${Number(item.end || 0).toFixed(2)}秒</b><small>${escapeHtml(item.text || "（未识别文字）")}</small></span><select data-dialogue-source-index="${itemIndex}" ${disabled}>${options}</select></div>`;
  }).join("")}</div>`;
}

function performanceEditor(shot, disabled) {
  if (!shot.has_performance) return "";
  const index = Number(shot.index);
  const performance = shot.performance || {};
  const manuallyReviewed = performance.manual_reviewed === true;
  const needsRegeneration = shot.performance_dirty === true;
  return `<section class="performance-editor ${needsRegeneration ? "dirty" : ""}">
    <div class="performance-editor-head"><div><b>台词与表演人工校订</b><small>人工文字保存后优先级最高，会覆盖本镜 AI 提取结果</small></div><span>${needsRegeneration ? "已修改 · 待重生" : manuallyReviewed ? "已人工保存" : "待人工核对"}</span></div>
    ${dialogueSourceEditor(shot, disabled)}
    <label><span>台词、说话人、语气与口型时序</span><textarea data-performance-dialogue="${index}" rows="4" maxlength="1800" placeholder="无台词时留空；有台词请保留时间段和 P1/P2 说话人" ${disabled}>${escapeHtml(performanceEditorValue(shot, "dialogue"))}</textarea></label>
    <label><span>表演、情绪、眼神、表情与姿态</span><textarea data-performance-acting="${index}" rows="4" maxlength="1800" placeholder="按时间段描述 P1/P2 的完整表演语义与可见证据" ${disabled}>${escapeHtml(performanceEditorValue(shot, "performance"))}</textarea></label>
    <div class="performance-editor-actions"><button type="button" data-reset-performance="${index}" ${disabled}>恢复 AI 提取文字</button><button class="save-performance" type="button" data-save-performance="${index}" ${disabled}>保存本镜校订</button></div>
  </section>`;
}

function renderFinalStyleLibrary(selectedId = "match_character") {
  const presets = state.config?.final_style_presets || [];
  const selected = presets.some((preset) => preset.id === selectedId) ? selectedId : "match_character";
  $("#finalStyle").value = selected;
  $("#finalStyleLibrary").innerHTML = presets.map((preset) => `
    <button type="button" class="style-preset-card ${preset.id === selected ? "active" : ""}" data-final-style="${escapeHtml(preset.id)}">
      <b>${escapeHtml(preset.label)}</b><small>${escapeHtml(preset.description)}</small>
    </button>`).join("");
}

function shotCard(shot, job) {
  const index = Number(shot.index);
  const actorIds = castFor(index);
  const media = shot.has_output ? shot.output_url : shot.has_white_model ? shot.white_model_url : shot.has_mosaic ? shot.mosaic_url : shot.has_depth ? shot.depth_url : shot.source_url;
  const mediaLabel = shot.has_output ? "最终成片" : shot.has_white_model ? "白模绿幕" : shot.has_mosaic ? "人脸打码" : shot.has_depth ? (shot.depth_padded ? "深度视频（片尾已定格延长）" : "深度视频") : "原始分镜";
  const mediaRevision = shot.task_id || shot.white_model_task_id || shot.white_model_signature || shot.output_signature || job.created_at;
  const mediaSrc = media ? `${media}?preview=${encodeURIComponent(`${job.id}-${index}-${mediaRevision}`)}` : "";
  const disabled = job.status === "running" && job.kind === "long_generate" ? "disabled" : "";
  const personSlots = Array.isArray(shot.person_slots) ? shot.person_slots : [];
  const mappingRows = actorIds.map((actorId, offset) => {
    const detected = personSlots[offset] || {};
    const position = performanceSlotAnchor(shot, offset + 1);
    const characterId = continuityCharacterFor(shot, offset + 1);
    const modelColor = ({red:"红",white:"白",blue:"蓝",yellow:"黄"})[shot.white_color_plan?.find(r => r.id === `p${offset+1}`)?.color] || "";
    const hasSemanticSlot = (shot?.performance?.performance || []).some((item) => Number(item.actor_slot) === offset + 1);
    const confidence = hasSemanticSlot
      ? " · 来自台词表演分析"
      : detected.confidence ? ` · 检测置信度${Math.round(Number(detected.confidence) * 100)}%` : " · 人工添加";
    const options = Array.from({ length: state.actorCount }, (_value, actorOffset) => actorOffset + 1).map((optionId) =>
      `<option value="${optionId}" ${optionId === actorId ? "selected" : ""}>人物${optionId} · ${escapeHtml(actorRole(optionId))}</option>`
    ).join("");
    return `<div class="person-map-row"><span><b>表演槽位 P${offset + 1}${modelColor ? ` · ${modelColor}模` : ""}${characterId ? ` · 原片身份 C${characterId}` : ""}</b><small>${escapeHtml(position)}${confidence}</small></span><i>替换为</i><select data-person-mapping="${index}" data-person-slot="${offset + 1}" ${disabled}>${options}</select><label class="position-lock-field"><span>位置与动作锁定</span><input data-position-lock="${index}" data-position-slot="${offset + 1}" maxlength="220" value="${escapeHtml(positionLockValue(shot, offset + 1))}" placeholder="例如：左侧内景坐着，较小；右侧前景站着，较大" ${disabled}></label></div>`;
  }).join("");
  const assignment = sceneAssignmentFor(index);
  const selectedGroup = assignment ? sceneGroupById(assignment.groupId) : null;
  const selectedImage = selectedGroup?.images.find((image) => image.index === Number(assignment?.imageIndex)) || null;
  const groupOptions = [`<option value="">选择新场景组</option>`, ...state.sceneGroups.map((group) =>
    `<option value="${group.id}" ${group.id === assignment?.groupId ? "selected" : ""} ${group.images.length ? "" : "disabled"}>${escapeHtml(group.name)} · ${group.images.length}张</option>`
  )].join("");
  const imageOptions = selectedGroup ? selectedGroup.images.map((image) =>
    `<option value="${image.index}" ${image.index === Number(assignment?.imageIndex) ? "selected" : ""}>角度 ${image.index} · ${escapeHtml(image.name)}</option>`
  ).join("") : "";
  const scenePreview = selectedImage ? (selectedImage.file ? URL.createObjectURL(selectedImage.file) : selectedImage.url) : "";
  const sceneStatus = selectedImage ? `${selectedGroup.name} · 角度${selectedImage.index}` : "尚未匹配新场景";
  const actions = [[shot.source_url, "原始", "video"], [shot.people_map_url, "人物标注", "image"], [shot.mosaic_url, "打码", "video"], [shot.white_model_url, "白模", "video"], [shot.performance_url, "表演JSON", "json"], [shot.depth_url, "深度", "video"], [shot.target_scene_url, "替换场景", "image"], [shot.scene_url, "生成场景参考", "image"], [shot.output_url, "成片", "video"]]
    .filter(([url]) => url)
    .map(([url, label, type]) => `<a class="shot-download" href="${url}?download=1" data-url="${url}" data-name="分镜${String(index).padStart(2, "0")}_${label}.${type === "image" ? "jpg" : type === "json" ? "json" : "mp4"}" data-type="${type}">下载${label}</a>`).join("");
  const performance = shot.performance || {};
  const performanceSummary = shot.has_performance ? `<div class="performance-summary">已提取 ${Number(performance.dialogue?.length || 0)} 句台词 · ${Number(performance.performance?.length || 0)} 段表演语义${performance.audio_summary ? ` · ${escapeHtml(performance.audio_summary)}` : ""}</div>` : "";
  const performanceForm = performanceEditor(shot, disabled);
  const compositionQa = shot.composition_qa || {};
  const compositionQaNotice = compositionQa.passed === false
    ? `<div class="scene-plate-note">构图验收未通过：${escapeHtml((compositionQa.reasons || []).join("；"))}。${shot.composition_approval_required ? `已完成 ${Number(shot.composition_retry_count || 0)} 次纠偏，系统已停止继续付费生成，需人工同意后才能重试。` : "系统正在按限制自动纠偏。"}</div>`
    : compositionQa.passed === true ? `<div class="scene-plate-note">自动构图/景别/运镜检查未发现明显差异。</div>` : "";
  const comparisonItems = [[shot.source_url, "原片"], [shot.white_model_url, "白膜"], [shot.output_url, "成片"]]
    .filter(([url]) => url)
    .map(([url, label]) => ({ url, label }));
  if (media && !comparisonItems.some((item) => item.url === media)) comparisonItems.push({ url: media, label: mediaLabel });
  const comparisonTabs = comparisonItems.length > 1 ? `<div class="shot-compare-tabs" aria-label="原片、白膜与成片切换">${comparisonItems.map((item) => {
    const source = `${item.url}?preview=${encodeURIComponent(`${job.id}-${index}-${item.label}-${mediaRevision}`)}`;
    return `<button type="button" class="${item.url === media ? "active" : ""}" data-shot-compare-src="${escapeHtml(source)}" data-shot-compare-label="${escapeHtml(item.label)}">${escapeHtml(item.label)}</button>`;
  }).join("")}</div>` : "";
  const preview = media ? `<div class="shot-comparison">${comparisonTabs}<div class="shot-preview-shell"><video class="shot-preview" controls playsinline preload="none" data-media-src="${escapeHtml(mediaSrc)}"></video><button type="button" class="shot-preview-load" data-load-shot-preview="${index}">加载${mediaLabel}预览</button></div><small class="shot-preview-caption">当前预览：${escapeHtml(mediaLabel)}</small></div>` : "";
  const semanticCount = detectedPeopleCount(shot);
  const detectorCount = stableDetectorPeopleCount(shot);
  const performanceCount = Math.max(0, ...(performance.performance || []).map((item) => Number(item.actor_slot || 0)));
  const countWarning = shot.has_performance && performanceCount < detectorCount ? ` · 表演分析漏记${detectorCount - performanceCount}人，已按本地检测补齐` : "";
  return `<article class="shot-card" data-shot-index="${index}"><div class="shot-head"><div><b>分镜 ${String(index).padStart(2, "0")}</b><small>${Number(shot.start).toFixed(2)}s–${Number(shot.end).toFixed(2)}s · ${Number(shot.duration).toFixed(2)}秒 · 原场景组 S${Number(shot.scene_cluster || index)} · ${mediaLabel}</small></div><span class="shot-status ${shot.status || "ready"}">${escapeHtml(shot.stage || shot.status || "ready")}</span></div>${preview}${shot.has_people_map ? `<div class="people-map-preview"><img src="${shot.people_map_url}?preview=${job.id}-${index}-people" alt="分镜人物检测标注"><small>彩框只是本地检测参考；多人重叠时可能重复。下方最终映射按“表演槽位”及动作位置锁定。</small></div>` : ""}${performanceSummary}${performanceForm}<div class="shot-cast"><div class="shot-cast-head"><b>原片表演槽位 → 新人物形象</b><span class="detection-badge ${shot.detection_confidence || "low"}">表演槽位 ${semanticCount} 人${escapeHtml(countWarning)}</span></div><div class="person-map-list">${mappingRows || `<div class="empty-person-map">当前标记为无人镜头；识别遗漏时点击“增加原片人物”。</div>`}</div><div class="person-map-controls"><button type="button" data-add-person="${index}" ${disabled}>＋ 增加原片人物</button><button type="button" data-remove-person="${index}" ${!actorIds.length || disabled ? "disabled" : ""}>减少一人</button><button class="clear-cast ${actorIds.length ? "" : "active"}" type="button" data-clear-cast="${index}" ${disabled}>无人镜头</button></div><small class="shot-route">${routeText(actorIds)}；生成时强制锁定左右、前后景、坐站和台词归属，禁止互换。</small></div><div class="shot-scene-map ${selectedImage ? "matched" : "missing"}"><div class="shot-scene-map-head"><div><b>本镜头新场景</b><small>${escapeHtml(sceneStatus)}</small></div><span>${selectedImage ? "已匹配" : "必须选择"}</span></div><div class="shot-scene-selects"><select data-shot-scene-group="${index}" ${disabled}>${groupOptions}</select><select data-shot-scene-image="${index}" ${!selectedGroup || disabled ? "disabled" : ""}>${imageOptions || `<option value="">先选择场景组</option>`}</select></div>${scenePreview ? `<img class="shot-scene" src="${scenePreview}" alt="本镜匹配的新场景">` : shot.has_target_scene ? `<img class="shot-scene" src="${shot.target_scene_url}?preview=${job.id}-${index}-target-scene" alt="已保存的新场景">` : ""}${shot.has_scene ? `<div class="scene-plate-note">已生成匹配机位的新场景板，可在下方下载预览。</div>` : ""}</div>${compositionQaNotice}<div class="shot-progress"><span style="width:${Math.max(0, Math.min(100, shot.progress || 0))}%"></span></div><div class="shot-artifact-row">${actions}</div>${shot.error ? `<div class="shot-error">${escapeHtml(shot.error)}</div>` : ""}</article>`;
}

function activateShotPreview(videoNode, onSettled = () => {}, forceReload = false) {
  if (!videoNode) { onSettled(); return; }
  const source = videoNode.dataset.mediaSrc;
  const shell = videoNode.closest(".shot-preview-shell");
  const button = shell?.querySelector(".shot-preview-load");
  if (!source) { onSettled(); return; }
  if (!forceReload && videoNode.dataset.loadedSource === source && videoNode.readyState >= 1) {
    if (button) button.hidden = true;
    onSettled(); return;
  }
  if (button) { button.hidden = false; button.disabled = true; button.textContent = "正在加载预览…"; }
  let finished = false;
  const finish = (succeeded) => {
    if (finished) return;
    finished = true;
    clearTimeout(videoNode._previewTimer);
    if (button) {
      button.disabled = false;
      button.hidden = succeeded;
      button.textContent = succeeded ? "已加载" : "重新加载预览";
    }
    if (!succeeded) videoNode.dataset.loadedSource = "";
    onSettled();
  };
  videoNode.addEventListener("loadedmetadata", () => finish(true), { once: true });
  videoNode.addEventListener("error", () => finish(false), { once: true });
  videoNode.dataset.loadedSource = source;
  videoNode.src = forceReload ? `${source}&retry=${Date.now()}` : source;
  videoNode.load();
  videoNode._previewTimer = setTimeout(() => finish(videoNode.readyState >= 1), 5000);
}

function hydrateShotPreviews() {
  const token = ++shotPreviewLoadToken;
  const previews = [...document.querySelectorAll("#shotGrid .shot-preview")];
  let index = 0;
  const loadNext = () => {
    if (token !== shotPreviewLoadToken || index >= previews.length) return;
    activateShotPreview(previews[index++], loadNext);
  };
  loadNext();
}

function renderShotGrid() {
  if (!state.lastJob) return;
  $("#shotGrid").innerHTML = (state.lastJob.shots || []).map((shot) => shotCard(shot, state.lastJob)).join("");
  const running = ["queued", "running"].includes(state.lastJob.status);
  for (const shot of state.lastJob.shots || []) {
    const card = $(`[data-shot-index="${Number(shot.index)}"]`);
    if (!card) continue;
    const status = card.querySelector(".shot-status");
    if (status) {
      status.insertAdjacentHTML(
        "afterend",
        `<button type="button" class="shot-delete-button" data-delete-shot="${Number(shot.index)}" ${running ? "disabled" : ""}>删除此镜</button>`,
      );
    }
    if (shot.has_output && !shot.composition_approval_required) {
      const actionRow = card.querySelector(".shot-artifact-row");
      if (actionRow) {
        actionRow.insertAdjacentHTML(
          "beforeend",
          `<button type="button" class="shot-regenerate-button" data-regenerate-shot="${Number(shot.index)}" ${running ? "disabled" : ""}>重新生成此镜</button>`,
        );
      }
    }
    if (shot.composition_approval_required) {
      const actionRow = card.querySelector(".shot-artifact-row");
      if (actionRow) {
        actionRow.insertAdjacentHTML(
          "beforeend",
          `<button type="button" class="shot-regenerate-button" data-approve-composition-retry="${Number(shot.index)}" ${running ? "disabled" : ""}>同意继续纠偏生成</button>`,
        );
      }
    }
    if (shot.has_white_model) {
      const actionRow = card.querySelector(".shot-artifact-row");
      if (actionRow) {
        actionRow.insertAdjacentHTML(
          "beforeend",
          `<button type="button" class="shot-regenerate-white-button" data-regenerate-white-shot="${Number(shot.index)}" ${running ? "disabled" : ""}>重新生成此镜白模</button>`,
        );
      }
    }
  }
  hydrateShotPreviews();
}

function invalidateCastConfirmation() {
  state.castConfirmed = false;
  if ($("#confirmCast")) $("#confirmCast").checked = false;
  updateCostAndButton();
}

function refreshShotCast(index) {
  if (!$(`[data-shot-index="${index}"]`)) return;
  renderShotGrid();
}

function updateCostAndButton() {
  const shots = state.lastJob?.shots || [];
  let single = 0; let multi = 0; let empty = 0; let unmatched = 0; let reuse = 0; let seedream = 0; let seedance = 0;
  for (const shot of shots) {
    const selected = castFor(shot.index);
    const assignment = sceneAssignmentFor(shot.index);
    const sceneGroup = assignment ? sceneGroupById(assignment.groupId) : null;
    const sceneImage = sceneGroup?.images.find((image) => image.index === Number(assignment?.imageIndex));
    if (!sceneImage) unmatched += 1;
    const previous = (shot.actor_ids || []).map(Number);
    const sameCast = previous.length === selected.length && previous.every((value, index) => value === selected[index]);
    const sameScene = Boolean(
      sceneImage && !sceneImage.file && shot.scene_group_id === assignment.groupId
      && Number(shot.scene_image_index) === Number(assignment.imageIndex)
    );
    const sameVoiceMode = shot.dialogue_voice_mode === "seedance_new_voice";
    const sameStyle = shot.final_style_id === $("#finalStyle").value;
    const samePerformance = shot.performance_dirty !== true && shot.position_binding_dirty !== true;
    const previousPositions = [...(shot.actor_mappings || [])].sort((a, b) => Number(a.slot) - Number(b.slot)).map((item) => String(item.source_position || ""));
    const currentPositions = selected.map((_actorId, offset) => positionLockValue(shot, offset + 1));
    const samePositions = previousPositions.length === currentPositions.length && previousPositions.every((value, index) => value === currentPositions[index]);
    if (shot.has_output && sameCast && sameScene && sameVoiceMode && sameStyle && samePerformance && samePositions) { reuse += 1; continue; }
    seedance += 1; seedream += 1;
    if (!selected.length) empty += 1;
    else if (selected.length === 1) single += 1;
    else multi += 1;
  }
  if (shots.length) {
    $("#costTitle").textContent = `${single} 个单人 · ${multi} 个多人 · ${empty} 个无人重绘 · ${unmatched} 个未匹配`;
    $("#costNote").textContent = `预计 ${seedream} 次 Seedream 场景板 + ${seedance} 次 Seedance；${reuse} 个分镜可复用`;
  }
  const running = state.busy || ["queued", "running"].includes(state.lastJob?.status);
  const whiteReady = shots.length > 0 && shots.every((shot) => shot.has_white_model);
  const performanceReady = shots.length > 0 && shots.every((shot) => Boolean(shot.performance));
  const newVoiceNeedsAnalysis = !performanceReady;
  if (shots.length && newVoiceNeedsAnalysis) {
    $("#costNote").textContent += "；新音色生成前需先完成全部台词与表演分析";
  }
  const generationBlocked = !shots.length || !state.castConfirmed || !sceneMappingsComplete() || running
    || ($("#requireWhiteModel").checked && !whiteReady) || newVoiceNeedsAnalysis;
  $("#generateBtn").disabled = generationBlocked;
  $("#regenerateAllBtn").disabled = generationBlocked || !shots.some((shot) => shot.has_output);
  document.querySelectorAll("[data-regenerate-shot]").forEach((button) => {
    const shot = shots.find((item) => Number(item.index) === Number(button.dataset.regenerateShot));
    const voiceModeReady = shot?.dialogue_voice_mode === "seedance_new_voice";
    button.disabled = generationBlocked || !shot?.has_output || !voiceModeReady;
    button.title = voiceModeReady ? "" : "请先生成整片，使全部分镜升级到当前声音模式。";
  });
  updatePipelineButtons();
}

function updatePipelineButtons() {
  const job = state.lastJob;
  const shots = job?.shots || [];
  const running = state.busy || job?.status === "running" || job?.status === "queued";
  const mosaicsReady = shots.length > 0 && shots.every((shot) => shot.has_mosaic);
  if ($("#mosaicBtn")) $("#mosaicBtn").disabled = !shots.length || running;
  if ($("#performanceBtn")) $("#performanceBtn").disabled = !shots.length || running || !$("#performanceConsent").checked;
  if ($("#whiteModelBtn")) $("#whiteModelBtn").disabled = !mosaicsReady || running || !$("#privacyReviewConfirmed").checked;
  document.querySelectorAll("[data-regenerate-white-shot]").forEach((button) => {
    const shot = shots.find((item) => Number(item.index) === Number(button.dataset.regenerateWhiteShot));
    button.disabled = !mosaicsReady || running || !$("#privacyReviewConfirmed").checked || !shot?.has_white_model;
  });
}

function setBusy(value) {
  state.busy = value;
  $("#analyzeBtn").disabled = value;
  if (!value) updateActorControls();
  updateCostAndButton();
  updatePipelineButtons();
}

function appendActors(form) {
  const used = new Set([...state.shotCasts.values()].flat());
  form.append("actor_count", String(state.actorCount));
  for (let index = 1; index <= state.actorCount; index += 1) {
    const role = $(`#roleDescription${index}`).value.trim();
    const person = $(`#personImage${index}`).files[0];
    const asset = $(`#personAsset${index}`).value.trim();
    const clothing = $(`#clothingImage${index}`).files[0];
    const existing = existingActor(index);
    if (!role) throw new Error(`请填写人物 ${index} 的原片角色定位。`);
    if (asset && !asset.startsWith("asset://")) throw new Error(`人物 ${index} 的授权素材 ID 必须以 asset:// 开头。`);
    if (used.has(index) && !person && !asset && !existing?.has_person) throw new Error(`请提供人物 ${index} 的人物形象。`);
    if (used.has(index) && !clothing && !existing?.has_clothing) throw new Error(`请提供人物 ${index} 的服装参考。`);
    form.append(`role_description_${index}`, role);
    if (asset) form.append(`person_asset_${index}`, asset);
    else if (person) form.append(`person_image_${index}`, person);
    if (clothing) form.append(`clothing_image_${index}`, clothing);
  }
}

function appendOptions(form) {
  form.append("scene_prompt", $("#scenePrompt").value.trim());
  form.append("image_model", $("#imageModel").value.trim());
  form.append("blur_range", $("#blurRange").value.trim());
  form.append("prompt", $("#prompt").value.trim());
  form.append("final_style_id", $("#finalStyle").value || "match_character");
  form.append("model", $("#model").value.trim());
  form.append("resolution", $("#resolution").value);
  form.append("ratio", $("#ratio").value);
  form.append("generate_audio", "true");
  form.append("dialogue_voice_mode", "seedance_new_voice");
  form.append("watermark", "false");
  form.append("delete_tos_after", $("#deleteTos").checked ? "true" : "false");
  form.append("preserve_original_audio", "false");
  const sceneLibrary = state.sceneGroups.filter((group) => group.images.length).map((group) => ({
    id: group.id,
    name: group.name.trim() || "未命名场景",
    description: group.description.trim(),
    image_count: group.images.length,
  }));
  if (!sceneLibrary.length) throw new Error("请先在场景库上传新场景图片。");
  if (!sceneMappingsComplete()) throw new Error("仍有分镜未匹配新场景，请点击自动匹配或逐镜选择。");
  form.append("scene_library", JSON.stringify(sceneLibrary));
  for (const group of state.sceneGroups) {
    group.images.forEach((image, offset) => {
      if (image.file) form.append(`scene_group_${group.id}_${offset + 1}`, image.file);
    });
  }
  form.append("scene_assignments", JSON.stringify((state.lastJob?.shots || []).map((shot) => {
    const assignment = sceneAssignmentFor(shot.index);
    return { index: Number(shot.index), group_id: assignment.groupId, image_index: Number(assignment.imageIndex) };
  })));
}

async function analyze() {
  const videoFile = $("#referenceVideo").files[0];
  if (!videoFile) return toast("请先选择长视频。", true);
  if (window.DepthFlowWorkspaces) await window.DepthFlowWorkspaces.ready;
  const form = new FormData();
  form.append("reference_video", videoFile);
  form.append("sensitivity", $("#sensitivity").value);
  form.append("manual_cuts", $("#manualCuts").value.trim());
  window.DepthFlowWorkspaces?.appendToForm(form);
  state.shotCasts.clear(); state.manualCasts.clear(); state.shotScenes.clear(); state.manualSceneShots.clear(); invalidateCastConfirmation();
  state.preserveSceneLibraryOnNextJob = true;
  setBusy(true); $("#analysisState").textContent = "正在本地分析镜头与人数……";
  try {
    const job = await api("/api/long-video/analyze", { method: "POST", body: form });
    state.jobId = job.id;
    await window.DepthFlowWorkspaces?.refresh();
    monitor(job.id, true);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function prepareMosaic() {
  try {
    if (!state.jobId || !state.shotCount) throw new Error("请先完成分镜分析。");
    const form = new FormData();
    form.append("source_job_id", state.jobId);
    form.append("block_size", $("#blockSize").value);
    form.append("face_score_threshold", $("#faceScoreThreshold").value);
    setBusy(true);
    const job = await api("/api/long-video/mosaic", { method: "POST", body: form });
    monitor(job.id, "mosaic");
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function analyzePerformance() {
  try {
    if (!state.jobId || !state.shotCount) throw new Error("请先完成分镜分析。");
    if (!$("#performanceConsent").checked) throw new Error("请先确认临时上传原片分镜用于分析。");
    if (!window.confirm(`将调用 ${state.shotCount} 次逐镜多模态分析，提取原台词、说话人、语气和表演神色；并追加 1 次全片跨镜人物身份校验，避免人物换边后映射错位。\n\n这是云端 API 步骤，是否继续？`)) return;
    const form = new FormData();
    form.append("source_job_id", state.jobId);
    form.append("performance_consent", "true");
    setBusy(true);
    const job = await api("/api/long-video/performance", { method: "POST", body: form });
    monitor(job.id, "performance");
  } catch (error) { setBusy(false); toast(error.message, true); }
}

function appendBasicVideoOptions(form) {
  form.append("model", $("#model").value.trim());
  form.append("resolution", "480p");
  form.append("ratio", $("#ratio").value);
  form.append("generate_audio", "false");
  form.append("watermark", "false");
  form.append("delete_tos_after", $("#deleteTos").checked ? "true" : "false");
}

function whiteModelForm(regenerationMode = "normal", forceShots = []) {
  if (!state.jobId || !state.shotCount) throw new Error("请先完成分镜分析。");
  if (!(state.lastJob?.shots || []).every((shot) => shot.has_mosaic)) throw new Error("请先生成全部打码分镜。");
  if (!$("#privacyReviewConfirmed").checked) throw new Error("请逐镜检查并确认没有漏脸。");
  const form = new FormData();
  form.append("source_job_id", state.jobId);
  form.append("privacy_review_confirmed", "true");
  form.append("colored_cast", document.querySelector("#coloredCastEnabled")?.checked ? "true" : "false");
  form.append("white_regeneration_mode", regenerationMode);
  form.append("force_white_shots", JSON.stringify(forceShots));
  appendBasicVideoOptions(form);
  return form;
}

async function submitWhiteModel(regenerationMode, forceShots, confirmation, monitorMode = "white") {
  try {
    const form = whiteModelForm(regenerationMode, forceShots);
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    const job = await api("/api/long-video/white-model", { method: "POST", body: form });
    monitor(job.id, monitorMode);
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function generateWhiteModel() {
  return submitWhiteModel(
    "normal",
    [],
    `将按 ${state.shotCount} 个分镜检查并提交需要生成的 Seedance 白模任务。\n\n白模固定使用 Seedance 2.0 / 480p 低成本规格；最终重绘仍使用你选择的成片分辨率。系统只使用打码视频作为参考，配置未变化的已有白模会复用。\n\n这是付费步骤，是否继续？`,
  );
}

async function regenerateWhiteModelShot(index) {
  const shot = (state.lastJob?.shots || []).find((item) => Number(item.index) === Number(index));
  if (!shot?.has_white_model) return toast(`分镜 ${String(index).padStart(2, "0")} 还没有可重新生成的白模。`, true);
  return submitWhiteModel(
    "selected",
    [Number(index)],
    `只重新生成分镜 ${String(index).padStart(2, "0")} 的白模视频。\n\n会按 Seedance 2.0 / 480p 新建 1 次付费任务；其他白模分镜直接复用，完成后自动重新合并完整白模视频。最终重绘成片不会自动改动。\n\n是否继续？`,
    "white-shot",
  );
}

function castPayload() {
  return (state.lastJob?.shots || []).map((shot) => {
    const actorIds = castFor(shot.index);
    return {
      index: Number(shot.index),
      actor_ids: actorIds,
      actor_mappings: actorIds.map((actorId, offset) => ({
        slot: offset + 1,
        actor_id: actorId,
        position_lock: positionLockValue(shot, offset + 1),
      })),
    };
  });
}

function generationForm(regenerationMode = "normal", forceShots = [], compositionRetryApproved = false) {
  if (!state.jobId || !state.shotCount) throw new Error("请先完成分镜分析。");
  if (!state.castConfirmed) throw new Error("请先核对全部分镜角色并勾选确认。");
  if (!sceneMappingsComplete()) throw new Error("仍有分镜没有匹配新场景。");
  const form = new FormData();
  form.append("source_job_id", state.jobId);
  form.append("shot_casts", JSON.stringify(castPayload()));
  form.append("cast_confirmed", "true");
  form.append("require_white_model", $("#requireWhiteModel").checked ? "true" : "false");
  form.append("regeneration_mode", regenerationMode);
  form.append("force_shots", JSON.stringify(forceShots));
  form.append("composition_retry_approved", compositionRetryApproved ? "true" : "false");
  appendActors(form); appendOptions(form);
  return form;
}

async function submitGeneration(regenerationMode, forceShots, confirmation, compositionRetryApproved = false) {
  try {
    const form = generationForm(regenerationMode, forceShots, compositionRetryApproved);
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    const job = await api("/api/long-video/generate", { method: "POST", body: form });
    monitor(job.id, "final");
  } catch (error) { setBusy(false); toast(error.message, true); }
}

async function approveCompositionRetry(index) {
  const shot = (state.lastJob?.shots || []).find((item) => Number(item.index) === Number(index));
  if (!shot?.composition_approval_required) return toast("本镜头当前不需要人工同意。", true);
  const retryCount = Number(shot.composition_retry_count || 0);
  return submitGeneration(
    "selected",
    [Number(index)],
    `分镜 ${String(index).padStart(2, "0")} 已完成 ${retryCount} 次构图纠偏，仍未通过自动验收。\n\n继续会再创建 1 次 Seedance 付费任务；不会自动连续追加更多任务。若再次不通过，系统会重新停下等待你的同意。\n\n是否同意继续？`,
    true,
  );
}

async function generate() {
  const selectedShots = castPayload();
  const empty = selectedShots.filter((item) => !item.actor_ids.length).length;
  return submitGeneration(
    "normal",
    [],
    `已核对 ${selectedShots.length} 个分镜\n\n系统会复用配置未变化的已有成片；其余分镜将重新生成场景板与 Seedance 视频。\n其中无人场景重绘 ${empty} 个，不再保留原画面。\n\n系统会先在本地隔离原片对白，只让 Seedance 参考逐字语速、停顿、重音与情绪并生成新人物音色；原片对白、BGM和环境音均不会合回，最终音轨完全来自Seedance直生。\n\n这是付费步骤，是否提交？`,
  );
}

async function regenerateAll() {
  const shotIndexes = (state.lastJob?.shots || []).map((shot) => Number(shot.index));
  return submitGeneration(
    "all",
    shotIndexes,
    `将强制重新生成全部 ${shotIndexes.length} 个分镜。\n\n会新建 ${shotIndexes.length} 次 Seedance 付费任务；白模与未变化的场景板会复用，场景配置变化时会补建 Seedream 场景板。\n每镜提交的动作参考完全静音；Seedance 只按人工校订后的说话人、台词和时间轴生成新人物声音，原片对白、BGM、歌声与环境音都不会进入参考或成片。\n完成后自动重新合并整片。\n\n是否继续？`,
  );
}

async function regenerateShot(index) {
  const shot = (state.lastJob?.shots || []).find((item) => Number(item.index) === Number(index));
  if (!shot?.has_output) return toast(`分镜 ${String(index).padStart(2, "0")} 还没有可重新生成的成片。`, true);
  return submitGeneration(
    "selected",
    [Number(index)],
    `只重新生成分镜 ${String(index).padStart(2, "0")}。\n\n会新建 1 次 Seedance 付费任务；其他已完成分镜直接复用。白模与未变化的场景板会复用。\n本镜提交的动作参考完全静音；Seedance 只按人工校订后的说话人、台词和时间轴生成新人物声音，原片对白、BGM、歌声与环境音都不会进入参考或成片。\n完成后自动重新合并整片。\n\n是否继续？`,
  );
}

async function savePerformanceEdits(index) {
  const card = $(`[data-shot-index="${index}"]`);
  const dialogue = card?.querySelector(`[data-performance-dialogue="${index}"]`)?.value ?? "";
  const performance = card?.querySelector(`[data-performance-acting="${index}"]`)?.value ?? "";
  const dialogueSources = Array.from(card?.querySelectorAll("[data-dialogue-source-index]") || []).map((node) => {
    const value = String(node.value || "uncertain");
    return {
      index: Number(node.dataset.dialogueSourceIndex),
      source_type: value.startsWith("p") ? "character" : value,
      speaker_slot: value.startsWith("p") ? Number(value.slice(1)) : 0,
    };
  });
  const button = card?.querySelector(`[data-save-performance="${index}"]`);
  if (!state.jobId) return toast("请先完成分镜分析。", true);
  if (dialogue.length + performance.length > 2600) return toast("本分镜台词与表演合计最多 2600 字，请精简后保存。", true);
  if (button) { button.disabled = true; button.textContent = "正在保存…"; }
  try {
    const job = await api(`/api/long-video/${state.jobId}/shots/${index}/performance`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dialogue_text: dialogue, performance_text: performance, dialogue_sources: dialogueSources }),
    });
    state.performanceDrafts.delete(Number(index));
    render(job);
    toast(`分镜 ${String(index).padStart(2, "0")} 的台词与表演已保存；生成时将优先使用人工文字。`);
  } catch (error) {
    if (button) { button.disabled = false; button.textContent = "保存本镜校订"; }
    toast(error.message, true);
  }
}

function resetPerformanceEditor(index) {
  const shot = (state.lastJob?.shots || []).find((item) => Number(item.index) === Number(index));
  const card = $(`[data-shot-index="${index}"]`);
  if (!shot || !card) return;
  const dialogue = extractedDialogueText(shot.performance || {});
  const performance = extractedPerformanceText(shot.performance || {});
  card.querySelector(`[data-performance-dialogue="${index}"]`).value = dialogue;
  card.querySelector(`[data-performance-acting="${index}"]`).value = performance;
  state.performanceDrafts.set(Number(index), { dialogue, performance });
  toast(`分镜 ${String(index).padStart(2, "0")} 已恢复为 AI 原始提取文字，点击保存后才会生效。`);
}

async function deleteShot(index) {
  const shots = state.lastJob?.shots || [];
  const shot = shots.find((item) => Number(item.index) === Number(index));
  if (!shot) return toast(`找不到分镜 ${String(index).padStart(2, "0")}。`, true);
  if (shots.length <= 1) return toast("至少需要保留一个分镜。", true);
  const duration = Number(shot.duration || 0).toFixed(2);
  if (!window.confirm(`删除分镜 ${String(index).padStart(2, "0")}（${duration} 秒）？\n\n该镜将从后续白模生成和最终合并中排除；本地原分镜文件仍会保留。`)) return;
  try {
    setBusy(true);
    const job = await api(`/api/long-video/${state.lastJob.id}/shots/${Number(index)}`, { method: "DELETE" });
    state.shotCasts.delete(Number(index));
    state.manualCasts.delete(Number(index));
    state.shotScenes.delete(Number(index));
    state.manualSceneShots.delete(Number(index));
    state.castConfirmed = false;
    render(job, false);
    toast(`分镜 ${String(index).padStart(2, "0")} 已删除，剩余 ${job.shot_count} 个分镜。`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function video(selector, url, key) {
  const node = $(selector);
  if (node.dataset.key === key) return;
  node.src = `${url}?preview=${encodeURIComponent(key)}`;
  node.dataset.key = key;
  node.load();
}

function downloadCenterLink(url, name, label, primary = false) {
  if (!url) return "";
  return `<a class="${primary ? "primary-button" : "secondary-button"}" href="${escapeHtml(url)}?download=1" data-result-download-url="${escapeHtml(url)}" data-result-download-name="${escapeHtml(name)}">${escapeHtml(label)}</a>`;
}

function downloadShotRows(job, artifact) {
  const isWhite = artifact === "white";
  return (job.shots || []).map((shot) => {
    const index = Number(shot.index);
    const available = isWhite ? shot.has_white_model : shot.has_raw_output;
    const url = isWhite ? shot.white_model_url : shot.raw_output_url;
    const fileName = `${isWhite ? "白模" : "裁剪前成片"}_分镜${String(index).padStart(2, "0")}_${job.id}.mp4`;
    return `<div class="download-shot-row"><span><b>分镜 ${String(index).padStart(2, "0")}</b><small>${Number(shot.start || 0).toFixed(2)}–${Number(shot.end || 0).toFixed(2)}秒 · ${Number(shot.duration || 0).toFixed(2)}秒</small></span>${available ? downloadCenterLink(url, fileName, isWhite ? "下载白模片段" : "下载裁剪前成片") : `<em>${isWhite ? "白模" : "裁剪前成片"}尚未生成</em>`}</div>`;
  }).join("");
}

function renderDownloadCenter(job) {
  const center = $("#downloadCenter");
  const hasWhite = Boolean(job.has_white_model || (job.shots || []).some((shot) => shot.has_white_model));
  const hasFinal = Boolean(job.has_output || (job.shots || []).some((shot) => shot.has_raw_output));
  center.classList.toggle("hidden", !hasWhite && !hasFinal);
  if (!hasWhite && !hasFinal) return;
  const bundleLinks = [];
  if (job.has_white_model) bundleLinks.push(downloadCenterLink(job.white_model_url, `完整白模绿幕视频_${job.id}.mp4`, "下载完整白模视频"));
  if (job.has_output) bundleLinks.push(downloadCenterLink(job.output_url, `完整最终成片_${job.id}.mp4`, "下载整合最终成片", true));
  $("#downloadBundleLinks").innerHTML = bundleLinks.join("");
  $("#whiteShotDownloadList").innerHTML = downloadShotRows(job, "white");
  $("#finalShotDownloadList").innerHTML = downloadShotRows(job, "final");
}

function render(job, analysisOnly = false) {
  const firstRender = state.lastJob?.id !== job.id;
  if (firstRender) {
    // Per-shot choices belong to one analysis job. Keeping them while switching jobs
    // can force a newly detected multi-person shot back to the previous one-person map.
    state.shotCasts.clear();
    state.manualCasts.clear();
    state.shotScenes.clear();
    state.manualSceneShots.clear();
    state.castConfirmed = false;
    state.performanceDrafts.clear();
    state.positionDrafts.clear();
    initializeSceneLibrary(job, state.preserveSceneLibraryOnNextJob);
    state.preserveSceneLibraryOnNextJob = false;
  }
  state.lastJob = job; state.jobId = job.id; state.activeJobId = job.id; state.shotCount = job.shot_count || 0;
  const detectedMax = Math.max(0, ...(job.shots || []).map(detectedPeopleCount));
  const requiredActorCount = Math.max(job.actors?.length || 0, detectedMax);
  if (firstRender && requiredActorCount) {
    state.actorCount = Math.max(1, Math.min(4, requiredActorCount));
    renderActorList(job.actors);
  }
  applyAutomaticCastSuggestions();
  if (!state.manualCasts.size && sceneMappingsComplete() && (job.shots || []).length && (job.shots || []).every((shot) => shot.cast_confirmed === true)) {
    state.castConfirmed = true;
  }
  $("#emptyTask").classList.add("hidden"); $("#activeTask").classList.remove("hidden");
  const kindLabels = {
    long_generate: "逐分镜最终重绘与合并",
    long_mosaic: "本地多脸时序打码",
    long_performance: "台词与表演神色分析",
    long_white_model: "白模绿幕母版生成",
    long_edit: "分镜编辑",
  };
  $("#jobKind").textContent = kindLabels[job.kind] || "本地分镜与人数分析";
  $("#jobStage").textContent = job.stage;
  $("#jobMeta").textContent = `本地任务：${job.id}${job.source_duration ? ` · 原片 ${Number(job.source_duration).toFixed(2)} 秒` : ""}`;
  $("#progressPercent").textContent = `${job.progress}%`; $("#progressBar").style.width = `${job.progress}%`;
  $("#jobLogs").textContent = job.logs.join("\n"); $("#jobLogs").scrollTop = $("#jobLogs").scrollHeight;
  renderShotGrid();
  $("#castConfirmBar").classList.toggle("hidden", !state.shotCount);
  $("#confirmCast").checked = state.castConfirmed;
  if (state.shotCount) $("#analysisState").textContent = `已拆分 ${state.shotCount} 个分镜，请核对每镜角色`;
  updateActorControls(); updateCostAndButton();
  $("#mosaicPreviewArea").classList.toggle("hidden", !job.has_mosaic);
  if (job.has_mosaic) {
    video("#mosaicVideo", job.mosaic_url, `${job.id}-mosaic-${job.created_at}`);
    $("#mosaicDownloadLink").href = `${job.mosaic_url}?download=1`;
    $("#mosaicDownloadLink").dataset.downloadUrl = job.mosaic_url;
    $("#mosaicDownloadLink").dataset.suggestedName = `人脸打码视频_${job.id}.mp4`;
  }
  $("#whitePreviewArea").classList.toggle("hidden", !job.has_white_model);
  if (job.has_white_model) {
    const whiteRevision = (job.shots || []).map((shot) => shot.white_model_task_id || shot.white_model_signature || "none").join("-");
    video("#whiteModelVideo", job.white_model_url, `${job.id}-white-${job.created_at}-${whiteRevision}`);
    $("#whiteModelDownloadLink").href = `${job.white_model_url}?download=1`;
    $("#whiteModelDownloadLink").dataset.downloadUrl = job.white_model_url;
    $("#whiteModelDownloadLink").dataset.suggestedName = `白模绿幕视频_${job.id}.mp4`;
  }
  $("#finalResultArea").classList.toggle("hidden", !job.has_output);
  if (job.has_output) {
    const outputRevision = (job.shots || []).map((shot) => shot.task_id || shot.output_signature || "none").join("-");
    video("#finalResultVideo", job.output_url, `${job.id}-final-${job.created_at}-${outputRevision}`);
    $("#finalDownloadLink").href = `${job.output_url}?download=1`;
    $("#finalDownloadLink").dataset.downloadUrl = job.output_url;
    $("#finalDownloadLink").dataset.suggestedName = `长视频多分镜复刻_${job.id}.mp4`;
  }
  renderDownloadCenter(job);
  if (analysisOnly && job.status === "succeeded") $("#shotGrid").scrollIntoView({ behavior: "smooth", block: "start" });
}

function monitor(id, mode) {
  clearInterval(state.pollTimer);
  const poll = async () => {
    try {
      const job = await api(`/api/jobs/${id}`);
      render(job, mode === true || mode === "analysis");
      if (["succeeded", "failed", "awaiting_approval"].includes(job.status)) {
        clearInterval(state.pollTimer); setBusy(false);
        const successMessages = {
          mosaic: "打码视频已完成，请逐镜检查是否有漏脸。",
          performance: "全部分镜的台词与表演信息已提取。",
          white: "白模绿幕视频已按原时长合并完成。",
          "white-shot": "所选分镜白模已重新生成，并重新合并完整白模视频。",
          final: "最终重绘视频已生成并合并完成。",
        };
        if (job.status === "succeeded") toast((mode === true || mode === "analysis") ? `分镜与人数分析完成：${job.shot_count}个，请逐镜核对角色。` : (successMessages[mode] || "任务已完成。"));
        else if (job.status === "awaiting_approval") toast("自动构图纠偏已达到上限，请预览对应分镜后决定是否继续付费生成。", true);
        else toast(window.DepthFlowUI?.formatJobError?.(job) || job.error || "任务失败。", true);
      }
    } catch (error) { clearInterval(state.pollTimer); setBusy(false); toast(error.message, true); }
  };
  poll(); state.pollTimer = setInterval(poll, 1800);
  $(".task-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function openFolder() {
  if (!state.activeJobId) return;
  try { await api(`/api/jobs/${state.activeJobId}/open-folder`, { method: "POST" }); } catch (error) { toast(error.message, true); }
}

async function saveFile(url, name, type) {
  if (typeof window.showSaveFilePicker !== "function") { const anchor = document.createElement("a"); anchor.href = `${url}?download=1`; anchor.download = name; anchor.click(); return; }
  try {
    const picker = type === "image"
      ? { description: "JPEG 图片", accept: { "image/jpeg": [".jpg", ".jpeg"] } }
      : type === "json"
        ? { description: "JSON 数据", accept: { "application/json": [".json"] } }
        : { description: "MP4 视频", accept: { "video/mp4": [".mp4"] } };
    const handle = await window.showSaveFilePicker({ suggestedName: name, types: [picker] });
    const response = await fetch(url); const writable = await handle.createWritable();
    if (response.body) await response.body.pipeTo(writable); else { await writable.write(await response.blob()); await writable.close(); }
    toast(`已保存：${handle.name}`);
  } catch (error) { if (error?.name !== "AbortError") toast(error.message, true); }
}

$("#shotGrid").addEventListener("change", (event) => {
  const sceneGroupSelect = event.target.closest("[data-shot-scene-group]");
  if (sceneGroupSelect) {
    const shotIndex = Number(sceneGroupSelect.dataset.shotSceneGroup);
    const group = sceneGroupById(sceneGroupSelect.value);
    if (group?.images.length) state.shotScenes.set(shotIndex, { groupId: group.id, imageIndex: 1 });
    else state.shotScenes.delete(shotIndex);
    state.manualSceneShots.add(shotIndex);
    renderShotGrid(); invalidateCastConfirmation(); return;
  }
  const sceneImageSelect = event.target.closest("[data-shot-scene-image]");
  if (sceneImageSelect) {
    const shotIndex = Number(sceneImageSelect.dataset.shotSceneImage);
    const current = sceneAssignmentFor(shotIndex);
    if (current) state.shotScenes.set(shotIndex, { ...current, imageIndex: Number(sceneImageSelect.value) });
    state.manualSceneShots.add(shotIndex);
    renderShotGrid(); invalidateCastConfirmation(); return;
  }
  const select = event.target.closest("[data-person-mapping]");
  if (!select) return;
  const shotIndex = Number(select.dataset.personMapping);
  const slotIndex = Number(select.dataset.personSlot) - 1;
  const actorId = Number(select.value);
  const current = [...castFor(shotIndex)];
  const previousActor = current[slotIndex];
  const shot = (state.lastJob?.shots || []).find((item) => Number(item.index) === shotIndex);
  const characterId = continuityCharacterFor(shot, slotIndex + 1);
  if (characterId) {
    const globalMap = continuityActorMap();
    const previousGlobalActor = globalMap.get(characterId) || previousActor;
    const conflictingEntry = [...globalMap.entries()].find(([otherCharacterId, mappedActorId]) => (
      Number(otherCharacterId) !== characterId && Number(mappedActorId) === actorId
    ));
    const conflictingCharacterId = Number(conflictingEntry?.[0] || 0);
    for (const otherShot of state.lastJob?.shots || []) {
      const otherIndex = Number(otherShot.index);
      const updated = [...castFor(otherIndex)];
      let changed = false;
      updated.forEach((_mappedActorId, offset) => {
        const otherCharacterId = continuityCharacterFor(otherShot, offset + 1);
        if (otherCharacterId === characterId) {
          updated[offset] = actorId;
          changed = true;
        } else if (conflictingCharacterId && otherCharacterId === conflictingCharacterId && previousGlobalActor) {
          updated[offset] = previousGlobalActor;
          changed = true;
        }
      });
      if (changed) {
        state.shotCasts.set(otherIndex, updated);
        state.manualCasts.add(otherIndex);
      }
    }
    renderShotGrid(); invalidateCastConfirmation();
    toast(`原片身份 C${characterId} 已在全部分镜统一映射为人物${actorId}。`);
    return;
  }
  const duplicateIndex = current.indexOf(actorId);
  if (duplicateIndex >= 0 && duplicateIndex !== slotIndex) current[duplicateIndex] = previousActor;
  current[slotIndex] = actorId;
  state.shotCasts.set(shotIndex, current); state.manualCasts.add(shotIndex);
  refreshShotCast(shotIndex); invalidateCastConfirmation();
});

$("#shotGrid").addEventListener("input", (event) => {
  const positionLock = event.target.closest("[data-position-lock]");
  if (positionLock) {
    const key = `${Number(positionLock.dataset.positionLock)}:${Number(positionLock.dataset.positionSlot)}`;
    state.positionDrafts.set(key, positionLock.value);
    invalidateCastConfirmation();
    return;
  }
  const dialogue = event.target.closest("[data-performance-dialogue]");
  const acting = event.target.closest("[data-performance-acting]");
  if (!dialogue && !acting) return;
  const index = Number((dialogue || acting).dataset.performanceDialogue || (dialogue || acting).dataset.performanceActing);
  const draft = state.performanceDrafts.get(index) || {};
  if (dialogue) draft.dialogue = dialogue.value;
  if (acting) draft.performance = acting.value;
  state.performanceDrafts.set(index, draft);
});

$("#shotGrid").addEventListener("click", (event) => {
  const compareButton = event.target.closest("[data-shot-compare-src]");
  if (compareButton) {
    const comparison = compareButton.closest(".shot-comparison");
    const video = comparison?.querySelector(".shot-preview");
    if (!video) return;
    video.pause();
    video.removeAttribute("src");
    video.dataset.loadedSource = "";
    video.dataset.mediaSrc = compareButton.dataset.shotCompareSrc;
    comparison.querySelectorAll("[data-shot-compare-src]").forEach((button) => button.classList.toggle("active", button === compareButton));
    const caption = comparison.querySelector(".shot-preview-caption");
    if (caption) caption.textContent = `当前预览：${compareButton.dataset.shotCompareLabel}`;
    const loadButton = comparison.querySelector("[data-load-shot-preview]");
    if (loadButton) loadButton.textContent = `加载${compareButton.dataset.shotCompareLabel}预览`;
    activateShotPreview(video, () => {}, true);
    return;
  }
  const previewButton = event.target.closest("[data-load-shot-preview]");
  if (previewButton) {
    const card = previewButton.closest(".shot-card");
    activateShotPreview(card?.querySelector(".shot-preview"), () => {}, true);
    return;
  }
  const savePerformanceButton = event.target.closest("[data-save-performance]");
  if (savePerformanceButton) {
    savePerformanceEdits(Number(savePerformanceButton.dataset.savePerformance));
    return;
  }
  const resetPerformanceButton = event.target.closest("[data-reset-performance]");
  if (resetPerformanceButton) {
    resetPerformanceEditor(Number(resetPerformanceButton.dataset.resetPerformance));
    return;
  }
  const deleteButton = event.target.closest("[data-delete-shot]");
  if (deleteButton) {
    deleteShot(Number(deleteButton.dataset.deleteShot));
    return;
  }
  const regenerateWhiteButton = event.target.closest("[data-regenerate-white-shot]");
  if (regenerateWhiteButton) {
    regenerateWhiteModelShot(Number(regenerateWhiteButton.dataset.regenerateWhiteShot));
    return;
  }
  const regenerateButton = event.target.closest("[data-regenerate-shot]");
  if (regenerateButton) {
    regenerateShot(Number(regenerateButton.dataset.regenerateShot));
    return;
  }
  const approveCompositionButton = event.target.closest("[data-approve-composition-retry]");
  if (approveCompositionButton) {
    approveCompositionRetry(Number(approveCompositionButton.dataset.approveCompositionRetry));
    return;
  }
  const addButton = event.target.closest("[data-add-person]");
  if (addButton) {
    const shotIndex = Number(addButton.dataset.addPerson); const current = [...castFor(shotIndex)];
    if (current.length >= Math.min(4, state.actorCount)) return toast("请先在上方角色库增加人物，再为本镜头增加原片人物槽位。", true);
    const unused = Array.from({ length: state.actorCount }, (_value, offset) => offset + 1).find((value) => !current.includes(value));
    if (unused) current.push(unused);
    state.shotCasts.set(shotIndex, current); state.manualCasts.add(shotIndex);
    refreshShotCast(shotIndex); invalidateCastConfirmation(); return;
  }
  const removeButton = event.target.closest("[data-remove-person]");
  if (removeButton) {
    const shotIndex = Number(removeButton.dataset.removePerson); const current = [...castFor(shotIndex)];
    current.pop(); state.shotCasts.set(shotIndex, current); state.manualCasts.add(shotIndex);
    refreshShotCast(shotIndex); invalidateCastConfirmation(); return;
  }
  const clearButton = event.target.closest("[data-clear-cast]");
  if (clearButton) {
    const shotIndex = Number(clearButton.dataset.clearCast); state.shotCasts.set(shotIndex, []); state.manualCasts.add(shotIndex);
    refreshShotCast(shotIndex); invalidateCastConfirmation(); return;
  }
  const link = event.target.closest(".shot-download");
  if (link) { event.preventDefault(); saveFile(link.dataset.url, link.dataset.name, link.dataset.type); }
});

$("#confirmCast").addEventListener("change", () => {
  if ($("#confirmCast").checked && !sceneMappingsComplete()) {
    $("#confirmCast").checked = false;
    state.castConfirmed = false;
    updateCostAndButton();
    return toast("仍有分镜没有匹配新场景，请先自动匹配或逐镜选择。", true);
  }
  state.castConfirmed = $("#confirmCast").checked;
  updateCostAndButton();
  if (state.castConfirmed) $("#generateBtn").scrollIntoView({ behavior: "smooth", block: "center" });
});

$("#finalStyleLibrary").addEventListener("click", (event) => {
  const button = event.target.closest("[data-final-style]");
  if (!button) return;
  renderFinalStyleLibrary(button.dataset.finalStyle);
  updateCostAndButton();
});

$("#finalDownloadLink").addEventListener("click", (event) => {
  event.preventDefault(); const link = $("#finalDownloadLink");
  if (link.dataset.downloadUrl) saveFile(link.dataset.downloadUrl, link.dataset.suggestedName, "video");
});

$("#downloadCenter").addEventListener("click", (event) => {
  const link = event.target.closest("[data-result-download-url]");
  if (!link) return;
  event.preventDefault();
  saveFile(link.dataset.resultDownloadUrl, link.dataset.resultDownloadName, "video");
});

async function loadConfig() {
  try {
    if (window.DepthFlowWorkspaces) await window.DepthFlowWorkspaces.ready;
    const config = await api("/api/config"); state.config = config;
    $("#scenePrompt").value = config.long_scene_prompt; $("#prompt").value = config.long_video_prompt;
    $("#model").value = config.model; $("#imageModel").value = config.image_model;
    badge("#arkBadge", config.ark_ready, "API 已配置", "API 未配置");
    badge("#channelBadge", config.temporary_upload_ready, "免费临时通道可用", "临时通道不可用");
    const activeWorkspace = window.DepthFlowWorkspaces?.active();
    const latest = activeWorkspace?.job_id
      ? await api(`/api/jobs/${activeWorkspace.job_id}`)
      : (window.DepthFlowWorkspaces ? null : config.latest_long_video);
    const savedStyle = latest?.shots?.find((shot) => shot.final_style_id)?.final_style_id || config.default_final_style || "match_character";
    renderFinalStyleLibrary(savedStyle);
    state.lastJob = null;
    if (latest) {
      render(latest, latest.kind !== "long_generate");
      if (["queued", "running"].includes(latest.status)) {
        setBusy(true);
        const mode = latest.kind === "long_analyze" ? "analysis"
          : latest.kind === "long_mosaic" ? "mosaic"
          : latest.kind === "long_performance" ? "performance"
          : latest.kind === "long_white_model" ? "white" : "final";
        monitor(latest.id, mode);
      }
    }
    else {
      state.actorCount = 1;
      renderActorList([]);
      initializeSceneLibrary(null);
    }
  } catch (error) { toast(error.message, true); renderActorList([]); }
}

bind("#referenceVideo", "#videoFileName");
$("#sceneGroupList").addEventListener("change", (event) => {
  const fileInput = event.target.closest("[data-scene-files]");
  if (fileInput) {
    const group = sceneGroupById(fileInput.dataset.sceneFiles);
    if (!group) return;
    const files = Array.from(fileInput.files || []).slice(0, 6);
    if (!files.length) return;
    group.images = files.map((file, offset) => ({ index: offset + 1, name: file.name, file, url: "" }));
    for (const [shotIndex, assignment] of state.shotScenes) {
      if (assignment.groupId === group.id && assignment.imageIndex > group.images.length) state.shotScenes.delete(shotIndex);
    }
    renderSceneLibrary();
    if (state.lastJob?.shots?.length) autoMatchScenes(false);
    else invalidateCastConfirmation();
    return;
  }
  const nameInput = event.target.closest("[data-scene-name]");
  if (nameInput) {
    const group = sceneGroupById(nameInput.dataset.sceneName);
    if (group) group.name = nameInput.value.trim() || "未命名场景";
    renderShotGrid(); invalidateCastConfirmation(); return;
  }
  const descriptionInput = event.target.closest("[data-scene-description]");
  if (descriptionInput) {
    const group = sceneGroupById(descriptionInput.dataset.sceneDescription);
    if (group) group.description = descriptionInput.value.trim();
    invalidateCastConfirmation();
  }
});
$("#sceneGroupList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-scene-group]");
  if (!button || state.sceneGroups.length <= 1) return;
  const groupId = button.dataset.removeSceneGroup;
  state.sceneGroups = state.sceneGroups.filter((group) => group.id !== groupId);
  for (const [shotIndex, assignment] of state.shotScenes) {
    if (assignment.groupId === groupId) state.shotScenes.delete(shotIndex);
  }
  renderSceneLibrary(); renderShotGrid(); invalidateCastConfirmation();
});
$("#addSceneGroupBtn").addEventListener("click", () => {
  if (state.sceneGroups.length >= 20) return toast("场景库最多支持20个场景组。", true);
  state.sceneGroups.push(normalizeSceneGroup(null, state.sceneGroups.length + 1));
  renderSceneLibrary(); invalidateCastConfirmation();
});
$("#autoMatchScenesBtn").addEventListener("click", () => autoMatchScenes(true));
$("#sensitivity").addEventListener("input", () => { $("#sensitivityValue").textContent = $("#sensitivity").value; });
$("#analyzeBtn").addEventListener("click", analyze); $("#generateBtn").addEventListener("click", generate);
$("#regenerateAllBtn").addEventListener("click", regenerateAll);
$("#mosaicBtn").addEventListener("click", prepareMosaic);
$("#performanceBtn").addEventListener("click", analyzePerformance);
$("#whiteModelBtn").addEventListener("click", generateWhiteModel);
$("#performanceConsent").addEventListener("change", updatePipelineButtons);
$("#privacyReviewConfirmed").addEventListener("change", updatePipelineButtons);
$("#requireWhiteModel").addEventListener("change", updateCostAndButton);
$("#addActorBtn").addEventListener("click", addActor); $("#removeActorBtn").addEventListener("click", removeActor);
$("#openFolderBtn").addEventListener("click", openFolder);
$("#resetScenePromptBtn").addEventListener("click", () => { $("#scenePrompt").value = state.config.long_scene_prompt; });
$("#resetPromptBtn").addEventListener("click", () => { $("#prompt").value = state.config.long_video_prompt; });
$("#mosaicDownloadLink").addEventListener("click", (event) => {
  event.preventDefault(); const link = $("#mosaicDownloadLink");
  if (link.dataset.downloadUrl) saveFile(link.dataset.downloadUrl, link.dataset.suggestedName, "video");
});
$("#whiteModelDownloadLink").addEventListener("click", (event) => {
  event.preventDefault(); const link = $("#whiteModelDownloadLink");
  if (link.dataset.downloadUrl) saveFile(link.dataset.downloadUrl, link.dataset.suggestedName, "video");
});
loadConfig();
