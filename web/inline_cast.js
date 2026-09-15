/* People remain in the original reference area and use its existing library. */
(() => {
  const library = window.DepthFlowCharacterLibrary, host = document.querySelector('#characters');
  if (!library || !host || !/\/(wardrobe|wardrobe-swap|wardrobe-continuation)$/.test(location.pathname)) return;
  const panel = document.createElement('section'); panel.className = 'inline-cast';
  panel.innerHTML = '<div data-cast-cards></div><button type="button" data-cast-add>＋ 添加人物</button><p data-cast-note>点击“添加人物”，每人分别选择形象和服装。双人使用红、白模；三人增加黄模，四人增加蓝模。双人默认男性红模，同性时首次同框左侧红模；三、四人按首次清晰同框从左到右分配。已有母版按原颜色记录对应，重新生成后使用新顺序。</p>';
  host.querySelector('.shared-character-library').before(panel);
  const style = document.createElement('style');style.textContent = '.inline-prompt-preview{margin:14px 0;padding:14px 18px;border:1px solid #b7d8cb;border-radius:12px;background:#f4faf7;color:#234a39}.inline-prompt-preview[hidden],.inline-prompt-superseded{display:none!important}.inline-prompt-preview summary{font-weight:700;cursor:pointer}.inline-prompt-preview p{font-size:14px;line-height:1.7}.inline-prompt-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;font:14px/1.8 system-ui;background:white;padding:12px;border-radius:8px}.inline-prompt-preview pre:empty{display:none}.inline-cast{margin:18px 0}.inline-cast [data-cast-cards]{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.inline-cast article{border:1px solid #b7d8cb;border-radius:14px;padding:16px;display:grid;gap:10px;background:#f7fcf9}.inline-cast img{width:100%;height:140px;object-fit:contain}.inline-cast img:not([src]){display:none}.inline-cast p,.inline-cast small{font-size:14px;line-height:1.7;color:#527163}.inline-cast button{margin:6px 0;padding:10px 14px;border:1px solid #a9d1bf;border-radius:9px;background:#e0f4e9;color:#126547;font:inherit;cursor:pointer}.inline-cast [data-cast-add]{background:#147853;color:white;padding:12px 24px}.inline-cast input:not([type=file]){padding:9px;border:1px solid #b7d8cb;border-radius:8px}.inline-cast .image-drop{min-height:100px}.inline-cast [data-cast-remove],.inline-cast [data-cast-clear-clothing]{background:transparent;font-size:14px}.inline-cast label{display:grid;gap:5px}.inline-cast article[data-editing="true"]{outline:2px solid #16825c}';document.head.append(style);
  let firstMaterials=[], primaryInput=null, primarySaved='', primaryUrl='';
  const hasClothing=()=>!!(primaryInput?.files[0]||primarySaved);
  const clothingState=()=>primaryInput?.files[0]?'upload':primarySaved?'saved':'none';
  let people=[], base=2, enabled=true, locked=false, context='', editing='', prompts=[], previous=[];
  let getPreview=null, colored=false, colorPlan=[], previewSignature='', previewVersion=0, previewQueued=false, finalPreview=null, whitePreview=null;
  const defaultColors=['red','white','yellow','blue'], colorNames={red:'红',white:'白',yellow:'黄',blue:'蓝'};
  const esc = s => String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const emit=()=>document.dispatchEvent(new Event('inline-cast-change'));
  const asset=p=>library.assets().find(a=>a.uri===(p?p.uri:document.querySelector('#personAsset').value));
  function bindings(){let n=base;return !enabled?[]:people.flatMap((p,i)=>{const a=[{id:p.id+':person',token:`@图片${++n}`,label:`人物${i+2}形象`,ready:!!p.uri}];if(p.file||p.clothing_url)a.push({id:p.id+':clothing',token:`@图片${++n}`,label:`人物${i+2}服装`,ready:true});return a;});}
  function remap(){const first=1+Number(hasClothing()), next=[...(hasClothing()?[{id:'primary:clothing',token:'@图片2'}]:[]),...Array.from({length:Math.max(0,base-first)},(_,i)=>({id:'base:'+i,token:'@图片'+(first+i+1)})),...bindings()],byId=new Map(next.map(b=>[b.id,b.token])),byToken=new Map(previous.map(b=>[b.token,b.id]));
    prompts.forEach(input=>{input.value=input.value.replace(/@\s*图片\s*(\d+)/g,(token,n)=>byToken.has('@图片'+n)?byId.get(byToken.get('@图片'+n))||'@已移除人物参考':token);});previous=next;
  }
  function previewBox(anchor, id, title){
    if(!anchor)return null;
    const box=document.createElement('details');box.id=id;box.className='inline-prompt-preview';box.open=true;
    const heading=document.createElement('summary');heading.textContent=title;
    const note=document.createElement('p');note.textContent='随人数、人物对应和参考图自动更新；补充要求继续在原编辑框填写。';
    const status=document.createElement('p');status.dataset.promptStatus='';status.setAttribute('aria-live','polite');
    const text=document.createElement('pre');text.dataset.promptText='';
    box.append(heading,note,status,text);anchor.after(box);return box;
  }
  function queuePreview(){
    if(previewQueued)return;
    previewQueued=true;Promise.resolve().then(()=>{previewQueued=false;updatePreview();});
  }
  async function updatePreview(){
    if(!getPreview)return;
    const active=enabled&&(people.length>0||!hasClothing()), params=getPreview();
    document.querySelectorAll('#dynamicFinalBase,#dynamicWhiteBase,#objectWhiteBase').forEach(node=>node.classList.toggle('inline-prompt-superseded',node.id==='dynamicFinalBase'?active:enabled&&people.length>0));
    if(!finalPreview)finalPreview=previewBox(document.querySelector('#objectPrompt')||document.querySelector('#prompt')||document.querySelector('#rangeList'),'inlineFinalPromptPreview','本次实际提交的多人提示词');
    const whiteAnchor=document.querySelector('#objectMakeWhite')||document.querySelector('#whiteModelBtn');
    if(!whitePreview&&whiteAnchor)whitePreview=previewBox(document.querySelector('#objectWhiteBase')||document.querySelector('#dynamicWhiteBase')||whiteAnchor,'inlineWhitePromptPreview','本次分色母版提示词');
    if(finalPreview)finalPreview.hidden=!active;if(whitePreview)whitePreview.hidden=!enabled||!people.length;
    if(!active){previewSignature='';previewVersion++;return;}
    const payload={...params,colored,color_plan:colorPlan,first_clothing:hasClothing(),people:people.map(p=>({source:p.source||'',has_clothing:!!(p.file||p.clothing_url)}))};
    const signature=JSON.stringify(payload);
    if(signature===previewSignature)return;
    previewSignature=signature;const version=++previewVersion;
    for(const box of [finalPreview,whitePreview])if(box){box.querySelector('[data-prompt-status]').textContent=`正在按 ${people.length+1} 人更新提示词…`;box.querySelector('pre').textContent='';}
    try{
      const response=await fetch('/api/inline-cast/prompt-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:signature});
      const result=await response.json();if(version!==previewVersion)return;
      if(!response.ok)throw Error(result.error||'提示词预览暂时无法读取，请刷新页面后重试。');
      for(const [box,kind] of [[finalPreview,'final'],[whitePreview,'white']])if(box){
        box.querySelector('[data-prompt-status]').textContent=result[kind+'_error']||`已按 ${result.count} 人更新${kind==='final'?` · ${result.image_count} 张图片引用`:''}`;
        box.querySelector('pre').textContent=result[kind+'_prompt']||'';
      }
    }catch(error){if(version!==previewVersion)return;previewSignature='';for(const box of [finalPreview,whitePreview])if(box)box.querySelector('[data-prompt-status]').textContent=error.message;}
  }
  function previews(){
    [...panel.querySelectorAll('article')].forEach((card,i)=>{const p=i?people[i-1]:null,a=asset(p);card.dataset.editing=String(editing===(p?.id||''));
      const color=colorPlan.length===people.length+1?colorPlan[i]?.color:defaultColors[i];
      card.querySelector('b').textContent=`人物${i+1}${people.length?' · '+colorNames[color]+'模':''}`;
      card.querySelector('[data-cast-name]').textContent=a?.name||(p?.uri?'已选择角色':'尚未选择人物');
      card.querySelector('[data-cast-choose]').textContent=(p?p.uri:library.selected())?'更换人物':'选择角色库人物';
      const image=card.querySelector('[data-cast-person-preview]');if(a?.url)image.src=a.url;else image.removeAttribute('src');
      const tag=card.querySelector('[data-cast-tags]');tag.textContent=i?bindings().filter(b=>b.id.startsWith(p.id+':')).map(b=>b.token+' '+b.label).join(' · '):(hasClothing()?'人物形象 @图片1 · 服装 @图片2':'人物形象及服装均使用 @图片1');
    });
  }
  function choose(p,focus=false,sourceMode='existing'){
    editing=p?.id||'';
    const target={key:p?.id||'primary',label:`人物${p?people.indexOf(p)+2:1}`,onSelected:()=>{
      if(editing!==(p?.id||'')||(p&&!people.includes(p)))return;
      const card=panel.querySelectorAll('article')[p?people.indexOf(p)+1:0];
      card.querySelector('[data-cast-choose]').focus({preventScroll:true});
      card.scrollIntoView({behavior:'smooth',block:'center'});
    }};
    if(p)Object.assign(target,{getUri:()=>p.uri,onSelect:uri=>{if(!people.includes(p))return false;p.uri=uri;previews();emit();}});
    library.choose(target,focus?{sourceMode}:{});previews();
    if(focus)host.querySelector('.shared-character-library').scrollIntoView({behavior:'smooth',block:'start'});
  }
  function draw(){
    firstMaterials.forEach(node=>node.remove());
    panel.querySelector('[data-cast-cards]').innerHTML=[null,...people].map((p,i)=>`<article data-id="${p?.id||''}"><b>人物${i+1}</b><img data-cast-person-preview alt="人物${i+1}形象预览"><span data-cast-name></span><div class="inline-cast-actions"><button type="button" data-cast-choose>选择角色库人物</button><button type="button" data-cast-upload>上传新人物</button></div>${p?`<label>人物${i+1}服装参考（可选，未上传使用此人物图中的服装）<input type="file" accept="image/png,image/jpeg,image/webp" data-cast-clothing></label><img data-cast-clothing-preview alt="服装预览" ${p.clothing_url?`src="${esc(p.clothing_url)}"`:''}><div><button type="button" data-cast-change-clothing>更换服装参考图</button><button type="button" data-cast-clear-clothing>删除服装参考图</button></div><label>对应原片人物（可选）<input maxlength="100" data-cast-source value="${esc(p.source)}" placeholder="例如：开头右侧戴眼镜的人"></label><button type="button" data-cast-remove>移除此人物</button>`:''}<small data-cast-tags></small></article>`).join('');
    panel.querySelectorAll('article').forEach((card,i)=>{const p=i?people[i-1]:null;card.querySelector('[data-cast-choose]').onclick=()=>choose(p,true);
      card.querySelector('[data-cast-upload]').onclick=()=>{choose(p,true,'upload');library.pickFile();};
      if(!p)return;
      const input=card.querySelector('[data-cast-clothing]');
      // Keep File objects outside the DOM when another card is added or removed.
      if(p.file){const image=card.querySelector('[data-cast-clothing-preview]');image.src=p.localUrl;}
      card.querySelector('[data-cast-change-clothing]').onclick=()=>input.click();
      input.onchange=()=>{if(p.localUrl)URL.revokeObjectURL(p.localUrl);p.file=input.files[0]||null;p.clothing_url='';p.localUrl=p.file?URL.createObjectURL(p.file):'';const image=card.querySelector('[data-cast-clothing-preview]');if(p.localUrl)image.src=p.localUrl;else image.removeAttribute('src');remap();previews();emit();};
      card.querySelector('[data-cast-clear-clothing]').onclick=()=>{if(p.localUrl)URL.revokeObjectURL(p.localUrl);p.file=null;p.clothing_url='';p.localUrl='';input.value='';card.querySelector('[data-cast-clothing-preview]').removeAttribute('src');remap();previews();emit();};
      card.querySelector('[data-cast-source]').oninput=e=>{p.source=e.target.value;emit();};
      card.querySelector('[data-cast-remove]').onclick=()=>{if(p.localUrl)URL.revokeObjectURL(p.localUrl);people=people.filter(x=>x!==p);remap();draw();if(editing===p.id)choose(null);emit();};
    });mountFirst();previews();lock();
  }
  function mountFirst(){
    if(!firstMaterials.length){
      primaryInput=document.querySelector('#objectClothing')||document.querySelector('#clothingFile')||document.querySelector('#newClothingImage');
      if(primaryInput){
        const label=primaryInput.closest('label');firstMaterials=[label];label.classList.remove('hidden');
        const title=document.createElement('b');title.textContent='人物1服装参考（可选）';label.prepend(title);
        const preview=document.querySelector(primaryInput.id==='objectClothing'?'#objectClothingPreview':primaryInput.id==='clothingFile'?'#clothingPreview':'#unusedClothingPreview')||document.createElement('img');
        preview.dataset.primaryClothingPreview='';preview.alt='人物1服装参考预览';firstMaterials.push(preview);
        const controls=document.createElement('div');controls.innerHTML='<button type="button" data-primary-clothing-change>上传 / 更换服装参考图</button><button type="button" data-primary-clothing-clear>删除服装参考图</button><small>不上传时，服装使用人物1形象图中的内容。</small>';firstMaterials.push(controls);
        const refresh=()=>{if(primaryUrl)URL.revokeObjectURL(primaryUrl);primaryUrl=primaryInput.files[0]?URL.createObjectURL(primaryInput.files[0]):'';const url=primaryUrl||primarySaved;preview.classList.toggle('hidden',!url);if(url)preview.src=url;else preview.removeAttribute('src');controls.querySelector('[data-primary-clothing-clear]').hidden=!hasClothing();};
        controls.querySelector('[data-primary-clothing-change]').onclick=()=>primaryInput.click();
        controls.querySelector('[data-primary-clothing-clear]').onclick=()=>{primarySaved='';primaryInput.value='';primaryInput.dispatchEvent(new Event('change',{bubbles:true}));refresh();emit();};
        primaryInput.addEventListener('change',()=>{if(primaryInput.files[0])primarySaved='';refresh();emit();});
        controls.refresh=refresh;refresh();
        if(primaryInput.id==='newClothingImage'){
          document.querySelector('#originalClothingField')?.classList.add('inline-prompt-superseded');
          document.querySelector('#preparedClothingCard')?.classList.add('inline-prompt-superseded');
          document.querySelector('#customClothingImage')?.closest('article')?.classList.add('inline-prompt-superseded');
        }
      }
    }
    const card=panel.querySelector('article');firstMaterials.forEach(node=>{if(node.parentElement!==card)card.append(node);});
    firstMaterials[0]?.classList.remove('hidden');firstMaterials.at(-1)?.refresh?.();
  }
  function lock(){library.lock(locked);panel.querySelectorAll('button,input').forEach(x=>x.disabled=locked);panel.querySelector('[data-cast-add]').disabled=locked||people.length>=3;}
  panel.querySelector('[data-cast-add]').onclick=()=>{if(locked||people.length>=3)return;const p={id:crypto.randomUUID(),uri:'',source:''};people.push(p);remap();draw();choose(p,true);emit();};
  function restore(job,force=false){
    if(!job)return;
    colorPlan=(job.inline_color_plan||[]).map(p=>({...p}));colored=!!colorPlan.length;
    const next=job.wardrobe_source_job_id||job.id;
    if(context===next&&!force){previews();queuePreview();return;}
    const replace=force||!!context||!!job.inline_cast?.length||colored;context=next;
    if(replace){
      people.forEach(p=>{if(p.localUrl)URL.revokeObjectURL(p.localUrl);});
      primarySaved=job.clothing_url||'';
      people=(job.inline_cast||[]).map(p=>({...p}));
      // A proxy can be made before identity references are chosen. Restore its
      // person slots from the saved color plan instead of silently showing one person.
      if(!people.length&&job.inline_color_plan?.length>1)people=job.inline_color_plan.slice(1).map(p=>({id:'restored-'+p.id,uri:'',source:p.source||''}));
      previous=bindings();draw();choose(null);
    }
  }
  window.DepthFlowInlineCast={
    configure(options){getPreview=options.getPreview||getPreview;base=options.base??base;enabled=options.enabled??enabled;locked=!!options.busy;prompts=options.prompts||prompts;panel.hidden=!enabled;mountFirst();remap();previews();lock();queuePreview();},
    bindings, hasClothing, clothingState, count:()=>enabled?people.length+1:1,
    problem:()=>!enabled?'':library.uploadProblem()||(colorPlan.length&&colorPlan.length!==people.length+1?`当前母版为 ${colorPlan.length} 人，请按当前 ${people.length+1} 人重新生成分色母版。`:people.some(p=>!p.uri)?'请选择新增人物的形象。':base+bindings().length>9?'人物、服装及其他参考图合计最多 9 张。':''),
    append(form){if(!enabled)return;form.append('primary_clothing_optional','true');form.append('primary_clothing_state',clothingState());if(primaryInput?.files[0])form.append('primary_clothing_image',primaryInput.files[0]);form.append('additional_people',JSON.stringify(people.map(p=>({id:p.id,uri:p.uri,source:p.source,keep_clothing:!!p.clothing_url&&!p.file}))));people.forEach(p=>{if(p.file)form.append('additional_clothing_'+p.id,p.file);});},
    appendWhite(form){form.append('inline_cast_count',enabled?people.length+1:1);form.append('inline_cast_sources',JSON.stringify(enabled?people.map(p=>p.source||''):[]));},
    restore,
    clear(){people.forEach(p=>{if(p.localUrl)URL.revokeObjectURL(p.localUrl);});people=[];context='';colored=false;colorPlan=[];primarySaved='';remap();draw();choose(null);queuePreview();},
  };
  document.querySelector('#personAsset').addEventListener('input',previews);
  document.addEventListener('character-library-loaded',previews);
  style.textContent += '.shared-character-library{scroll-margin-top:100px}.inline-cast-actions{display:flex;flex-wrap:wrap;gap:8px}.inline-cast-actions button{flex:1}.inline-cast article{scroll-margin-top:100px}';
  draw();choose(null);
})();
