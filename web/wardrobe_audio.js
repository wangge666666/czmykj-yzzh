(() => {
  let panel;
  window.DepthFlowAudio = {
    render(job, {anchor, busy=false, onRestore}) {
      const target = document.querySelector(anchor);
      if (!target) return;
      if (!panel) { panel=document.createElement('div'); panel.id='wardrobeAudioRepair'; }
      if (panel.previousElementSibling !== target) target.after(panel);
      panel.hidden = !job.has_white_model || !job.kind?.startsWith('wardrobe_prepare_');
      if (panel.hidden) return;
      const button=document.createElement('button'); button.type='button'; button.className='secondary-button';
      button.textContent='恢复原片声音（免费）'; button.disabled=busy;
      const note=document.createElement('p');
      note.textContent='白模应能听到原片对白。旧视频可免费恢复声音；若画面已生成闭嘴，需要重新生成成片来修正口型。';
      button.onclick=async()=>{
        button.disabled=true;
        try {
          const form=new FormData(); form.set('source_job_id',job.id);
          const response=await fetch('/api/wardrobe-swap/restore-audio',{method:'POST',body:form});
          const body=await response.json(); if(!response.ok) throw Error(body.error);
          onRestore(body.id);
        } catch(error) { note.textContent=error.message; button.disabled=false; }
      };
      panel.replaceChildren(button,note);
    }
  };
})();
