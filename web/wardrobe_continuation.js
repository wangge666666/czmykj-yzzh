(() => {
  const $ = id => document.getElementById(id);
  const state = {job:null,busy:false,dirty:true,timer:0,sourceUrl:'',confirming:false,previewKey:'',touched:false,
    workflow:'library',sourceDirty:false,library:[],uploading:false,uploadUri:'',uploadTimer:0,uploadVersion:0,libraryVersion:0,loadVersion:0,uploadChoice:false,autoName:'',clothingUrl:'',uploadPreviewUrl:'',extras:[],extrasTouched:false};
  const running = job => ['queued','running','submitted'].includes(job?.status);
  const api = async (url,options={}) => {const response=await fetch(url,options);const data=await response.json();if(!response.ok){const error=Error(data.error||'请求失败');error.status=response.status;throw error;}return data;};
  const notice = text => {$('notice').textContent=text;$('notice').classList.add('show');setTimeout(()=>$('notice').classList.remove('show'),6500);};
  const file = () => $('sourceFile').files[0];
  const show = (id,yes) => $(id).classList.toggle('hidden',!yes);
  const person = () => $('personAsset').value.trim();
  const clothing = () => $('clothingFile').files[0];
  const matches = () => state.job && (state.job.rewrite_workflow||'legacy')===state.workflow;
  const extraBase = (index,workflow=state.workflow) => workflow==='references'?1+Number(window.DepthFlowInlineCast?.hasClothing()??!!(clothing()||state.job?.clothing_url)):0;
  function restoreExtras(job){
    state.extras.filter(item=>item.file).forEach(item=>URL.revokeObjectURL(item.url));
    state.extras=(job.extra_references||[]).map(item=>({...item}));state.extrasTouched=false;drawExtras();
  }
  function drawExtras(){
    $('extraReferenceList').replaceChildren(...state.extras.map((item,index)=>{
      const card=document.createElement('article'),title=document.createElement('b'),image=document.createElement('img'),button=document.createElement('button');
      title.textContent=`${`@图片${extraBase(0)+index+1}`} · ${item.name}`;
      image.src=item.url;image.alt=item.name;button.type='button';button.textContent='移除此图';button.disabled=state.busy||state.confirming;
      button.onclick=()=>{rows().forEach((row,rowIndex)=>{const input=row.querySelector('.range-prompt'),number=extraBase(rowIndex)+index+1;
        input.value=input.value.replace(/@\s*图片\s*(\d+)/g,(token,n)=>Number(n)===number?'@已移除参考':Number(n)>number&&Number(n)<=extraBase(rowIndex)+state.extras.length?`@图片${Number(n)-1}`:token);
      });if(item.file)URL.revokeObjectURL(item.url);state.extras.splice(index,1);state.extrasTouched=true;state.touched=true;drawExtras();readiness();};
      card.append(title,image,button);return card;
    }));
    $('extraReferenceHint').textContent=state.extras.length?`已添加 ${state.extras.length}/7 张。每段可点击对应 @图片 标签插入引用；未指定的内容沿用原片。`:'可留空。支持 JPG、PNG、WEBP，最多 7 张；可分次添加，预览后单独移除。';
  }
  function appendExtras(form){
    let upload=0;const choices=state.extras.map(item=>{if(item.file){form.append('extra_images',item.file);return {upload:upload++};}return {keep:item.index,source_job_id:item.source_job_id,sha256:item.sha256};});
    form.append('extra_references',JSON.stringify(choices));
  }
  window.toast=notice;
  $('rewriteForm').onsubmit=event=>event.preventDefault();
  function workflowView(){
    show('videoLibraryPane',state.workflow==='library');show('localSourcePane',state.workflow!=='library');
    show('mosaicControls',state.workflow==='references');show('actorReferencePane',state.workflow==='references');
    show('mosaicReviewPane',state.workflow==='references'&&matches()&&!!state.job?.mosaic_url&&!state.sourceDirty);
    document.querySelectorAll('[data-rewrite-workflow]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.rewriteWorkflow===state.workflow)));
    $('workflowNote').textContent=state.workflow==='references'?'原片先给全部人脸打码，再结合人物、服装参考直接改写。无需生成白模。':state.workflow==='library'?'原片审核入库后直接引用，人物与服装沿用原片；只填写每个区间的改写要求。':'已恢复旧版任务，可以继续使用，或切换上方任一新素材流程。';
  }
  function dirtySource(){state.touched=true;state.sourceDirty=true;state.loadVersion++;clearTimeout(state.timer);invalidate();workflowView();show('cancelSource',state.workflow==='library'&&matches());}
  function chooseVideoUpload(upload){state.uploadChoice=upload;show('libraryUploadPane',upload);show('videoAssetField',!upload);show('libraryCandidate',!upload);dirtySource();if(!state.uploading){state.uploadVersion++;state.uploadUri='';clearTimeout(state.uploadTimer);$('uploadStatus').textContent='';show('useUploadedVideo',false);}refreshLibrary();}
  function candidate(){return state.library.find(item=>item.uri===$('videoAsset').value&&item.group_id===$('videoGroup').value);}
  function displayCandidate(){
    const item=candidate();show('candidateVideo',!!item?.url);if(item?.url)media($('candidateVideo'),item.url);
    $('candidateStatus').textContent=item?`${item.name} · ${item.status==='Active'?'审核通过，可以预览后使用':'尚未审核通过，暂不能使用'}`:'请选择组内视频。';readiness();
  }
  function groupVideos(preferred=''){
    const list=state.library.filter(item=>item.group_id===$('videoGroup').value);
    $('videoAsset').replaceChildren(new Option(list.length?'请选择组内视频':'此组暂无视频',''),...list.map(item=>new Option(`${item.name} · ${item.status==='Active'?'可使用':'待审核'}`,item.uri)));
    if(list.some(item=>item.uri===preferred))$('videoAsset').value=preferred;displayCandidate();
  }
  async function refreshLibrary(){
    const version=++state.libraryVersion;
    try{const value=await api('/api/wardrobe-continuation/video-library');if(version!==state.libraryVersion)return;state.library=value.assets||[];
      const old=$('videoGroup').value, selected=$('videoAsset').value;
      $('videoGroup').replaceChildren(new Option('请选择素材组',''),...(value.groups||[]).map(group=>new Option(group.name,group.id)));
      $('videoGroup').value=old||(!state.sourceDirty&&matches()?state.job.video_group_id:'')||'';
      $('libraryMessage').textContent=value.message||'';groupVideos(selected||(!state.sourceDirty&&matches()?state.job.video_asset:'')||'');
    }catch(error){$('libraryMessage').textContent=error.message;}
  }
  async function sourceAction(uri=''){
    if(state.busy||state.confirming)return;
    const previousRanges=segments();
    const form=new FormData();form.append('rewrite_workflow',state.workflow);
    if(state.workflow==='library')form.append('video_asset',uri);
    else{form.append('face_score_threshold',$('faceThreshold').value);if(file())form.append('reference_video',file());else form.append('source_job_id',state.job?.id||'');}
    state.touched=true;state.busy=true;state.loadVersion++;readiness();
    try{const job=await api('/api/wardrobe-continuation/source',{method:'POST',body:form});state.sourceDirty=false;$('sourceFile').value='';$('mosaicReviewed').checked=false;show('cancelSource',false);render(job);if(state.workflow==='library'){state.uploadChoice=false;show('libraryUploadPane',false);show('videoAssetField',true);show('libraryCandidate',true);state.uploadUri='';show('useUploadedVideo',false);$('videoGroup').value=job.video_group_id||state.library.find(item=>item.uri===job.video_asset)?.group_id||'';groupVideos(job.video_asset);}if(previousRanges.length){$('rangeList').replaceChildren();previousRanges.forEach(addRange);readiness();}}
    catch(error){state.busy=false;notice(error.message);readiness();}
  }
  async function uploadStatus(key){
    const version=state.uploadVersion;clearTimeout(state.uploadTimer);
    try{const record=await api('/api/wardrobe-continuation/upload-status/'+encodeURIComponent(key));if(version!==state.uploadVersion)return;
      const status=record.asset_status||record.status;state.uploadUri=status==='Active'?record.uri:'';
      const labels={Active:'原片已审核通过，点击下方按钮使用。',Processing:'原片审核中，请稍候。',quota_full:'角色库素材额度不足。',failed:'上传失败。',Failed:'审核未通过。',uncertain:'入库状态尚未确认，请刷新素材核对，避免重复上传。'};
      $('uploadStatus').textContent=(record.deduplicated&&status==='Active'?'已复用内容相同的原片。':labels[status]||'正在上传原片并等待审核。')+(record.error?' '+record.error:'');
      show('useUploadedVideo',!!state.uploadUri);state.uploading=!['Active','Failed','failed','quota_full','uncertain'].includes(status);
      if(state.uploading)state.uploadTimer=setTimeout(()=>uploadStatus(key),7000);else{localStorage.removeItem('depthflow.intervals.upload');await refreshLibrary();}readiness();
    }catch(error){if(version!==state.uploadVersion)return;$('uploadStatus').textContent=error.message;state.uploadTimer=setTimeout(()=>uploadStatus(key),7000);}
  }
  const rows = () => [...$('rangeList').querySelectorAll('.range-card')];
  const segments = () => rows().map(row=>({start:Number(row.querySelector('.range-start').value),end:Number(row.querySelector('.range-end').value),prompt:row.querySelector('.range-prompt').value.trim()}));
  const total = () => file()?$('sourceVideo').duration:Number(state.job?.settings?.source_duration);
  const fmt = n => Number(n).toFixed(3).replace(/\.?0+$/,'');
  function media(node,url){if(url&&node.getAttribute('src')!==url){node.src=url;node.load?.();}}
  function invalidate(){state.touched=true;state.dirty=true;state.previewKey='';readiness();}
  function addRange(value={start:2,end:4,prompt:''}){
    const row=$('rangeTemplate').content.firstElementChild.cloneNode(true);
    row.querySelector('.range-start').value=value.start;row.querySelector('.range-end').value=value.end;row.querySelector('.range-prompt').value=value.prompt||'';
    for(const kind of ['start','end']){
      row.querySelector('.range-'+kind).addEventListener('input',invalidate);
      row.querySelector('.use-'+kind).onclick=()=>{row.querySelector('.range-'+kind).value=$('sourceVideo').currentTime.toFixed(3);invalidate();};
    }
    row.querySelector('.range-prompt').addEventListener('input',()=>{state.touched=true;readiness();});
    row.querySelector('.remove-range').onclick=()=>{row.remove();invalidate();};
    $('rangeList').append(row);
  }
  function rangeProblem(){
    const list=segments(), duration=total();
    if(!list.length)return '请添加至少一个改写区间。';
    if(!Number.isFinite(duration)||duration<=0)return '请上传原片并等待视频时长加载。';
    if(rows().some(row=>!row.querySelector('.range-start').value||!row.querySelector('.range-end').value))return '请填写每段的开始和结束秒数。';
    if(list.some(s=>!Number.isFinite(s.start)||!Number.isFinite(s.end)||s.start<0||s.end<=s.start||s.end>duration+0.00001))return '区间需满足：0 ≤ 开始 < 结束 ≤ 原片时长。';
    if(list.some(s=>s.end-s.start>15.00001))return '每段最长 15 秒，较长范围请拆成多个区间。';
    const sorted=[...list].sort((a,b)=>a.start-b.start);
    if(sorted.some((s,i)=>i&&s.start<sorted[i-1].end-0.00001))return '改写区间不能重叠，请合并或调整开始、结束时间。';
    return '';
  }
  function readiness(){
    const locked=state.busy||state.confirming, problem=rangeProblem(), duration=total();
    window.DepthFlowInlineCast?.configure({base:extraBase(0)+state.extras.length,enabled:state.workflow==='references',busy:locked,prompts:rows().map(row=>row.querySelector('.range-prompt')),getPreview:()=>({mode:'rewrite',extra_count:state.extras.length,duration:total(),fps:state.job?.settings?.fps,segments:segments()})});
    const videoReady=matches()&&!state.sourceDirty&&(state.workflow==='library'?!!state.job.video_asset:state.workflow==='references'&&!!state.job.mosaic_url);
    const sourceProblem=state.workflow==='legacy'?'请选择上方任一素材流程，绑定完整原片后直接生成。':!videoReady?(state.workflow==='library'?'请选择并使用角色库原片。':'请先生成 / 重新打码。'):state.workflow==='references'&&!$('mosaicReviewed').checked?'请预览打码视频并勾选确认。':'';
    let reason=locked?'任务处理中，请等待。':sourceProblem||problem||((duration<2||duration>15.08)?'完整参考视频需为 2–15 秒，请更换符合时长的原片。':state.job?.needs_recovery?'已有生成结果待确认，请先使用下方恢复按钮。':segments().some(s=>!s.prompt)?'请为每个时间区间填写改写要求。':'');
    if(!reason&&segments().reduce((n,s)=>n+s.prompt.length,0)>1000)reason='所有区间的改写要求合计最多 1000 字，请适当精简。';
    if(!reason&&state.workflow==='references')reason=!person()?'请选择或上传人物到角色库。':'';
    if(!reason)reason=window.DepthFlowInlineCast?.problem()||'';
    rows().forEach((row,i)=>{
      row.querySelector('h3').textContent=`改写区间 ${i+1}`;
      row.querySelector('.remove-range').disabled=locked||rows().length===1;
      const refs=[['@视频1',state.workflow==='references'?'完整打码视频':'角色库完整原片',videoReady]];
      if(state.workflow==='references'){refs.push(['@图片1','人物参考（含默认服装）',person()]);if(window.DepthFlowInlineCast?.hasClothing()??!!(clothing()||state.job?.clothing_url))refs.push(['@图片2','人物1服装',true]);}
      refs.push(...state.extras.map((item,index)=>[`@图片${extraBase(i)+index+1}`,item.name,item.url]));
      refs.push(...(window.DepthFlowInlineCast?.bindings()||[]).map(b=>[b.token,b.label,b.ready]));
      const controls=refs.map(([token,label,url])=>{const button=document.createElement('button');button.type='button';button.textContent=`${token} · ${label}`;button.disabled=locked||!url;button.onclick=()=>{const input=row.querySelector('.range-prompt');const before=input.value.slice(0,input.selectionStart),match=before.match(/@[^\s@，。；]*$/);input.setRangeText(token+' ',match?input.selectionStart-match[0].length:input.selectionStart,input.selectionEnd,'end');input.focus();readiness();};return button;});
      row.querySelector('.references').replaceChildren(...controls);
      row.querySelector('.reference-note').textContent=state.workflow==='references'?'@视频1 绑定打码视频；人物图同时提供默认服装，独立服装可选上传。图片编号按当前素材顺序显示。':'@视频1 绑定角色库完整原片，其他参考图从 @图片1 开始。所有区间要求一并提交。';
      if(!reason){const text=segments()[i].prompt.replace(/@\s*(视频|图片)\s*(\d+)/g,'@$1$2');const allowed=refs.filter(x=>x[2]).map(x=>x[0]);if(/@/.test(text.replace(/@(视频|图片)\d+/g,''))||(text.match(/@(?:视频|图片)\d+/g)||[]).some(token=>!allowed.includes(token)))reason=`第 ${i+1} 段提示词含有未绑定的 @ 素材。`;}
      row.querySelectorAll('input,textarea').forEach(input=>input.disabled=locked);
      row.querySelectorAll('.use-start,.use-end').forEach(button=>button.disabled=locked||!$('sourceVideo').src);
    });
    $('generateBtn').disabled=!!reason;
    $('generateBtn').textContent='生成完整改写视频';
    $('generateReason').textContent=reason||`已就绪：将 ${segments().length} 个区间的要求一并提交，生成 1 条完整视频。`;
    $('lengthNote').textContent=Number.isFinite(duration)?`完整原片约 ${fmt(duration)} 秒，要求修改 ${segments().length} 个区间；生成时长跟随参考视频。`:'上传原片后显示完整时长。';
    $('addRange').disabled=locked||rows().length>=12;
    for(const id of ['sourceFile','resolution','generateAudio'])$(id).disabled=locked;
    $('recoverBtn').disabled=locked;
    document.querySelectorAll('[data-rewrite-workflow]').forEach(button=>button.disabled=locked||state.uploading);
    for(const id of ['chooseLibrary','chooseUpload','cancelSource','videoGroup','videoAsset','faceThreshold','mosaicReviewed'])$(id).disabled=locked;
    $('referenceFields').disabled=locked;
    $('extraReferenceFiles').disabled=locked||state.extras.length>=7;
    $('extraReferenceList').querySelectorAll('button').forEach(button=>button.disabled=locked);
    const selected=candidate(),bound=matches()&&!state.sourceDirty&&selected?.uri===state.job.video_asset;
    $('useVideoBtn').disabled=locked||selected?.status!=='Active'||bound;$('useVideoBtn').textContent=bound?'当前已使用此原片':'使用此原片';
    $('useUploadedVideo').disabled=locked||!state.uploadUri;
    const uploadFile=$('libraryVideoFile').files[0],uploadReason=state.uploading?'正在上传或审核，请等待。':!$('videoGroup').value?'请先选择素材组。':!uploadFile?'请选择本地原片视频。':!$('videoName').value.trim()?'请填写原片名称。':'';
    $('uploadVideoBtn').disabled=locked||!!uploadReason;$('uploadReason').textContent=uploadReason;
    $('libraryVideoFile').disabled=locked||state.uploading;$('videoName').disabled=locked||state.uploading;
    const threshold=Number($('faceThreshold').value),mosaicReason=!file()&&!state.job?.source_url?'请先上传原片。':!Number.isFinite(threshold)||threshold<.3||threshold>.9?'检测阈值需在 0.30–0.90 之间。':'';
    $('mosaicBtn').disabled=locked||!!mosaicReason;$('mosaicReason').textContent=mosaicReason;
    const sorted=segments().sort((a,b)=>a.start-b.start);$('timelineBar').replaceChildren();
    if(!problem){let cursor=0;for(const s of [...sorted,{start:duration,end:duration}]){if(s.start>cursor){const span=document.createElement('span');span.className='kept';span.style.flexGrow=s.start-cursor;span.title=`保留 ${fmt(cursor)}–${fmt(s.start)} 秒`;$('timelineBar').append(span);}if(s.end>s.start){const span=document.createElement('span');span.className='rewritten';span.style.flexGrow=s.end-s.start;span.title=`改写 ${fmt(s.start)}–${fmt(s.end)} 秒`;$('timelineBar').append(span);}cursor=s.end;}}
    $('timeline').textContent=problem||`要求修改：${sorted.map(s=>`${fmt(s.start)}–${fmt(s.end)} 秒`).join('、')}。其余时间要求模型尽量保持原样。`;
  }
  function render(job,restore=false){
    window.DepthFlowInlineCast?.restore(job,restore);
    const first=state.job?.id!==job.id;state.job=job;state.busy=running(job);
    if(first||restore){
      state.previewKey='';$('rangeList').replaceChildren();
      state.workflow=job.rewrite_workflow||'legacy';state.sourceDirty=false;
      if(restore||(!state.extrasTouched&&job.extra_references?.length))restoreExtras(job);
      if(job.workflow_version===2){state.dirty=!job.segments.length;job.segments.forEach(segment=>addRange(state.workflow==='legacy'?{...segment,prompt:(segment.prompt||'').replace(/@\s*图片\s*(\d+)/g,(token,n)=>Number(n)>1+Number(!!segment.has_after)&&Number(n)<=1+Number(!!segment.has_after)+state.extras.length?`@图片${Number(n)-1-Number(!!segment.has_after)}`:'@旧版图片需重新选择')}:segment));if(!job.segments.length)addRange({start:0,end:Math.min(4,job.settings.source_duration||4),prompt:''});}
      else{state.dirty=true;const start=Number(job.settings.keep_seconds)||0;addRange({start,end:Math.min(job.settings.source_duration,start+15),prompt:(job.settings.prompt||'').replace(/@\s*图片\s*\d+/g,'@旧版图片需重新选择')});$('legacyNote').textContent='已保留旧版任务和原片。选择上方任一素材流程后，可直接填写按秒改写要求生成完整视频。';$('legacyNote').classList.remove('hidden');}
      $('resolution').value=job.settings.resolution||'720p';$('generateAudio').checked=job.settings.generate_audio??true;
      $('faceThreshold').value=job.face_score_threshold||.55;$('mosaicReviewed').checked=false;
      if(restore&&job.person_asset){$('personAsset').value=job.person_asset;$('personAsset').dispatchEvent(new Event('input',{bubbles:true}));}
    }
    if(!file()){media($('sourceVideo'),job.source_url);$('sourceVideo').classList.remove('hidden');$('sourceName').textContent=`已恢复原片 · ${fmt(job.settings.source_duration)} 秒，可重新设置任意区间。`;}
    if(job.mosaic_url){media($('mosaicVideo'),job.mosaic_url);$('mosaicDownload').href=job.mosaic_url+'?download=1';}
    if(job.clothing_url&&!clothing()){media($('clothingPreview'),job.clothing_url);show('clothingPreview',true);}
    workflowView();
    $('stage').textContent=job.status==='succeeded'&&job.stage?.includes('衔接')?'原片已恢复，可直接填写要求生成。':job.stage;$('progress').value=job.progress;$('logs').textContent=(job.logs||[]).join('\n');$('error').textContent=job.error||'';
    $('recoverBtn').classList.toggle('hidden',!job.can_recover);$('recoverBtn').disabled=state.busy;
    $('outputCard').classList.toggle('hidden',!job.output_url);if(job.output_url){media($('outputVideo'),job.output_url+`?v=${encodeURIComponent(job.revision||job.created_at)}`);$('outputDownload').href=job.output_url+'?download=1';}
    const outputs=(job.segments||[]).filter(s=>s.output_url), signature=JSON.stringify(outputs.map(s=>s.output_url))+job.revision;
    if($('segmentResults').dataset.signature!==signature){$('segmentResults').dataset.signature=signature;$('segmentResults').replaceChildren(...outputs.map(s=>{const card=document.createElement('article'),title=document.createElement('h3'),link=document.createElement('a'),video=document.createElement('video');title.textContent=`改写 ${fmt(s.start)}–${fmt(s.end)} 秒 `;link.textContent='下载';link.href=s.output_url+'?download=1';title.append(link);video.controls=true;video.playsInline=true;video.src=s.output_url+`?v=${encodeURIComponent(job.revision)}`;card.append(title,video);return card;}));}
    readiness();clearTimeout(state.timer);if(state.busy)state.timer=setTimeout(poll,2500);
  }
  async function poll(){if(!state.job)return;const version=state.loadVersion;try{const job=await api(`/api/wardrobe-continuation/jobs/${state.job.id}`);if(version===state.loadVersion)render(job);}catch(error){if(version!==state.loadVersion)return;notice(error.message);state.timer=setTimeout(poll,5000);}}
  function confirm(){state.confirming=true;readiness();const list=segments();$('paidDetails').textContent=`参考完整原片（约 ${fmt(total())} 秒），${$('resolution').value}。`;$('paidRanges').replaceChildren(...list.map(s=>{const li=document.createElement('li');li.textContent=`${fmt(s.start)}–${fmt(s.end)} 秒：${s.prompt}`;return li;}));$('paidCost').textContent='本次提交 1 次 Seedance 2.5 付费生成，输出完整视频；多个区间要求一并提交。';$('paidDialog').returnValue='';$('paidDialog').showModal();return new Promise(resolve=>$('paidDialog').addEventListener('close',()=>{state.confirming=false;readiness();resolve($('paidDialog').returnValue==='confirm');},{once:true}));}
  async function generate(){if($('generateBtn').disabled||!(await confirm()))return;const form=new FormData();form.append('source_job_id',state.job.id);form.append('segments',JSON.stringify(segments()));form.append('resolution',$('resolution').value);form.append('generate_audio',String($('generateAudio').checked));form.append('paid_confirmed','true');form.append('generation_mode','full_video');if(state.workflow==='references'){form.append('mosaic_reviewed',String($('mosaicReviewed').checked));form.append('person_asset',person());if(clothing())form.append('clothing_image',clothing());}appendExtras(form);window.DepthFlowInlineCast?.append(form);const storage=`depthflow.fullRewrite.request.${state.job.id}`;let key=localStorage.getItem(storage);if(!key){key=crypto.randomUUID();localStorage.setItem(storage,key);}form.append('request_id',key);state.busy=true;readiness();try{const job=await api('/api/wardrobe-continuation/generate',{method:'POST',body:form});localStorage.removeItem(storage);$('clothingFile').value='';restoreExtras(job);window.DepthFlowInlineCast?.restore(job,true);render(job);$('results').scrollIntoView({behavior:'smooth'});}catch(error){if(error.status&&error.status<500)localStorage.removeItem(storage);state.busy=false;notice(error.message);readiness();}}
  $('sourceFile').addEventListener('change',()=>{if(state.sourceUrl)URL.revokeObjectURL(state.sourceUrl);if(file()){state.sourceUrl=URL.createObjectURL(file());media($('sourceVideo'),state.sourceUrl);$('sourceVideo').classList.remove('hidden');$('sourceName').textContent=file().name;}else if(state.job?.source_url)media($('sourceVideo'),state.job.source_url);dirtySource();});
  $('sourceVideo').addEventListener('loadedmetadata',()=>{if(file()&&rows().length===1&&Number.isFinite(total())&&segments()[0].end>total()){rows()[0].querySelector('.range-start').value=0;rows()[0].querySelector('.range-end').value=Math.min(total(),4).toFixed(3);}readiness();});
  $('addRange').onclick=()=>{const end=Math.max(0,...segments().map(s=>s.end));addRange({start:end,end:Math.min(Number.isFinite(total())?total():end+2,end+2),prompt:''});invalidate();};
  for(const id of ['resolution','generateAudio'])$(id).addEventListener('input',readiness);
  document.addEventListener('inline-cast-change',readiness);
  $('generateBtn').onclick=generate;
  $('recoverBtn').onclick=async()=>{const form=new FormData();form.append('source_job_id',state.job.id);state.busy=true;readiness();try{render(await api('/api/wardrobe-continuation/recover',{method:'POST',body:form}));}catch(error){state.busy=false;notice(error.message);readiness();}};
  $('extraReferenceFiles').onchange=()=>{
    const selected=[...$('extraReferenceFiles').files];$('extraReferenceFiles').value='';
    if(state.extras.length+selected.length>7){notice('其他参考最多添加 7 张图片，请先移除不需要的图片。');return;}
    if(selected.some(item=>!(/\.(jpe?g|png|webp)$/i.test(item.name)))){notice('请选择 JPG、PNG 或 WEBP 图片。');return;}
    state.extras.push(...selected.map(file=>({file,name:file.name,url:URL.createObjectURL(file)})));state.extrasTouched=true;state.touched=true;drawExtras();readiness();
  };
  document.querySelectorAll('[data-rewrite-workflow]').forEach(button=>button.onclick=()=>{
    if(state.busy||state.confirming||state.uploading)return;
    const next=button.dataset.rewriteWorkflow;
    if(next!==state.workflow)rows().forEach((row,index)=>{const input=row.querySelector('.range-prompt'),base=extraBase(index),newBase=next==='references'?2:0;
      input.value=input.value.replace(/@\s*图片\s*(\d+)/g,(token,n)=>Number(n)>base&&Number(n)<=base+state.extras.length?`@图片${newBase+Number(n)-base}`:Number(n)<=base?'@请重新选择图片':token);
    });
    window.DepthFlowInlineCast?.clear();
    state.workflow=next;state.touched=true;state.loadVersion++;clearTimeout(state.timer);state.sourceDirty=false;state.job=null;state.previewKey='';state.dirty=true;
    $('sourceFile').value='';$('sourceVideo').removeAttribute('src');show('sourceVideo',false);$('sourceName').textContent='尚未选择原片。';$('mosaicReviewed').checked=false;show('legacyNote',false);show('outputCard',false);$('segmentResults').replaceChildren();$('stage').textContent='尚未开始';$('progress').value=0;$('error').textContent='';$('logs').textContent='';show('recoverBtn',false);
    workflowView();drawExtras();readiness();
    if(state.workflow==='library')refreshLibrary();
  });
  $('chooseLibrary').onclick=()=>chooseVideoUpload(false);$('chooseUpload').onclick=()=>chooseVideoUpload(true);
  $('cancelSource').onclick=()=>{if(!matches())return;state.sourceDirty=false;state.dirty=!state.job.segments?.length||segments().length!==state.job.segments.length||segments().some((s,i)=>s.start!==state.job.segments[i]?.start||s.end!==state.job.segments[i]?.end);state.uploadChoice=false;show('libraryUploadPane',false);show('videoAssetField',true);show('libraryCandidate',true);show('cancelSource',false);$('videoGroup').value=state.job.video_group_id;groupVideos(state.job.video_asset);readiness();};
  $('videoGroup').onchange=()=>{dirtySource();groupVideos();};$('videoAsset').onchange=()=>{dirtySource();displayCandidate();};$('refreshVideos').onclick=refreshLibrary;
  $('useVideoBtn').onclick=()=>sourceAction(candidate()?.uri||'');$('useUploadedVideo').onclick=()=>sourceAction(state.uploadUri);$('mosaicBtn').onclick=()=>sourceAction();
  $('faceThreshold').oninput=()=>{ $('mosaicReviewed').checked=false;dirtySource();};$('mosaicReviewed').oninput=readiness;
  $('clothingFile').onchange=()=>{if(state.clothingUrl)URL.revokeObjectURL(state.clothingUrl);if(clothing()){state.clothingUrl=URL.createObjectURL(clothing());media($('clothingPreview'),state.clothingUrl);show('clothingPreview',true);}else if(state.job?.clothing_url && window.DepthFlowInlineCast?.clothingState()!=='none')media($('clothingPreview'),state.job.clothing_url);else show('clothingPreview',false);state.touched=true;readiness();};
  $('personAsset').addEventListener('change',readiness);$('personAsset').addEventListener('input',readiness);
  $('libraryVideoFile').onchange=()=>{const selected=$('libraryVideoFile').files[0];if(state.uploadPreviewUrl)URL.revokeObjectURL(state.uploadPreviewUrl);if(selected){state.uploadPreviewUrl=URL.createObjectURL(selected);media($('uploadVideoPreview'),state.uploadPreviewUrl);if(!$('videoName').value.trim()||$('videoName').value===state.autoName){state.autoName=selected.name.replace(/\.[^.]+$/,'').slice(0,64);$('videoName').value=state.autoName;}}show('uploadVideoPreview',!!selected);state.uploadVersion++;state.uploadUri='';show('useUploadedVideo',false);$('uploadStatus').textContent='';dirtySource();};
  $('videoName').oninput=readiness;
  $('uploadVideoBtn').onclick=async()=>{if($('uploadVideoBtn').disabled)return;state.uploadVersion++;state.uploading=true;state.uploadUri='';show('useUploadedVideo',false);readiness();const key=crypto.randomUUID(),form=new FormData();localStorage.setItem('depthflow.intervals.upload',key);form.append('request_id',key);form.append('group_id',$('videoGroup').value);form.append('name',$('videoName').value);form.append('reference_video',$('libraryVideoFile').files[0]);try{await api('/api/wardrobe-continuation/upload-video',{method:'POST',body:form});await uploadStatus(key);}catch(error){notice(error.message);if(error.status&&error.status<500){state.uploading=false;localStorage.removeItem('depthflow.intervals.upload');readiness();}else uploadStatus(key);}};
  window.addEventListener('beforeunload',()=>{clearTimeout(state.timer);clearTimeout(state.uploadTimer);for(const url of [state.sourceUrl,state.clothingUrl,state.uploadPreviewUrl,...state.extras.filter(item=>item.file).map(item=>item.url)])if(url)URL.revokeObjectURL(url);});
  addRange();drawExtras();workflowView();readiness();refreshLibrary();api('/api/wardrobe-continuation/latest').then(job=>{if(job.id&&!state.touched&&!file()){render(job,true);if(state.workflow==='library')refreshLibrary();}}).catch(error=>notice('历史任务恢复失败：'+error.message));
  const pendingUpload=localStorage.getItem('depthflow.intervals.upload');if(pendingUpload){state.uploading=true;uploadStatus(pendingUpload);}
})();
