(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const key = "depthflow.motionTransfer";
  const state = {job:null, busy:false, sourceDirty:false, dirty:false, mode:"cast", roles:[], library:{assets:[],groups:[]}, sourceMode:"original", timer:0, blobs:new Map(), saved:{}, clearLast:false, pending:null, menu:[], menuIndex:0};
  function store(name,value) { try { value ? localStorage.setItem(name,value) : localStorage.removeItem(name); } catch (_) {} }
  function read(name) { try { return localStorage.getItem(name) || ""; } catch (_) { return ""; } }
  const uploads = {busy:false, loading:false, timer:0, entries:[], context:read(key)||`new-${crypto.randomUUID()}`, error:""};
  try { const saved=JSON.parse(read(`${key}.uploads`)||"[]");if(Array.isArray(saved))uploads.entries=saved.filter(u=>u&&typeof u.uri==="string"&&typeof u.context==="string").slice(-50); } catch (_) {}
  try { state.pending = JSON.parse(read(`${key}.pending`) || "null"); } catch (_) {}
  const running = () => ["queued","running","submitted"].includes(state.job?.status);
  const locked = () => state.busy || uploads.busy || running();
  function notice(message,error=false) { $("notice").textContent=message; $("notice").hidden=!message; $("notice").classList.toggle("error",error); }
  async function api(path,options={}) { const r=await fetch(path,{cache:"no-store",...options}); const p=await r.json().catch(()=>({})); if(!r.ok){const e=new Error(p.error || `请求失败（HTTP ${r.status}）`);e.status=r.status;throw e;} return p; }
  function blob(id,file) { if(state.blobs.has(id))URL.revokeObjectURL(state.blobs.get(id));const url=URL.createObjectURL(file);state.blobs.set(id,url);return url; }
  function setMedia(id,url) { const n=$(id); if((n.getAttribute("src")||"")!==(url||"")){if(url)n.src=url;else n.removeAttribute("src");}n.hidden=!url; }
  function imageURL(field) { const file=$(`${field}File`).files[0];return file ? state.blobs.get(field)||"" : field==="last"&&state.clearLast ? "" : state.saved[`${field}_url`]||""; }
  function roleURL(role) { const a=state.library.assets.find(a=>a.uri===role.uri);return a?.preview_url||a?.url||""; }
  function openImage(url,title) { if(!url)return notice("图片暂时不可用，请刷新角色库后重试。",true);$("imageTitle").textContent=title;$("imageError").hidden=true;setMedia("fullImage",url);$("fullImage").alt=title;$("imageDialog").showModal(); }
  function markDirty() { state.dirty=true;$("draftState").textContent="有未保存的修改";updateControls(); }
  function setMode(mode,dirty=true) { state.mode=mode;document.querySelectorAll("[data-mode]").forEach(b=>{b.classList.toggle("selected",b.dataset.mode===mode);b.setAttribute("aria-pressed",String(b.dataset.mode===mode));});$("castPane").hidden=mode!=="cast";$("framesPane").hidden=mode!=="frames";renderReferences();if(dirty)markDirty(); }
  function setSource(mode) { state.sourceMode=mode;document.querySelectorAll("[data-source]").forEach(b=>{b.classList.toggle("selected",b.dataset.source===mode);b.setAttribute("aria-pressed",String(b.dataset.source===mode));});$("originalSource").hidden=mode!=="original";$("whiteSource").hidden=mode!=="white"; }
  function materials() {
    const list=state.mode==="cast" ? [{token:"@图片1",name:"目标场景",url:imageURL("scene"),available:!!imageURL("scene")},...state.roles.map((r,i)=>({token:`@图片${i+2}`,name:r.name,url:roleURL(r),available:true}))] : [{token:"@图片1",name:"首帧参考",url:imageURL("first"),available:!!imageURL("first")},...(imageURL("last")?[{token:"@图片2",name:"尾帧参考",url:imageURL("last"),available:true}]:[])];
    list.push({token:"@视频1",name:"动作白膜",url:"",available:!!state.job?.white_url&&!state.sourceDirty});
    return list;
  }
  function renderReferences() { $("referenceChips").innerHTML=materials().map(m=>`<button type="button" data-token="${m.token}" ${!m.available||locked()?"disabled":""}>${m.url?`<img src="${esc(m.url)}" alt="">`:""}<span>${m.token} · ${esc(m.name)}</span></button>`).join("");$("promptCount").textContent=`${$("prompt").value.length} / 1600`; }
  function renderImages() { for(const field of ["scene","first","last"]){const url=imageURL(field);setMedia(`${field}Preview`,url);$(`${field}PreviewButton`).hidden=!url;}renderReferences(); }
  function renderRoles() {
    $("rolesEmpty").hidden=state.roles.length>0;
    $("selectedRoles").innerHTML=state.roles.map((r,i)=>`<article class="selected-role"><button type="button" data-preview-role="${i}" aria-label="放大 ${esc(r.name)}">${roleURL(r)?`<img src="${esc(roleURL(r))}" alt="${esc(r.name)}">`:"图片暂不可用"}</button><div><b>@图片${i+2} · ${esc(r.name)}</b><input data-role-label="${i}" maxlength="100" value="${esc(r.role)}" aria-label="${esc(r.name)}对应的原片人物" placeholder="例如：原片左侧人物" ${locked()?"disabled":""}></div><button type="button" data-remove-role="${i}" aria-label="移除 ${esc(r.name)}" ${locked()?"disabled":""}>移除</button></article>`).join("");renderReferences();
  }
  function defaultPrompt() {
    if(state.mode==="frames")return `以@图片1为首帧画面参考，人物严格复刻@视频1的动作顺序、身体姿态和节奏，保持首帧中的身份、服装和场景稳定。${imageURL("last")?"最后自然过渡到@图片2的尾帧画面。":""}动作自然连续，输出正常彩色视频。`;
    return `参考@视频1的完整动作，在@图片1的场景中完成表演。${state.roles.map((r,i)=>`@图片${i+2}中的人物对应${r.role||`原片人物${i+1}`}。`).join("")}严格保留动作顺序、节奏、互动和移动路径，人物身份与服装全程稳定，画面自然真实。`;
  }
  function draftError() {
    if(!state.job?.white_url || state.sourceDirty)return "请先生成或导入本次要使用的白膜。";
    if(state.mode==="cast"&&!imageURL("scene"))return "请上传目标场景图。";
    if(state.mode==="cast"&&!state.roles.length)return "请上传新人物并等待入库审核通过，或从角色库选择至少一位人物。";
    if(state.mode==="frames"&&!imageURL("first"))return "请上传首帧参考图。";
    const prompt=$("prompt").value.trim();if(!prompt)return "请填写动作迁移提示词。";
    if(prompt.includes("【已移除"))return "提示词中有已移除的素材，请修正后继续。";
    const allowed=new Set(materials().filter(m=>m.available).map(m=>m.token));
    for(const match of prompt.matchAll(/@(图片|视频)\s*(\d+)/g)){if(!allowed.has(`@${match[1]}${Number(match[2])}`))return `${match[0]}没有对应素材，请修改引用。`;}
    if(!$("whiteReviewed").checked)return "请预览白膜并勾选确认。";
    if(state.job?.needs_query)return "请先查询上次已提交任务。";
    if(state.pending)return "上次提交响应尚未确认，请先刷新任务状态。";
    return "";
  }
  function updateControls() {
    const busy=locked();
    for(const id of ["prepare","studio"])$(id).querySelectorAll("input,select,textarea").forEach(n=>n.disabled=busy);
    document.querySelectorAll("[data-mode],[data-source],[data-remove-role]").forEach(n=>n.disabled=busy);
    for(const id of ["newProject","history","defaultPrompt","clearLast"])$(id).disabled=busy;
    $("addRole").disabled=busy||state.roles.length>=8;
    $("uploadRole").disabled=busy||state.roles.length>=8;
    updateUploadControls();
    $("saveDraft").disabled=busy||!state.job||state.sourceDirty;
    const prep=busy?"当前项目正在处理。":!$("sourceFile").files.length?"请选择原片视频。":"本步在本地处理，免费。";
    $("prepareBtn").disabled=busy||!$("sourceFile").files.length;$("prepareReason").textContent=prep;
    $("importBtn").disabled=busy||!$("whiteFile").files.length;$("importReason").textContent=busy?"当前项目正在处理。":"直接导入白膜，不调用生成模型。";
    $("mosaicReviewed").disabled=busy||!state.job?.mosaic_url||state.sourceDirty;
    $("whiteReviewed").disabled=busy||!state.job?.white_url||state.sourceDirty;
    const white=busy?"当前项目正在处理。":state.sourceDirty?"原片已改变，请重新打码。":state.job?.white_url?"白膜已就绪，可进入视频制作。":!state.job?.mosaic_url?"请先完成本地打码。":!$("mosaicReviewed").checked?"请先预览打码视频并勾选确认。":state.job?.needs_query||state.pending?"请先查询或刷新上次任务。":"";
    $("whiteBtn").disabled=!!white;$("whiteReason").textContent=white||"提交前会再次确认模型与调用次数。";
    const reason=busy?"当前项目正在处理。":draftError();$("generateBtn").disabled=!!reason;$("generateReason").textContent=reason||"素材已就绪。提交前确认 1 次 Seedance 2.5 视频任务。";
    $("recoverBtn").hidden=!state.job?.needs_query;$("recoverBtn").disabled=busy;
    $("promptCount").textContent=`${$("prompt").value.length} / 1600`;
  }
  function restoreDraft(draft) {
    state.saved=draft;state.roles=(draft.roles||[]).map(r=>({...r}));state.clearLast=false;
    for(const field of ["scene","first","last"]){$(`${field}File`).value="";if(state.blobs.has(field)){URL.revokeObjectURL(state.blobs.get(field));state.blobs.delete(field);}}
    $("prompt").value=draft.prompt||"";$("resolution").value=draft.resolution||"720p";$("generateAudio").checked=!!draft.generate_audio;
    setMode(draft.mode||"cast",false);renderImages();renderRoles();state.dirty=false;$("draftState").textContent="制作草稿已保存";
  }
  function renderJob(job,restore=false) {
    if(!job.id)return;
    const different=state.job?.id!==job.id;state.job=job;store(key,job.id);uploads.context=job.id;
    if(state.pending?.project===job.id && ((job.request_ids||[]).includes(state.pending.id)||(!["queued","running","submitted"].includes(job.status)&&!job.needs_query))){state.pending=null;store(`${key}.pending`,"");}
    if(different||restore){state.sourceDirty=false;restoreDraft(job.draft);$("mosaicReviewed").checked=false;$("whiteReviewed").checked=false;setSource(job.source_origin==="white"?"white":"original");if(different)$("outputs").replaceChildren();}
    for(const [field,url] of [["source",job.source_url],["mosaic",job.mosaic_url],["white",job.white_url]]){if(field!=="source"||!state.sourceDirty)setMedia(`${field}Preview`,url);$(`${field}Empty`).hidden=!!(field==="source"&&state.sourceDirty?$("sourcePreview").getAttribute("src"):url);if(field!=="source"){$(`${field}Download`).hidden=!url;if(url)$(`${field}Download`).href=`${url}?download=1`;}}
    $("jobStage").textContent=job.stage;$("jobId").textContent=`项目 ${job.id}`;$("jobProgress").value=job.progress||0;$("jobError").textContent=job.error||"";$("jobError").hidden=!job.error;$("logs").textContent=(job.logs||[]).join("\n")||"暂无处理日志";
    for(const [i,output] of job.outputs.entries()){
      let card=$(`output-${output.id}`);if(!card){card=document.createElement("article");card.id=`output-${output.id}`;card.className="output-card";card.innerHTML=`<header><b>成片 ${String(i+1).padStart(2,"0")}</b><small></small></header><p></p><video id="video-${output.id}" controls playsinline preload="metadata" hidden></video><a hidden>下载成片 ↓</a><details><summary>本次提示词</summary><p class="used-prompt"></p></details>`;$("outputs").append(card);}
      card.querySelector("header small").textContent=({succeeded:"已完成",running:"生成中",queued:"等待提交",failed:"未完成"})[output.status]||output.status;card.querySelector(":scope > p").textContent=output.error||output.stage;card.querySelector(".used-prompt").textContent=output.prompt;setMedia(`video-${output.id}`,output.url);const a=card.querySelector("a");a.hidden=!output.url;if(output.url)a.href=`${output.url}?download=1`;
    }
    $("outputsEmpty").hidden=!!job.outputs.length;syncUploads();renderReferences();updateControls();clearTimeout(state.timer);if(running())state.timer=setTimeout(()=>refresh(false),2500);
  }
  async function history() {try{const p=await api("/api/motion-transfer/projects");$("history").innerHTML='<option value="">选择已保存项目</option>'+p.projects.map(p=>`<option value="${p.id}">${esc(p.created_at)} · ${esc(p.name)}</option>`).join("");$("history").value=state.job?.id||"";}catch(e){notice(e.message,true);} }
  async function refresh(restore=false) { try{const id=state.job?.id||read(key);const p=await api(id?`/api/motion-transfer/projects/${id}`:"/api/motion-transfer/latest");if(p.id)renderJob(p,restore);}catch(e){notice(`读取状态失败：${e.message}`,true);if(running()){clearTimeout(state.timer);state.timer=setTimeout(()=>refresh(false),5000);}} }
  function form(draft=false) { const f=new FormData();if(state.job)f.append("project_id",state.job.id);if(draft){f.append("mode",state.mode);f.append("roles",JSON.stringify(state.roles));f.append("prompt",$("prompt").value);f.append("resolution",$("resolution").value);f.append("generate_audio",String($("generateAudio").checked));for(const field of ["scene","first","last"]){if($(`${field}File`).files[0])f.append(field,$(`${field}File`).files[0]);}if(state.clearLast)f.append("clear_last","true");}return f; }
  let paidResolve=null;
  function confirmPaid(title,description) {$("paidTitle").textContent=title;$("paidDescription").textContent=description;$("paidDialog").showModal();$("paidCancel").focus();return new Promise(resolve=>paidResolve=resolve);}
  function closePaid(yes) {$("paidDialog").close();paidResolve?.(yes);paidResolve=null;}
  async function submit(action,data,{paid=null,restore=false}={}) {
    if(locked())return;state.busy=true;updateControls();
    try{
      if(paid&&!await confirmPaid(...paid))return;
      if(paid){state.pending={project:state.job.id,action,id:crypto.randomUUID()};store(`${key}.pending`,JSON.stringify(state.pending));data.append("request_id",state.pending.id);data.append("paid_confirmed","true");}
      const p=await api(`/api/motion-transfer/${action}`,{method:"POST",body:data});
      if(["prepare","import-white"].includes(action)&&p.id&&p.id!==state.job?.id){for(const u of uploads.entries)if(u.context===uploads.context)u.context=p.id;saveUploads();}
      if(paid){state.pending=null;store(`${key}.pending`,"");}
      renderJob(p,restore||action==="generate");notice(action==="draft"?"制作草稿已保存。":"任务已接收，可查看处理进度。");history();if(action==="generate"||action==="white-model")$("results").scrollIntoView({behavior:"smooth"});
    }catch(e){if(paid&&e.status&&e.status<500){state.pending=null;store(`${key}.pending`,"");}notice(e.message+(state.pending?" 请先刷新任务状态确认提交结果，避免重复生成。":""),true);}
    finally{state.busy=false;syncUploads();updateControls();renderReferences();}
  }
  async function loadLibrary() {
    if(uploads.loading)return;
    uploads.loading=true;$("refreshRoles").disabled=true;$("refreshUploads").disabled=true;$("libraryState").textContent="正在读取角色库图片…";
    try {
      state.library=await api("/api/character-library");uploads.error="";
      const selected=$("roleGroup").value;
      $("roleGroup").innerHTML='<option value="">全部人像组</option>'+(state.library.groups||[]).map(g=>`<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("");$("roleGroup").value=selected;
      renderUploadGroups();syncUploads();renderLibrary();renderRoles();
    } catch(e) {uploads.error=`角色库读取失败：${e.message}`;$("libraryState").textContent=uploads.error;renderUploads();}
    finally {uploads.loading=false;$("refreshRoles").disabled=false;$("refreshUploads").disabled=false;updateUploadControls();scheduleUploadReview();}
  }
  function saveUploads() {uploads.entries=uploads.entries.slice(-50);store(`${key}.uploads`,JSON.stringify(uploads.entries));}
  function renderUploadGroups(preferred=$("roleUploadGroup").value) {
    const groups=(state.library.groups||[]).filter(g=>g.group_type==="AIGC");
    $("roleUploadGroup").innerHTML='<option value="">请选择人像组</option>'+groups.map(g=>`<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("");
    if(groups.some(g=>g.id===preferred))$("roleUploadGroup").value=preferred;
    else if(groups.length===1)$("roleUploadGroup").value=groups[0].id;
    $("roleGroupDetails").open=!groups.length;
  }
  function uploadFileError(file) {
    if(!file)return "请选择人物图片，可在提交前放大预览。";
    if(!/\.(jpe?g|png|webp)$/i.test(file.name)||(file.type&&!['image/jpeg','image/png','image/webp'].includes(file.type)))return "人物图片只支持 JPG、PNG 或 WEBP。";
    if(!file.size)return "图片文件为空，请重新选择。";
    if(file.size>30*1024*1024)return "人物图片不能超过 30 MB。";
    return "";
  }
  function uploadError() {
    if(locked())return uploads.busy?"正在提交，请稍候…":"当前项目正在处理，请完成后上传。";
    if(!state.library.upload_ready)return uploads.error||"正在读取角色库配置；若尚未配置，请先完成角色库配置。";
    if(state.roles.length>=8)return "本次最多使用 8 位人物，请先移除一位再上传。";
    return uploadFileError($("roleUploadFile").files[0])||(!$("roleUploadName").value.trim()?"请填写人物名称。":"")||(!$("roleUploadGroup").value?"请选择人像组，或创建一个新的人像组。":"")||(!$("roleUploadConsent").checked?"请确认人物素材授权。":"");
  }
  function updateUploadControls() {
    $("roleUploadDialog").querySelectorAll("input,select").forEach(n=>n.disabled=locked());
    $("closeRoleUpload").disabled=uploads.busy;
    $("createRoleGroup").disabled=locked()||uploads.loading||!state.library.configured||!$("roleNewGroupName").value.trim();
    const reason=uploadError();$("submitRoleUpload").disabled=!!reason;$("roleUploadReason").textContent=reason||"入库审核通过后，自动加入本次制作的人物列表。";
    $("submitRoleUpload").textContent=uploads.busy?"正在提交…":"上传到角色库并用于制作";
  }
  function uploadStatus(message,error=false) {$("roleUploadStatus").textContent=message;$("roleUploadStatus").classList.toggle("error",error);}
  function addLibraryRole(asset) {
    if(locked()||state.roles.length>=8||asset.status!=="Active"||state.roles.some(r=>r.uri===asset.uri))return false;
    state.roles.push({uri:asset.uri,name:asset.name||asset.id,role:`原片人物${state.roles.length+1}`});renderRoles();markDirty();return true;
  }
  function syncUploads() {
    for(const u of uploads.entries) {
      const asset=state.library.assets.find(a=>a.uri===u.uri);
      if(asset&&!state.library.stale)u.status=asset.status;
      if(u.context===uploads.context&&u.autoAdd&&state.mode==="cast"&&asset&&u.status==="Active")addLibraryRole(asset);
    }
    saveUploads();renderUploads();scheduleUploadReview();
  }
  function renderUploads() {
    const entries=uploads.entries.filter(u=>u.context===uploads.context);
    $("roleUploads").hidden=!entries.length;
    $("uploadReviewState").textContent=uploads.error||(state.library.stale?"当前为缓存状态，正在等待最新审核结果。":"审核中的人物每 15 秒自动刷新；通过后会加入本项目。入库不会自动提交视频生成。");
    $("uploadList").innerHTML=entries.map(u=>{
      const chosen=state.roles.some(r=>r.uri===u.uri),active=u.status==="Active",failed=u.status==="Failed",asset=state.library.assets.find(a=>a.uri===u.uri),url=asset?.preview_url||asset?.url;
      const label=chosen?"已入库 · 已加入本次制作":active?(state.roles.length>=8?"已入库 · 本次已满 8 位人物":"已入库 · 可用于制作"):failed?"入库审核未通过，请更换人物图片后重新上传。":u.watch===false?"已停止自动查询，可在角色库查看进度。":"已上传 · 正在入库审核，通过后可用于制作。";
      return `<article class="upload-entry"><div>${url?`<button type="button" class="upload-thumb" data-upload-preview="${esc(u.uri)}" aria-label="放大 ${esc(u.name)}"><img src="${esc(url)}" alt="${esc(u.name)}"></button>`:""}<span><b>${esc(u.name)}</b><small class="${failed?"error":""}">${label}</small></span></div>${active&&!chosen?`<button type="button" class="secondary" data-upload-select="${esc(u.uri)}" ${locked()||state.roles.length>=8?"disabled":""}>选用人物</button>`:!active&&!failed&&u.watch!==false?`<button type="button" class="text-button" data-upload-dismiss="${esc(u.uri)}">停止自动选用</button>`:""}</article>`;
    }).join("");
  }
  function scheduleUploadReview() {
    clearTimeout(uploads.timer);
    if(uploads.entries.some(u=>u.context===uploads.context&&u.watch!==false&&!["Active","Failed"].includes(u.status)))uploads.timer=setTimeout(loadLibrary,15000);
  }
  async function createRoleGroup() {
    if($("createRoleGroup").disabled||locked())return;
    const name=$("roleNewGroupName").value.trim();uploads.busy=true;updateControls();uploadStatus("正在创建人像组…");
    try {
      const result=await api("/api/character-library/groups",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
      if(!result.group_id)throw new Error("未收到人像组 ID，请刷新角色库确认创建结果。");
      state.library.groups.push({id:result.group_id,name,group_type:"AIGC"});renderUploadGroups(result.group_id);$("roleNewGroupName").value="";$("roleGroupDetails").open=false;uploadStatus(`已创建并选用“${name}”，可以上传人物图片。`);
    } catch(e) {uploadStatus(e.message,true);}
    finally {uploads.busy=false;updateControls();syncUploads();renderLibrary();}
  }
  async function uploadRole() {
    const error=uploadError();if(error)return uploadStatus(error,true);
    const context=uploads.context,name=$("roleUploadName").value.trim(),data=new FormData();
    data.append("group_id",$("roleUploadGroup").value);data.append("name",name);data.append("asset_file",$("roleUploadFile").files[0]);
    uploads.busy=true;updateControls();uploadStatus("正在上传人物图片，请保持页面打开…");
    try {
      const result=await api("/api/character-library/assets",{method:"POST",body:data});
      if(!result.uri)throw new Error("未收到素材 ID，请先刷新角色库确认是否已入库，避免重复上传。");
      uploads.entries.push({uri:result.uri,name,context,status:result.status||"Processing",autoAdd:true,watch:true});saveUploads();
      $("roleUploadFile").value="";$("roleUploadName").value="";$("roleUploadConsent").checked=false;setMedia("roleUploadPreview","");$("roleUploadPreviewButton").hidden=true;
      if(state.blobs.has("roleUpload")){URL.revokeObjectURL(state.blobs.get("roleUpload"));state.blobs.delete("roleUpload");}
      uploadStatus(`“${name}”已上传，正在等待入库审核。可关闭此窗口查看进度，或继续上传下一位人物。`);
      renderUploads();
    } catch(e) {uploadStatus(`上传未完成：${e.message}${!e.status?" 请先查看角色库确认提交结果，避免重复上传。":""}`,true);}
    finally {uploads.busy=false;updateControls();syncUploads();renderLibrary();loadLibrary();}
  }
  function renderLibrary() {
    const q=$("roleSearch").value.trim().toLowerCase(),group=$("roleGroup").value;
    const assets=state.library.assets.filter(a=>(!a.asset_type||a.asset_type==="Image")&&(!group||a.group_id===group)&&(!q||`${a.name} ${a.id}`.toLowerCase().includes(q)));
    $("libraryState").textContent=`${assets.length} 张图片 · 点击图片放大，点击“选用”添加人物。${state.library.stale?"当前显示缓存，请刷新获取最新状态。":""}`;
    $("roleGrid").innerHTML=assets.map(a=>{const chosen=state.roles.some(r=>r.uri===a.uri),url=a.preview_url||a.url;return `<article class="role-card${chosen?" chosen":""}"><button type="button" class="thumb" data-library-preview="${esc(a.uri)}" aria-label="放大 ${esc(a.name)}">${url?`<img src="${esc(url)}" alt="${esc(a.name)}" loading="lazy">`:"暂无图片"}</button><b title="${esc(a.name)}">${esc(a.name||a.id)}</b><small>${esc(state.library.groups.find(g=>g.id===a.group_id)?.name||"角色库")} · ${esc(a.status)}</small><button type="button" data-library-select="${esc(a.uri)}" ${chosen||a.status!=="Active"||locked()||state.roles.length>=8?"disabled":""}>${chosen?"已选用 ✓":a.status==="Active"?"选用人物":"等待审核通过"}</button></article>`;}).join("")||'<p class="empty">没有匹配的图片，试试更换名称或人像组。</p>';
  }
  function mentionMatch() {const t=$("prompt");return t.value.slice(0,t.selectionStart).match(/@([^@\s，。；、\n]*)$/u);}
  function showMentions() { const match=mentionMatch();if(!match){$("mentionMenu").hidden=true;return;}state.menu=materials().filter(m=>m.available&&`${m.token} ${m.name}`.toLowerCase().includes(match[1].toLowerCase()));state.menuIndex=0;renderMentions(); }
  function renderMentions() {$("mentionMenu").hidden=!state.menu.length;$("mentionMenu").innerHTML=state.menu.map((m,i)=>`<button type="button" role="option" id="mention-${i}" aria-selected="${i===state.menuIndex}" data-mention="${i}" class="${i===state.menuIndex?"active":""}">${m.url?`<img src="${esc(m.url)}" alt="">`:"▶"}<span>${m.token} · ${esc(m.name)}</span></button>`).join("");$("prompt").setAttribute("aria-expanded",String(!!state.menu.length)); }
  function insertToken(token,replaceMention=false) {const t=$("prompt"),match=replaceMention?mentionMatch():null;const start=match?match.index:t.selectionStart;t.setRangeText(`${token} `,start,t.selectionEnd,"end");t.focus();$("mentionMenu").hidden=true;markDirty();}
  $("prompt").oninput=()=>{markDirty();showMentions();};$("prompt").onclick=showMentions;
  $("prompt").onkeydown=e=>{if($("mentionMenu").hidden)return;if(e.key==="Escape"){$("mentionMenu").hidden=true;e.preventDefault();}else if(["ArrowDown","ArrowUp"].includes(e.key)){e.preventDefault();state.menuIndex=(state.menuIndex+(e.key==="ArrowDown"?1:-1)+state.menu.length)%state.menu.length;renderMentions();}else if(e.key==="Enter"){e.preventDefault();insertToken(state.menu[state.menuIndex].token,true);}};
  $("mentionMenu").onpointerdown=e=>e.preventDefault();$("mentionMenu").onclick=e=>{const b=e.target.closest("[data-mention]");if(b)insertToken(state.menu[Number(b.dataset.mention)].token,true);};
  $("referenceChips").onpointerdown=e=>e.preventDefault();$("referenceChips").onclick=e=>{const b=e.target.closest("[data-token]");if(b&&!b.disabled)insertToken(b.dataset.token);};
  document.addEventListener("click",e=>{if(!e.target.closest(".editor"))$("mentionMenu").hidden=true;});
  document.querySelectorAll("[data-source]").forEach(b=>b.onclick=()=>setSource(b.dataset.source));
  document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{if(b.dataset.mode===state.mode)return;setMode(b.dataset.mode);syncUploads();if($("prompt").value)notice("已切换制作方式，请核对提示词中的图片引用；可点击“填入当前模式示例”重新填写。");});
  $("defaultPrompt").onclick=()=>{$("prompt").value=defaultPrompt();markDirty();};
  for(const field of ["scene","first","last"]){$(`${field}File`).onchange=()=>{const f=$(`${field}File`).files[0];if(f)blob(field,f);if(field==="last")state.clearLast=false;renderImages();markDirty();};$(`${field}PreviewButton`).onclick=()=>openImage(imageURL(field),({scene:"目标场景",first:"首帧参考",last:"尾帧参考"})[field]);}
  $("clearLast").onclick=()=>{$("lastFile").value="";state.clearLast=true;if(state.mode==="frames")$("prompt").value=$("prompt").value.replace(/@图片\s*2(?!\d)/g,"【已移除的尾帧】");renderImages();markDirty();};
  $("sourceFile").onchange=()=>{const f=$("sourceFile").files[0];if(f){setMedia("sourcePreview",blob("source",f));$("sourceEmpty").hidden=true;state.sourceDirty=true;$("mosaicReviewed").checked=false;$("whiteReviewed").checked=false;if(state.job)notice("已选择新原片，请重新打码并生成白膜。当前制作草稿将随新项目重新开始。");}updateControls();};
  $("whiteFile").onchange=()=>updateControls();
  $("prepareBtn").onclick=()=>{const f=form();f.append("source",$("sourceFile").files[0]);submit("prepare",f,{restore:true});};
  $("importBtn").onclick=()=>{const f=form();f.append("white_video",$("whiteFile").files[0]);submit("import-white",f,{restore:true});};
  $("whiteBtn").onclick=()=>{const f=form();f.append("mosaic_reviewed",String($("mosaicReviewed").checked));submit("white-model",f,{paid:["生成动作白膜","Seedance 2.0 · 480p · 预计 1 次视频任务。使用已确认的打码视频提取动作，生成独立的白膜母版。"]});};
  $("generateBtn").onclick=()=>{const error=draftError();if(error)return notice(error,true);const f=form(true);f.append("white_reviewed",String($("whiteReviewed").checked));submit("generate",f,{paid:["生成人物动作迁移视频",`Seedance 2.5 · ${$("resolution").value} · 预计 1 次视频任务。${state.mode==="cast"?`使用 1 张场景图与 ${state.roles.length} 位人物。`:"使用首帧参考图"+(imageURL("last")?"与尾帧参考图。":"。")}${state.mode==="frames"?"首尾图片引导画面，不保证逐像素一致。":""}`]});};
  $("saveDraft").onclick=()=>submit("draft",form(true),{restore:true});
  $("recoverBtn").onclick=()=>submit("recover",form());
  $("refreshJob").onclick=()=>refresh(false);
  for(const id of ["mosaicReviewed","whiteReviewed"])$(id).onchange=updateControls;
  for(const id of ["resolution","generateAudio"])$(id).onchange=markDirty;
  $("uploadRole").onclick=()=>{if(locked())return;$("roleUploadDialog").showModal();updateUploadControls();loadLibrary();};
  $("closeRoleUpload").onclick=()=>{if(!uploads.busy)$("roleUploadDialog").close();};
  $("roleUploadDialog").addEventListener("cancel",e=>{if(uploads.busy)e.preventDefault();});
  $("roleUploadFile").onchange=()=>{
    const file=$("roleUploadFile").files[0],error=uploadFileError(file);
    const url=file&&!error?blob("roleUpload",file):"";setMedia("roleUploadPreview",url);$("roleUploadPreviewButton").hidden=!url;
    if(file&&!error)$("roleUploadName").value=file.name.replace(/\.[^.]+$/,"").slice(0,64);
    $("roleUploadConsent").checked=false;uploadStatus(error||"预览并确认人物图片后，选择人像组提交入库。",!!file&&!!error);updateUploadControls();
  };
  $("roleUploadPreviewButton").onclick=()=>openImage(state.blobs.get("roleUpload"),$("roleUploadName").value||"待上传的人物图片");
  for(const id of ["roleUploadName","roleNewGroupName"])$(id).oninput=updateUploadControls;
  for(const id of ["roleUploadGroup","roleUploadConsent"])$(id).onchange=updateUploadControls;
  $("createRoleGroup").onclick=createRoleGroup;$("submitRoleUpload").onclick=uploadRole;$("refreshUploads").onclick=loadLibrary;
  $("uploadList").onclick=e=>{
    const preview=e.target.closest("[data-upload-preview]"),select=e.target.closest("[data-upload-select]"),dismiss=e.target.closest("[data-upload-dismiss]");
    const uri=preview?.dataset.uploadPreview||select?.dataset.uploadSelect||dismiss?.dataset.uploadDismiss;
    const entry=uploads.entries.find(u=>u.uri===uri&&u.context===uploads.context),asset=state.library.assets.find(a=>a.uri===uri);if(!entry)return;
    if(preview&&asset)return openImage(asset.preview_url||asset.url,entry.name);
    if(select&&!select.disabled&&asset)addLibraryRole(asset);
    if(dismiss){entry.autoAdd=false;entry.watch=false;}
    saveUploads();renderUploads();renderLibrary();scheduleUploadReview();
  };
  $("addRole").onclick=()=>{$("roleDialog").showModal();renderLibrary();loadLibrary();};$("closeRoles").onclick=()=>$("roleDialog").close();$("refreshRoles").onclick=loadLibrary;$("roleSearch").oninput=renderLibrary;$("roleGroup").onchange=renderLibrary;
  $("roleGrid").onclick=e=>{const preview=e.target.closest("[data-library-preview]"),select=e.target.closest("[data-library-select]");const uri=preview?.dataset.libraryPreview||select?.dataset.librarySelect;const a=state.library.assets.find(a=>a.uri===uri);if(!a)return;if(preview)return openImage(a.preview_url||a.url,a.name);if(select&&!select.disabled&&addLibraryRole(a)){renderLibrary();renderUploads();}};
  $("selectedRoles").onclick=e=>{const p=e.target.closest("[data-preview-role]"),r=e.target.closest("[data-remove-role]");if(p){const role=state.roles[Number(p.dataset.previewRole)];return openImage(roleURL(role),role.name);}if(r&&!locked()){const index=Number(r.dataset.removeRole),removed=state.roles.splice(index,1)[0];for(const u of uploads.entries)if(u.context===uploads.context&&u.uri===removed.uri)u.autoAdd=false;saveUploads();if(state.mode==="cast")$("prompt").value=$("prompt").value.replace(/@图片\s*(\d+)/g,(text,num)=>Number(num)===index+2?"【已移除的人物图片】":Number(num)>index+2?`@图片${Number(num)-1}`:text);renderRoles();markDirty();renderUploads();}};
  $("selectedRoles").oninput=e=>{if(e.target.dataset.roleLabel!==undefined){state.roles[Number(e.target.dataset.roleLabel)].role=e.target.value;markDirty();}};
  $("closeImage").onclick=()=>$("imageDialog").close();$("fullImage").onerror=()=>{$("fullImage").hidden=true;$("fullImage").removeAttribute("src");$("imageError").hidden=false;};
  $("roleGrid").addEventListener("error",e=>{if(e.target.tagName==="IMG"){e.target.hidden=true;const span=document.createElement("span");span.textContent="图片暂不可用，可刷新重试";e.target.parentElement.append(span);}},true);
  $("paidCancel").onclick=()=>closePaid(false);$("paidSubmit").onclick=()=>closePaid(true);$("paidDialog").addEventListener("cancel",e=>{e.preventDefault();closePaid(false);});
  for(const id of ["imageDialog","roleDialog"])$(id).onclick=e=>{if(e.target===$(id)){const r=$(id).getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$(id).close();}};
  $("history").onchange=async()=>{const id=$("history").value;if(!id)return;try{renderJob(await api(`/api/motion-transfer/projects/${id}`),true);}catch(e){notice(e.message,true);}};
  $("newProject").onclick=()=>{clearTimeout(state.timer);uploads.context=`new-${crypto.randomUUID()}`;state.job=null;state.sourceDirty=false;state.saved={};state.roles=[];state.dirty=false;state.clearLast=false;store(key,"");document.querySelectorAll('input[type="file"]').forEach(n=>n.value="");document.querySelectorAll('input[type="checkbox"]').forEach(n=>n.checked=false);state.blobs.forEach(url=>URL.revokeObjectURL(url));state.blobs.clear();setMedia("roleUploadPreview","");$("roleUploadPreviewButton").hidden=true;$("roleUploadName").value="";uploadStatus("");renderUploads();scheduleUploadReview();$("prompt").value="";for(const f of ["source","mosaic","white"]){setMedia(`${f}Preview`,"");$(`${f}Empty`).hidden=false;}for(const id of ["mosaicDownload","whiteDownload","jobError"])$(id).hidden=true;$("outputs").replaceChildren();$("outputsEmpty").hidden=false;$("jobStage").textContent="等待准备新的动作素材";$("jobId").textContent="";$("jobProgress").value=0;$("logs").textContent="尚无任务";$("history").value="";$("draftState").textContent="草稿未保存";setSource("original");setMode("cast",false);renderImages();renderRoles();updateControls();notice("已开始新项目，之前的素材与成片仍可从历史项目打开。");$("prepare").scrollIntoView({behavior:"smooth"});};
  window.addEventListener("beforeunload",e=>{if(state.dirty||state.sourceDirty||uploads.busy){e.preventDefault();e.returnValue="";}});
  renderImages();renderRoles();updateControls();refresh(true);history();loadLibrary();
})();
