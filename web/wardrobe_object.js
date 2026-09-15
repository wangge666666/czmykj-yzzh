/* Object/background edits share material controls, with independent source histories. */
window.startWardrobeObject = async function (mode = 'dynamic_object') {
  const scene = mode === 'dynamic_scene', namespace = scene ? 'scene' : 'object';
  const whiteLabel = scene ? '绿底白模' : '主角白模';
  const editLabel = scene ? '背景' : '物品';
  const referenceLabel = scene ? '场景参考图' : '物品参考图';
  const storagePrefix = `depthflow.${namespace}`;
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const api = async (url, options = {}) => {
    const response = await fetch(url.replace('/api/wardrobe-object/', `/api/wardrobe-${namespace}/`), options);
    const body = await response.json();
    if (!response.ok) { const error = new Error(body.error || `请求失败 ${response.status}`); error.status = response.status; throw error; }
    return body;
  };
  const notice = (message, error = false) => {
    const node = $('#toast'); node.textContent = message; node.classList.toggle('error', error); node.classList.add('show');
    clearTimeout(node._timer); node._timer = setTimeout(() => node.classList.remove('show'), 6500);
  };
  window.toast = notice;
  const savedWorkflow = localStorage.getItem(`${storagePrefix}.workflow`);
  const state = {workflow: ['library', 'references'].includes(savedWorkflow) ? savedWorkflow : scene ? 'library' : 'references', source: null, generation: null,
    working: false, supported: !scene, dirty: false, timer: 0, uploadTimer: 0, library: [], uploadBusy: false, uploadAsset: '', revision: '', loadVersion: 0,
    videoChoice: 'library', videoPending: false, libraryVersion: 0, uploadVersion: 0, autoVideoName: ''};
  const mediaUrls = new Map();
  const show = (id, yes) => $(id).classList.toggle('hidden', !yes);
  const file = id => $(id).files[0];
  const actor = () => $('#personAsset')?.value?.trim() || '';
  const meta = () => state.source?.wardrobe_dynamic || {};
  const active = job => job && ['queued', 'running', 'submitted'].includes(job.status);
  const busy = () => state.working || active(state.source) || active(state.generation);
  const refs = () => state.workflow === 'references';
  const form = values => { const fd = new FormData(); Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== null) fd.append(key, value); }); return fd; };
  const post = (path, data) => {const fd=form(data);if(path==='/api/wardrobe-swap/generate')window.DepthFlowInlineCast?.append(fd);if(path==='/api/wardrobe-swap/white-model'){if(window.DepthFlowInlineCast)window.DepthFlowInlineCast.appendWhite(fd);else fd.append('inline_cast_count',1);}return api(path,{method:'POST',body:fd});};
  const media = (id, url) => {
    const node = $(id), safe = typeof url === 'string' && /^(\/api\/|blob:|https?:\/\/)/.test(url) ? url : '';
    if (node.getAttribute('src') !== safe) { if (safe) node.src = safe; else node.removeAttribute('src'); }
    show(id, !!safe);
  };
  const download = (id, url) => { show(id, !!url); if (url) $(id).href = url + (url.includes('?') ? '&' : '?') + 'download=1'; };
  const previewFile = (input, target) => {
    if (mediaUrls.has(input)) URL.revokeObjectURL(mediaUrls.get(input));
    const value = file(input), url = value ? URL.createObjectURL(value) : '';
    mediaUrls.set(input, url); media(target, url);
  };
  function bindings() {
    const items = [{token: '@视频1', label: refs() ? whiteLabel : '角色库原片视频', ready: !!state.source && !state.dirty && (refs() ? state.source.has_white_model : !!meta().video_asset && !state.videoPending)}];
    if (refs()) {items.push({token:'@图片1',label:'原片人物（未上传服装时含服装）',ready:!!actor()});if(window.DepthFlowInlineCast?.hasClothing()??!!file('#objectClothing'))items.push({token:'@图片2',label:'人物1服装',ready:true});}
    if (file('#objectItem')) items.push({token: refs() ? '@图片'+(2+Number(window.DepthFlowInlineCast?.hasClothing()??!!file('#objectClothing'))) : '@图片1', label: referenceLabel, ready: true});
    items.push(...(window.DepthFlowInlineCast?.bindings() || []));
    return items;
  }
  function insert(token) {
    const input = $('#objectPrompt'), before = input.value.slice(0, input.selectionStart);
    const match = before.match(/@[^\s@，。；]*$/);
    input.setRangeText(token, match ? input.selectionStart - match[0].length : input.selectionStart, input.selectionEnd, 'end');
    input.focus(); show('#objectMentions', false); readiness();
  }
  function readiness() {
    window.DepthFlowInlineCast?.configure({base:(refs()?1+Number(window.DepthFlowInlineCast?.hasClothing()??!!file('#objectClothing')):0)+Number(!!file('#objectItem')),enabled:refs(),busy:busy(),prompts:[$('#objectPrompt')],getPreview:()=>({mode,custom:$('#objectPrompt').value,white_extra:$('#objectWhitePrompt').value,has_replacement:!!file('#objectItem')})});
    const locked = busy() || !state.supported, source = state.source, data = meta();
    const threshold = Number($('#objectThreshold').value);
    const thresholdInvalid = !Number.isFinite(threshold) || threshold < .3 || threshold > .9;
    let sourceProblem = !file('#objectOriginal') && (!source?.source_url || data.white_uploaded) ? `请先上传原片，或直接上传已有${whiteLabel}。` : thresholdInvalid ? '阈值必须在 0.30–0.90 之间。' : '';
    $('#objectMosaic').disabled = locked || !!sourceProblem;
    $('#objectMosaic').dataset.disabledReason = locked ? '任务处理中，请等待完成。' : sourceProblem;
    $('#objectMosaicHint').textContent = sourceProblem || `重新打码免费复用原片；完成后需重新生成${whiteLabel}。`;
    $('#objectImportWhite').disabled = locked || !file('#objectWhiteUpload');
    let whiteProblem = !source?.has_mosaic ? '请先完成打码。' : state.dirty ? '原片或阈值已调整，请重新打码。' : !$('#objectMosaicReviewed').checked ? '请先预览并确认打码完整。' : '';
    $('#objectMakeWhite').disabled = locked || !!whiteProblem;
    $('#objectMakeWhite').dataset.disabledReason = locked ? '任务处理中，请等待完成。' : whiteProblem;
    $('#objectWhiteHint').textContent = whiteProblem || (scene ? '主要人物变白，背景变为纯绿；保持人物占位、动作、道具与镜头运动。' : '只将主要人物变白，保持目标物品、背景与镜头运动。');
    let problem = !state.supported ? '正在确认背景替换后台是否已更新，请稍候；若持续显示请重启项目服务。' : locked ? '任务处理中，请等待完成。' : !source ? (refs() ? `请先生成或上传${whiteLabel}。` : '请先从视频素材库选择原片。') : '';
    if (!problem && refs()) problem = state.dirty ? '原片或阈值已调整，请重新打码并生成白模。' : !source.has_white_model ? `请先生成或上传${whiteLabel}。` : !$('#objectWhiteReviewed').checked ? `请预览并确认${whiteLabel}。` : !actor() ? '请选择原片人物角色，或上传人物图片入库。' : '';
    if (!problem && !refs() && !data.video_asset) problem = '请选择审核通过的原片视频。';
    if (!problem && !refs() && state.videoPending) problem = '新原片尚未绑定，请预览后使用新原片，或取消更换。';
    if (!problem && scene && refs() && !file('#objectItem')) problem = '请上传新场景参考图，对应 @图片3。';
    if (!problem && !scene && $('#objectSpecial').checked && !file('#objectItem')) problem = '特殊物品需要上传物品参考图。';
    if (!problem) problem = window.DepthFlowInlineCast?.problem() || '';
    const prompt = $('#objectPrompt').value.trim().replace(/@\s*(图片|视频)\s*(\d+)/g, '@$1$2');
    const available = bindings().filter(x => x.ready).map(x => x.token);
    if (!problem && !prompt) problem = (scene ? '请说明需要更换的背景场景。' : '请说明原片中的哪个物品要换成什么。');
    if (!problem && (/@/.test(prompt.replace(/@(图片|视频)\d+/g, '')) || (prompt.match(/@(?:图片|视频)\d+/g) || []).some(token => !available.includes(token)))) problem = '提示词包含未就绪的素材引用，请重新选择 @ 标签。';
    $('#objectGenerateBtn').disabled = !!problem;
    $('#objectGenerateBtn').dataset.disabledReason = problem;
    $('#objectGenerateHint').textContent = problem || `已就绪：${available.join('、')}，生成时自动附加${editLabel}替换与运镜保持规则。`;
    $('#objectBindings').replaceChildren(...bindings().map(item => {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `${item.token} · ${item.label}${item.ready ? '' : '（未就绪）'}`;
      button.disabled = !item.ready; button.onclick = () => insert(item.token); return button;
    }));
    $('#objectItemBinding').textContent = `${(scene ? refs() : $('#objectSpecial').checked) ? '必需' : '可选'} · 绑定 ${refs() ? '@图片3' : '@图片1'}`;
    const uploadProblem = !state.supported ? '请先更新后台服务。' : state.uploadBusy ? '正在上传或审核，请等待状态更新。' : !$('#objectVideoGroup').value ? '请先选择上方的素材组。' : !file('#objectLibraryUpload') ? '请选择要上传的新原片视频。' : !$('#objectVideoName').value.trim() ? '请填写原片名称。' : '';
    $('#objectUploadVideo').disabled = !!uploadProblem;
    $('#objectUploadVideo').dataset.disabledReason = uploadProblem;
    $('#objectUploadVideoHint').textContent = uploadProblem || '已就绪：上传后刷新审核状态，再点击“使用这份已入库原片”。';
    $('#objectLibraryUpload').disabled = state.uploadBusy;
    $('#objectVideoName').disabled = state.uploadBusy;
    $('#objectReuseVideo').disabled = locked || !state.uploadAsset;
    $('#objectChooseLibrary').disabled = locked; $('#objectChooseUpload').disabled = locked;
    $('#objectCancelVideoChange').disabled = locked;
    show('#objectCancelVideoChange', !!data.video_asset && state.videoPending);
    $('#objectVideoChoiceHint').textContent = locked ? '当前步骤正在处理，完成后即可更换原片。' : state.videoChoice === 'upload' ? '选择素材组和本地视频，上传通过审核后使用。新原片确认前，当前原片仍保留。' : state.videoPending ? '请选择并预览另一份原片，再点击“使用此原片”。更换后会保留已填写的提示词和参考图。' : '先选择素材组，再选择组内视频。预览后点击“使用此原片”，绑定为 @视频1。';
    $('#objectVideoGroup').disabled = locked;
    $('#objectVideoAsset').disabled = locked || !$('#objectVideoGroup').value || $('#objectVideoAsset').options.length <= 1;
    $$('[data-object-workflow]').forEach(button => button.disabled = locked);
    $$('#objectVideoAssets button').forEach(button => {
      const bound = !state.videoPending && button.dataset.uri === data.video_asset;
      button.disabled = locked || button.dataset.status !== 'Active' || bound;
      button.textContent = button.dataset.status !== 'Active' ? '待审核通过后可用' : bound ? '当前已使用此原片' : '使用此原片';
    });
  }
  function render(job, restoring = false) {
    window.DepthFlowAudio?.render(job,{anchor:'#objectWhitePreview',busy:active(job),onRestore:id=>poll(id)});
    window.DepthFlowSegments?.render(job,{anchor:'#objectSourceTitle',busy:active(job),onSelect:id=>{
      $('#objectOriginal').value='';state.generation=null;state.dirty=false;clearTimeout(state.timer);window.DepthFlowInlineCast?.clear();poll(id);
    }});
    if (job.kind === `wardrobe_prepare_${mode}`) {
      window.DepthFlowInlineCast?.restore(job, restoring);
      state.source = job;
      const revision = `${job.id}:${job.white_model_revision || ''}`;
      if (state.revision !== revision) { $('#objectWhiteReviewed').checked = false; state.revision = revision; }
      if (restoring) {
        $('#objectThreshold').value = job.wardrobe_mosaic?.face_score_threshold ?? .55;
        $('#objectPrompt').value = meta().custom_prompt || '';
        $('#objectSpecial').checked = !!meta().special_object;
        $('#objectWhitePrompt').value = '';
      }
      media('#objectMosaicPreview', job.mosaic_url); download('#objectMosaicDownload', job.mosaic_url);
      media('#objectWhitePreview', job.white_model_url ? `${job.white_model_url}?v=${job.white_model_revision || job.id}` : ''); download('#objectWhiteDownload', job.white_model_url);
      if (!file('#objectOriginal')) media('#objectOriginalPreview', meta().white_uploaded ? '' : job.source_url);
      media('#objectSelectedPreview', $('#objectVideoAsset').value === meta().video_asset ? '' : meta().video_url);
      $('#objectSelectedVideo').textContent = meta().video_asset ? `已选择：${meta().video_name || meta().video_asset}` : '尚未选择原片视频。';
      show('#objectRecoverWhite', !!job.recovery_action);
    } else state.generation = job;
    $('#objectStage').textContent = `${job.stage || job.status} · ${job.progress || 0}%`;
    $('#objectLogs').textContent = [...(job.logs || []), job.error || ''].filter(Boolean).join('\n');
    if (job.output_url) { media('#objectOutput', job.output_url); download('#objectOutputDownload', job.output_url); }
    readiness();
  }
  async function poll(id, version = state.loadVersion) {
    clearTimeout(state.timer);
    try {
      const job = await api(`/api/jobs/${id}`);
      if (version !== state.loadVersion) return;
      if (job.kind === `wardrobe_generate_${mode}` && job.status === 'succeeded' && state.source?.has_output && state.source.white_model_revision !== job.white_model_revision) {
        render(state.source); return;
      }
      render(job);
      if (active(job)) state.timer = setTimeout(() => poll(id, version), 2500);
    } catch (error) { if (version !== state.loadVersion) return; notice(error.message, true); state.timer = setTimeout(() => poll(id, version), 6000); }
  }
  async function switchWorkflow(workflow) {
    if (busy()) return;
    clearTimeout(state.timer); state.loadVersion++; const version = state.loadVersion;
    window.DepthFlowInlineCast?.clear();
    state.workflow = workflow; state.source = null; state.generation = null; state.dirty = false; state.revision = ''; state.videoPending = false;
    setVideoChoice('library', false);
    $$('#objectVideoAssets video').forEach(video => video.pause()); $('#objectVideoAssets').replaceChildren();
    localStorage.setItem(`${storagePrefix}.workflow`, workflow);
    $$('[data-object-workflow]').forEach(button => button.classList.toggle('active', button.dataset.objectWorkflow === workflow));
    ['#objectReferenceSource', '#objectPrepare', '#objectActorReferences', '#objectWhiteReview'].forEach(id => show(id, refs()));
    show('#objectLibrarySource', !refs());
    $('#objectSourceTitle').textContent = refs() ? `上传原片或已有${whiteLabel}` : '上传原片至角色库，或选择已有视频';
    $('#objectReferences h2').textContent = refs() ? `人物、服装与${scene ? '场景' : '物品'}` : `${referenceLabel}（按需添加）`;
    $('#objectWorkflowNote').textContent = scene ? (refs() ? '原片全部人脸打码 → 绿底白模 → 添加原片人物、服装和新场景参考图 → 生成背景替换成片。已有绿底白模可直接上传。' : '原片以视频素材入库，审核通过后直接引用原片，用提示词说明新背景；也可添加场景图指定外观。人物和服装沿用原片。') : refs() ? '原片全部人脸打码 → 仅主角变白 → 添加原片人物和服装参考 → 用提示词替换物品。已有主角白模可直接上传。' : '原片以视频素材入库，通过审核后直接引用原片。用提示词指定物品，不经过打码和白模步骤。';
    ['#objectOriginal', '#objectWhiteUpload', '#objectClothing', '#objectItem'].forEach(id => $(id).value = '');
    ['#objectOriginalPreview', '#objectMosaicPreview', '#objectWhitePreview', '#objectOutput', '#objectClothingPreview', '#objectItemPreview'].forEach(id => media(id, ''));
    ['#objectMosaicDownload', '#objectWhiteDownload', '#objectOutputDownload'].forEach(id => show(id, false));
    $('#objectMosaicReviewed').checked = false; $('#objectWhiteReviewed').checked = false; $('#objectSpecial').checked = false; $('#objectPrompt').value = '';
    $('#objectStage').textContent = '等待准备素材'; $('#objectLogs').textContent = '此流程尚未准备素材。';
    readiness();
    try {
      const job = await api(`/api/wardrobe-object/latest?workflow=${workflow}`);
      if (version !== state.loadVersion) return;
      if (job.id) {
        render(job, true);
        const entries = Object.values(job.wardrobe_dynamic?.requests || {}).filter(entry => entry.action === 'generate');
        if (entries.length) await poll(entries[entries.length - 1].job_id, version);
        else if (active(job)) await poll(job.id, version);
      }
      if (!refs()) await refreshLibrary();
    } catch (error) { notice(error.message, true); }
  }
  async function perform(fn) {
    if (!state.supported) return notice('背景替换后台尚未更新，请重启项目服务后刷新。', true);
    if (busy()) return;
    state.working = true; readiness();
    try { await fn(); } catch (error) { notice(error.message, true); }
    finally { state.working = false; readiness(); }
  }
  let confirmResolve = null;
  function closeConfirm(value) { $('#paidConfirmModal').hidden = true; $('#paidConfirmModal').setAttribute('aria-hidden', 'true'); document.body.classList.remove('workspace-dialog-open'); const resolve = confirmResolve; confirmResolve = null; resolve?.(value); }
  function confirmPaid(title) {
    $('#paidConfirmTitle').textContent = title;
    const li = document.createElement('li'); li.textContent = '提交 1 次付费视频生成任务。取消不会提交。'; $('#paidConfirmLines').replaceChildren(li);
    $('#paidConfirmModal').hidden = false; $('#paidConfirmModal').setAttribute('aria-hidden', 'false'); document.body.classList.add('workspace-dialog-open');
    $('#paidConfirmSubmit').focus(); return new Promise(resolve => confirmResolve = resolve);
  }
  async function paid(action, endpoint, values) {
    const storage = `${storagePrefix}.pending.${state.source.id}.${action}`;
    if (!await confirmPaid(action === 'white' ? `生成${whiteLabel} · Seedance 2.0` : `生成${editLabel}替换成片 · Seedance 2.5`)) return;
    let key = localStorage.getItem(storage);
    if (!key) { key = crypto.randomUUID(); localStorage.setItem(storage, key); }
    try {
      const job = await post(endpoint, {...values, source_job_id: state.source.id, mode, request_id: key, paid_confirmed: 'true'});
      localStorage.removeItem(storage); render(job); await poll(job.id);
    } catch (error) { if (error.status && error.status < 500) localStorage.removeItem(storage); throw error; }
  }
  async function refreshLibrary() {
    const version = ++state.libraryVersion, loadVersion = state.loadVersion;
    try {
      const result = await api('/api/wardrobe-object/video-library');
      if (version !== state.libraryVersion || loadVersion !== state.loadVersion) return;
      state.library = result.assets || [];
      const select = $('#objectVideoGroup'), previous = select.value;
      select.replaceChildren(new Option('请选择素材组', ''), ...(result.groups || []).map(group => new Option(`${group.name} · ${group.group_type === 'LivenessFace' ? '真人授权组' : '虚拟人像组'}`, group.id)));
      if ([...select.options].some(option => option.value === previous)) select.value = previous;
      if (!select.value && meta().video_asset && !state.videoPending) select.value = state.library.find(asset => asset.uri === meta().video_asset)?.group_id || meta().video_group_id || '';
      $('#objectLibraryMessage').textContent = result.message || `这里选择原片，只显示所选组内的视频素材；图片请在下方${scene ? '人物或场景' : '人物或物品'}参考区选择。`;
      groupAssets($('#objectVideoAsset').value || (!state.videoPending ? meta().video_asset : '') || '');
    } catch (error) { $('#objectLibraryMessage').textContent = error.message; }
  }
  function groupAssets(preferred = '') {
    const group = $('#objectVideoGroup').value, items = state.library.filter(asset => asset.group_id === group);
    const select = $('#objectVideoAsset');
    select.replaceChildren(new Option(!group ? '请先选择素材组' : items.length ? '请选择组内视频' : '此素材组暂无视频', ''), ...items.map(asset => new Option(`${asset.name} · ${asset.status === 'Active' ? '可使用' : asset.status === 'Processing' ? '审核中' : '审核未通过'}`, asset.uri)));
    select.disabled = !group || !items.length;
    if (items.some(asset => asset.uri === preferred)) select.value = preferred;
    renderVideoCandidate();
  }
  function renderVideoCandidate() {
    const selected = state.library.find(asset => asset.uri === $('#objectVideoAsset').value && asset.group_id === $('#objectVideoGroup').value);
    const current = $('#objectVideoAssets article');
    if (selected && current?.dataset.assetUri === selected.uri && current.dataset.assetUrl === selected.url && current.dataset.assetStatus === selected.status && !current.dataset.previewFailed) {
      readiness(); return; // Refreshing status must not restart a playing preview.
    }
    $$('#objectVideoAssets video').forEach(video => video.pause());
    $('#objectVideoAssets').replaceChildren(...(selected ? [selected] : []).map(asset => {
        const card = document.createElement('article'), title = document.createElement('b'), status = document.createElement('small');
        card.dataset.assetUri = asset.uri; card.dataset.assetUrl = asset.url || ''; card.dataset.assetStatus = asset.status;
        title.textContent = `原片预览 · ${asset.name}`; status.textContent = '正在加载视频画面…'; status.setAttribute('role', 'status'); card.append(title, status);
        if (/^https?:\/\//.test(asset.url)) {
          const video = document.createElement('video'); video.src = asset.url; video.controls = true;
          video.preload = 'auto'; video.muted = true; video.autoplay = true; video.loop = true; video.playsInline = true;
          video.setAttribute('aria-label', `${asset.name} 视频预览`);
          video.addEventListener('loadeddata', () => { status.textContent = '画面已加载 · 预览默认静音，可用播放器开启声音或全屏查看'; });
          video.addEventListener('error', () => { card.dataset.previewFailed = 'true'; status.textContent = '视频预览加载失败，请点击下方“刷新视频素材与审核状态”重新获取播放地址。'; });
          card.append(video);
        } else status.textContent = '素材库暂未提供播放地址，请刷新素材状态后重试。';
        const button = document.createElement('button'); button.type = 'button'; button.dataset.status = asset.status; button.dataset.uri = asset.uri;
        button.onclick = () => perform(() => bindVideo(asset.uri));
        card.append(button); return card;
      }));
    media('#objectSelectedPreview', '');
    readiness();
  }
  function clearVideoSelection() {
    if (!refs()) {
      clearTimeout(state.timer); state.loadVersion++;
      state.videoPending = true;
      $('#objectSelectedVideo').textContent = meta().video_asset ? `当前原片：${meta().video_name || meta().video_asset}（确认新原片后替换）` : '尚未绑定原片，请预览后点击“使用此原片”。';
    }
  }
  function setVideoChoice(choice, invalidate = true) {
    state.videoChoice = choice;
    if (invalidate && !busy()) clearVideoSelection();
    show('#objectNewVideoUpload', choice === 'upload');
    show('#objectVideoAssetField', choice === 'library');
    show('#objectVideoAssets', choice === 'library');
    $('#objectChooseLibrary').setAttribute('aria-pressed', String(choice === 'library'));
    $('#objectChooseUpload').setAttribute('aria-pressed', String(choice === 'upload'));
    if (invalidate) refreshLibrary();
    readiness();
  }
  function clearUploadReceipt() {
    if (state.uploadBusy) return;
    clearTimeout(state.uploadTimer); state.uploadVersion++; state.uploadAsset = '';
    show('#objectReuseVideo', false); $('#objectUploadStatus').textContent = '';
  }
  async function bindVideo(uri) {
    const job = await post('/api/wardrobe-object/select-video', {video_asset: uri});
    clearTimeout(state.timer); state.loadVersion++;
    state.videoPending = false; state.generation = null;
    media('#objectOutput', ''); show('#objectOutputDownload', false);
    render(job);
    $('#objectVideoGroup').value = state.library.find(asset => asset.uri === uri)?.group_id || meta().video_group_id || '';
    setVideoChoice('library', false); groupAssets(uri);
    notice(`已使用 ${meta().video_name || '所选原片'}，@视频1 已更新。`);
  }
  async function uploadStatus(key) {
    clearTimeout(state.uploadTimer);
    const version = state.uploadVersion;
    try {
      const record = await api(`/api/wardrobe-object/upload-status/${encodeURIComponent(key)}`);
      if (version !== state.uploadVersion) return;
      const status = record.asset_status || record.status;
      const labels = {uploading: '正在核对已有原片；没有相同视频时再上传', submitting: '正在提交角色库', submitted: '已提交，等待审核', Processing: '审核处理中', Active: '已通过审核，可以选择使用', Failed: '审核失败，请在角色库查看原因', failed: '新增入库失败', quota_full: '素材额度不足', uncertain: '入库响应未确认，请先刷新素材库核对，避免重复入库'};
      $('#objectUploadStatus').textContent = record.deduplicated && status === 'Active'
        ? `这份原片已经入库（${record.existing_name || record.name}），内容完全一致，已复用原素材，不再占用新增额度。`
        : (labels[status] || status) + (record.error ? `：${record.error}` : '');
      state.uploadAsset = status === 'Active' ? record.uri : '';
      show('#objectReuseVideo', !!state.uploadAsset);
      if (['Active', 'Failed', 'failed', 'quota_full', 'uncertain'].includes(status)) { state.uploadBusy = false; localStorage.removeItem(`${storagePrefix}.videoUpload`); await refreshLibrary(); }
      else { state.uploadBusy = true; state.uploadTimer = setTimeout(() => uploadStatus(key), 7000); }
      readiness();
    } catch (error) { if (version !== state.uploadVersion) return; $('#objectUploadStatus').textContent = error.message; state.uploadTimer = setTimeout(() => uploadStatus(key), 8000); }
  }
  try {
    const response = await fetch(`/static/wardrobe_${namespace}.html?v=20260914-6`);
    if (!response.ok) throw new Error(`${editLabel}替换页面加载失败，请刷新。`);
    const container = document.createElement('div'); container.id = 'objectWorkspace'; container.innerHTML = await response.text();
    const main = $('#mode').parentElement;
    ['#source', '#prepare', '#references', '#generate', '#result'].forEach(id => show(id, false));
    main.append(container); $('#objectCharacterHost').append($('#characters'));
    $('#characterTitle').textContent = '选择原片人物'; $('#characterNote').textContent = '人物参考绑定 @图片1，可上传新人物图入库或选择已有角色。';
    $('#stepNav').replaceChildren(...[['mode', '选择功能'], ['objectWorkflow', '选择素材流程'], ['objectSource', '准备视频'], ['objectReferences', '参考素材'], ['objectGenerate', '设置替换'], ['objectResults', '预览下载']].map(([id, label], index) => {
      const a = document.createElement('a'); a.href = '#' + id; const i = document.createElement('i'); i.textContent = String(index + 1).padStart(2, '0'); const span = document.createElement('span'); span.textContent = label; a.append(i, span); return a;
    }));
    localStorage.setItem('depthflow.wardrobe.mode', mode);
    const flow = $$('.hero-flow b');
    ['选择素材流程', `${whiteLabel} / 原片视频`, `描述替换${editLabel}`, 'Seedance 2.5'].forEach((label, index) => { if (flow[index]) flow[index].textContent = label; });
    $$('[data-mode]').forEach(button => { button.classList.toggle('active', button.dataset.mode === mode); button.onclick = () => { if (busy() || confirmResolve) return notice('请等待当前操作完成。'); localStorage.setItem('depthflow.wardrobe.mode', button.dataset.mode); location.hash = button.dataset.mode; location.reload(); }; });
    $$('[data-object-workflow]').forEach(button => button.onclick = () => switchWorkflow(button.dataset.objectWorkflow));
    $('#wardrobeForm').addEventListener('submit', event => event.preventDefault());
    $('#paidConfirmForm').onsubmit = event => { event.preventDefault(); closeConfirm(true); };
    $('#paidConfirmCancel').onclick = $('#paidConfirmBackdrop').onclick = () => closeConfirm(false);
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && confirmResolve) closeConfirm(false); });
    $('#objectOriginal').onchange = () => { previewFile('#objectOriginal', '#objectOriginalPreview'); state.dirty = true; $('#objectMosaicReviewed').checked = false; $('#objectWhiteReviewed').checked = false; readiness(); };
    $('#objectThreshold').oninput = () => { if (state.source?.has_mosaic) state.dirty = Number($('#objectThreshold').value) !== state.source.wardrobe_mosaic?.face_score_threshold || !!file('#objectOriginal'); $('#objectMosaicReviewed').checked = false; readiness(); };
    $('#objectMosaic').onclick = () => perform(async () => { const job = await post('/api/wardrobe-swap/mosaic', {mode, reference_video: file('#objectOriginal'), source_job_id: state.source?.id, face_score_threshold: $('#objectThreshold').value}); state.dirty = false; state.generation = null; $('#objectOriginal').value = ''; $('#objectMosaicReviewed').checked = false; media('#objectOutput', ''); show('#objectOutputDownload', false); render(job); await poll(job.id); });
    $('#objectImportWhite').onclick = () => perform(async () => { const job = await post('/api/wardrobe-object/import-white', {white_video: file('#objectWhiteUpload')}); state.dirty = false; state.generation = null; $('#objectOriginal').value = ''; media('#objectOutput', ''); show('#objectOutputDownload', false); render(job); });
    $('#objectMakeWhite').onclick = () => perform(() => paid('white', '/api/wardrobe-swap/white-model', {mosaic_reviewed: $('#objectMosaicReviewed').checked, white_prompt: $('#objectWhitePrompt').value}));
    $('#objectRecoverWhite').onclick = () => perform(async () => { const job = await post('/api/wardrobe-swap/recover-white-model', {source_job_id: state.source.id}); render(job); await poll(job.id); });
    $('#objectWhitePrompt').addEventListener('input',readiness);
    document.addEventListener('inline-cast-change',readiness);
    $('#objectGenerateBtn').onclick = () => perform(() => paid('generate', '/api/wardrobe-swap/generate', {
      white_reviewed: $('#objectWhiteReviewed').checked, person_asset: refs() ? actor() : undefined,
      clothing_image: refs() ? file('#objectClothing') : undefined, replacement_image: file('#objectItem'),
      special_object: $('#objectSpecial').checked, prompt: $('#objectPrompt').value, resolution: $('#objectResolution').value, generate_audio: $('#objectAudio').checked,
    }));
    $('#objectRefreshVideos').onclick = refreshLibrary;
    $('#objectChooseLibrary').onclick = () => { if (!busy()) setVideoChoice('library'); };
    $('#objectChooseUpload').onclick = () => { if (!busy()) { clearUploadReceipt(); setVideoChoice('upload'); } };
    $('#objectCancelVideoChange').onclick = () => {
      if (busy()) return;
      state.videoPending = false;
      $('#objectVideoGroup').value = state.library.find(asset => asset.uri === meta().video_asset)?.group_id || meta().video_group_id || '';
      $('#objectSelectedVideo').textContent = `当前原片：${meta().video_name || meta().video_asset}`;
      setVideoChoice('library', false); groupAssets(meta().video_asset);
    };
    $('#objectVideoGroup').addEventListener('change', () => { clearVideoSelection(); groupAssets(); });
    $('#objectVideoAsset').addEventListener('change', () => { if ($('#objectVideoAsset').value !== meta().video_asset) clearVideoSelection(); renderVideoCandidate(); });
    $('#objectReuseVideo').onclick = () => perform(() => bindVideo(state.uploadAsset));
    $('#objectUploadVideo').onclick = async () => {
      if (state.uploadBusy) return;
      clearUploadReceipt(); state.uploadBusy = true; readiness(); const key = crypto.randomUUID(); localStorage.setItem(`${storagePrefix}.videoUpload`, key);
      try { await post('/api/wardrobe-object/upload-video', {request_id: key, group_id: $('#objectVideoGroup').value, name: $('#objectVideoName').value, reference_video: file('#objectLibraryUpload')}); await uploadStatus(key); }
      catch (error) { notice(error.message, true); if (error.status && error.status < 500) { state.uploadBusy = false; localStorage.removeItem(`${storagePrefix}.videoUpload`); readiness(); } else uploadStatus(key); }
    };
    $('#objectClearItem').onclick = () => { $('#objectItem').value = ''; previewFile('#objectItem', '#objectItemPreview'); readiness(); };
    [['#objectClothing', '#objectClothingPreview'], ['#objectItem', '#objectItemPreview']].forEach(([input, target]) => $(input).onchange = () => { previewFile(input, target); readiness(); });
    $('#objectLibraryUpload').onchange = () => {
      clearUploadReceipt();
      clearVideoSelection();
      const selected = file('#objectLibraryUpload');
      if (selected && (!$('#objectVideoName').value.trim() || $('#objectVideoName').value === state.autoVideoName)) {
        state.autoVideoName = selected.name.replace(/\.[^.]+$/, '').slice(0, 64) || '新原片';
        $('#objectVideoName').value = state.autoVideoName;
      }
      previewFile('#objectLibraryUpload', '#objectUploadPreview'); readiness();
    };
    ['#objectSpecial', '#objectMosaicReviewed', '#objectWhiteReviewed', '#objectWhiteUpload', '#objectVideoGroup', '#objectVideoName'].forEach(id => { $(id).addEventListener('change', readiness); $(id).addEventListener('input', readiness); });
    document.addEventListener('change', event => { if (event.target.id === 'personAsset') readiness(); });
    document.addEventListener('input', event => { if (event.target.id === 'personAsset') readiness(); });
    $('#objectPrompt').oninput = () => {
      readiness(); const input = $('#objectPrompt'), match = input.value.slice(0, input.selectionStart).match(/@[^\s@，。；]*$/);
      const items = match ? bindings().filter(item => item.ready) : [];
      $('#objectMentions').replaceChildren(...items.map(item => { const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role', 'option'); button.textContent = `${item.token} · ${item.label}`; button.onclick = () => insert(item.token); return button; })); show('#objectMentions', !!items.length);
    };
    $('#objectPrompt').onkeydown = event => { if (event.key === 'Escape') show('#objectMentions', false); if (event.key === 'ArrowDown' && !$('#objectMentions').classList.contains('hidden')) { event.preventDefault(); $('#objectMentions button')?.focus(); } };
    readiness();
    try {
      const config = await api('/api/config'); $('#arkBadge').textContent = config.ark_ready ? 'API 已配置' : 'API 未配置'; $('#objectWhiteBase').textContent = (scene ? config.wardrobe_dynamic_modes?.dynamic_scene?.white_prompt : config.wardrobe_dynamic_white_prompt) || '';
      state.supported = !scene || config.wardrobe_dynamic_modes?.dynamic_scene?.workflow_version === 2;
      if (!state.supported) throw new Error('背景替换后台尚未更新，请使用项目启动入口重启服务。');
    } catch (error) { $('#arkBadge').textContent = '连接待重试'; notice(error.message, true); }
    await switchWorkflow(state.workflow);
    const pending = localStorage.getItem(`${storagePrefix}.videoUpload`);
    if (pending) uploadStatus(pending);
    else {
      try { const version = state.uploadVersion; const latestUpload = await api('/api/wardrobe-object/latest-upload'); if (version === state.uploadVersion && !file('#objectLibraryUpload') && latestUpload.request_id) uploadStatus(latestUpload.request_id); }
      catch (error) { notice(error.message, true); }
    }
    window.addEventListener('beforeunload', () => { clearTimeout(state.timer); clearTimeout(state.uploadTimer); mediaUrls.forEach(url => URL.revokeObjectURL(url)); });
  } catch (error) { notice(error.message, true); }
};
