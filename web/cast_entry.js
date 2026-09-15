(() => {
  const path=location.pathname, isLong=/\/(real-long-video|long-video)$/.test(path);
  if(!isLong)return;
  const anchor=document.querySelector('.hero')||document.querySelector('main')||document.querySelector('form');if(!anchor)return;
  const box=document.createElement('section');box.style.cssText='margin:20px auto;padding:20px 24px;max-width:1080px;border:1px solid #afd2c0;border-radius:16px;background:#eef8f1;color:#18392e;font:16px/1.8 system-ui';
  if(isLong){
    box.innerHTML='<b>双人 / 多人分色母版</b><p>逐镜角色槽位支持最多 4 人。生成人物母版时可使用红、白、黄、蓝区分人物；双人优先将已确认的男性分配为红模，同性或无可靠性别描述时，按首次出现的左右位置分配。跨镜身份分析中的同一人物保持同色。</p><label><input id="coloredCastEnabled" type="checkbox" checked> 新生成的多人母版启用颜色区分</label><p>请先完成人数与跨镜人物分析，核对每镜人物槽位；最终角色和服装继续在下方逐人绑定。已有白模沿用原对应关系，重新生成后才应用颜色。</p>';
  }
  anchor.after(box);
})();
