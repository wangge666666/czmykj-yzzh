(() => {
  let panel;
  window.DepthFlowSegments = {
    render(job, {anchor, busy=false, onSelect}) {
      if(!panel){panel=document.createElement('section');panel.className='panel';panel.id='wardrobeSegments';}
      const element=document.querySelector(anchor),target=element?.closest('section')||element;if(!target)return;
      if(panel.previousElementSibling!==target)target.after(panel);
      const data=job.wardrobe_segments;panel.hidden=!data?.segments?.length;
      if(panel.hidden)return;
      const heading=document.createElement('h2');heading.textContent=`原片分段 · 共 ${data.segments.length} 段 / ${data.duration.toFixed(3)} 秒`;
      const note=document.createElement('p');note.textContent='选择一段后，使用下方原有步骤打码、生成母版和成片。每段最多15秒；不足5秒时末尾补黑场，生成后恢复该段原时长。全部分段完成后自动合成完整视频。';
      const list=document.createElement('div');list.className='dynamic-reference-chips';
      for(const s of data.segments){
        const button=document.createElement('button');button.type='button';button.dataset.segmentIndex=s.index;
        button.textContent=`第${s.index}段 · ${s.start.toFixed(3)}–${s.end.toFixed(3)}秒${s.finished?' · 成片已保存':''}`;
        button.setAttribute('aria-pressed',String(data.current===s.index));button.disabled=busy;
        button.onclick=()=>onSelect(s.job_id);list.append(button);
      }
      const status=document.createElement('p');status.textContent=`当前制作第 ${data.current} 段 · 已完成 ${data.segments.filter(s=>s.finished).length}/${data.segments.length} 段`;
      const merge=document.createElement('button');merge.type='button';merge.className='secondary-button';merge.textContent='重新合成完整视频（免费）';
      merge.disabled=busy||data.segments.some(s=>!s.finished);
      merge.onclick=async()=>{merge.disabled=true;try{const form=new FormData();form.set('source_job_id',data.segments[data.current-1].job_id);const r=await fetch('/api/wardrobe-swap/segments/assemble',{method:'POST',body:form});const body=await r.json();if(!r.ok)throw Error(body.error);onSelect(body.id);}catch(e){status.textContent=e.message;merge.disabled=false;}};
      const signature=JSON.stringify([data,busy]);
      if(panel.dataset.signature===signature)return;panel.dataset.signature=signature;
      panel.replaceChildren(heading,note,list,status,merge);
      if(data.output_url){const video=document.createElement('video');video.controls=true;video.preload='metadata';video.className='media-preview';video.src=data.output_url+'?v='+data.output_revision;const download=document.createElement('a');download.href=data.output_url+'?download=1';download.textContent='下载完整成片';panel.append(video,download);}
    }
  };
})();
