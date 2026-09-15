// Local DOM workflow test; no model requests and no browser automation.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname,'..');
const tick = async () => { for (let i=0;i<5;i++) await new Promise(resolve=>setImmediate(resolve)); };
const html = fs.readFileSync(path.join(root,'web/wardrobe.html'),'utf8');
const script = fs.readFileSync(path.join(root,'web/wardrobe.js'),'utf8');

function setup({restore=false, oldServer=false, thresholdSupported=true, savedThreshold=null, mode='dynamic', segments=false}={}) {
  const dom = new JSDOM(html,{url:`http://localhost/projects/wardrobe#${mode}`,runScripts:'outside-only'});
  const w=dom.window, $=id=>w.document.getElementById(id), errors=[], posts=[], jobs={};
  w.addEventListener('error',event=>errors.push(event.error));
  w.requestAnimationFrame = fn=>fn();
  w.setTimeout = ()=>1; w.clearTimeout=()=>{};
  w.HTMLMediaElement.prototype.load=function(){}; w.HTMLMediaElement.prototype.pause=function(){};
  w.HTMLElement.prototype.scrollIntoView=function(){};
  w.URL.createObjectURL=()=> 'blob:fixture'; w.URL.revokeObjectURL=()=>{};
  const selection = w.document.createElement('input'); selection.id='personAsset'; $('characters').append(selection);
  const files=new WeakMap();
  for (const el of w.document.querySelectorAll('input[type=file]')) {
    Object.defineProperty(el,'files',{get:()=>files.get(el)||[]});
    Object.defineProperty(el,'value',{get:()=>'',set:()=>files.delete(el)});
  }
  const change=id=>$(id).dispatchEvent(new w.Event('change',{bubbles:true}));
  const input=(id,value)=>{$(id).value=value;$(id).dispatchEvent(new w.Event('input',{bubbles:true}));};
  const setFile=(id,name,type='image/png')=>{files.set($(id),[new w.File(['fixture'],name,{type})]);change(id);};
  const anchors=[{time:0,x:.2,y:.2,w:.2,h:.3,absent:false}];
  function job(id,extra={}) {
    return {id,kind:`wardrobe_prepare_${mode}`,status:'succeeded',progress:100,logs:[],created_at:'2026-09-13',source_url:`/api/jobs/${id}/file/source`,has_mosaic:true,mosaic_url:`/api/jobs/${id}/file/mosaic`,wardrobe_dynamic:{anchors,description:'已保存的原片目标'},...extra};
  }
  if(restore)jobs.saved=job('saved',{has_white_model:true,white_model_url:'/white.mp4',white_model_revision:'1',wardrobe_mosaic: savedThreshold===null ? {} : {face_score_threshold:savedThreshold}});
  if(segments){
    const group={group_id:'group',duration:29,current:1,segments:[{job_id:'saved',index:1,start:0,end:15,duration:15,finished:false},{job_id:'second',index:2,start:15,end:29,duration:14,finished:false}]};
    jobs.saved.wardrobe_segments=group;
    jobs.second=job('second',{has_mosaic:false,mosaic_url:'',has_white_model:false,wardrobe_segments:{...group,current:2}});
  }
  let loseResponse=false;
  w.fetch=async(url,options={})=>{
    if(options.method==='POST') {
      posts.push({url,body:options.body}); const form=options.body;
      let result;
      if(url.endsWith('/mosaic')) {
        result=job(`source-${posts.length}`,{wardrobe_dynamic:{mosaic_scope:mode==='dynamic'?'all_faces':'preserve_source',description:form.get('target_description')||''},wardrobe_mosaic:{face_score_threshold:Number(form.get('face_score_threshold'))}});
      } else if(url.endsWith('/white-model')) {
        result=job(form.get('source_job_id'),{has_white_model:true,white_model_url:'/white.mp4',white_model_revision:String(posts.length),wardrobe_mosaic:jobs[form.get('source_job_id')].wardrobe_mosaic});
      } else if(url.endsWith('/restore-audio')) {
        result={...jobs[form.get('source_job_id')],white_model_revision:'repaired'};
      } else if(url.endsWith('/generate')) {
        if(loseResponse) {loseResponse=false;throw new Error('响应丢失');}
        result=job('output',{kind:`wardrobe_generate_${mode}`,has_output:true,output_url:'/output.mp4'});
      } else throw Error(`Unexpected POST ${url}`);
      jobs[result.id]=result; return {ok:true,json:async()=>result};
    }
    let body;
    if(url==='/api/config')body={ark_ready:true,wardrobe_mosaic_threshold_supported:thresholdSupported,wardrobe_dynamic_modes:oldServer?{}:{dynamic_object:{white_prompt:'物品白模@视频1',final_prompt:'新物品@图片1与白模@视频1',image_label:'新物品图',target_label:'指定物品',target_default:''},dynamic_scene:{white_prompt:'场景白模@视频1',final_prompt:'新场景@图片1与白模@视频1',image_label:'新场景图',target_label:'指定场景',target_default:'背景环境'}},wardrobe_dynamic_mosaic_scope:oldServer?undefined:'all_faces',wardrobe_dynamic_white_prompt:'只换主角，参考@视频1',wardrobe_swap_prompts:{dynamic:'参考@视频1，人物@图片1，服装@图片2'}};
    else if(url.startsWith('/api/wardrobe-swap/latest'))body=restore?jobs.saved:{id:''};
    else if(url.startsWith('/api/jobs/'))body=jobs[url.split('/').at(-1)];
    else throw Error(`Unexpected GET ${url}`);
    return {ok:true,json:async()=>body};
  };
  const confirm=async()=>{$('paidConfirmForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();};
  w.eval(fs.readFileSync(path.join(root,'web/wardrobe_segments.js'),'utf8'));
  w.eval(fs.readFileSync(path.join(root,'web/wardrobe_audio.js'),'utf8'));
  w.eval(script);
  return {w,$,posts,jobs,input,change,setFile,confirm,lose:()=>{loseResponse=true;},close:()=>{assert.deepEqual(errors,[]);w.close();}};
}

async function run(){
  const split=setup({restore:true,segments:true});await tick();
  assert.equal(split.$('generateAudio').checked,true,'source dialogue is preserved by default');
  split.w.document.querySelector('#wardrobeAudioRepair button').click();await tick();
  assert.equal(split.posts[0].url,'/api/wardrobe-swap/restore-audio');
  split.w.document.querySelector('[data-segment-index="2"]').click();await tick();
  assert.match(split.$('sourcePreview').src,/second\/file\/source/);
  assert.equal(split.$('whiteCard').classList.contains('hidden'),true,'another segment cannot inherit the first white preview');
  assert.equal(split.$('mosaicBtn').disabled,false);
  assert.equal(split.$('whiteModelBtn').disabled,true);
  assert.match(split.$('wardrobeSegments').textContent,/当前制作第 2 段/);
  split.close();
  const t=setup(); await tick(); const {$,w}=t;
  assert.equal($('modeInput').value,'dynamic');
  assert.equal($('referenceExtractionStage').classList.contains('hidden'),true);
  assert.equal($('newClothingField').classList.contains('hidden'),false);
  assert.equal($('originalSceneField').classList.contains('hidden'),true);
  assert.equal($('generateBtn').disabled,true);
  assert.equal($('dynamicTargetCanvas'),null); assert.equal($('dynamicDescription'),null);
  t.setFile('referenceVideo','original.mp4','video/mp4');
  assert.equal($('mosaicBtn').disabled,false);
  $('mosaicBtn').click();await tick();
  assert.equal(t.posts.length,1); assert.equal(t.posts[0].body.has('target_anchors'),false); assert.equal(t.posts[0].body.has('target_description'),false);
  assert.equal(t.posts[0].body.get('face_score_threshold'),'0.55');
  assert.equal($('whiteModelBtn').disabled,true);
  $('dynamicMosaicReviewed').checked=true;t.change('dynamicMosaicReviewed');$('whiteModelBtn').click();await tick();
  assert.equal(t.posts.length,1,'confirmation must precede paid request');
  await t.confirm();assert.equal(t.posts.length,2);assert.equal(t.posts[1].body.get('mosaic_reviewed'),'true');
  t.input('personAsset','asset://role-active-123');t.setFile('newClothingImage','衣服.png');
  $('dynamicWhiteReviewed').checked=true;t.change('dynamicWhiteReviewed');
  assert.equal($('generateBtn').disabled,false);
  t.input('prompt','领口参考 @');
  assert.equal($('dynamicMentionMenu').children.length,3);
  $('dynamicMentionMenu').children[2].click();assert.equal($('prompt').value,'领口参考 @图片2 ');
  t.lose();$('generateBtn').click();await tick();await t.confirm();
  const requestId=t.posts[2].body.get('request_id');
  $('generateBtn').click();await tick();await t.confirm();
  assert.equal(t.posts[3].body.get('request_id'),requestId,'lost-response retry must retain id');
  assert.equal(t.posts[3].body.get('person_asset'),'asset://role-active-123');
  assert.equal(t.posts[3].body.get('new_clothing_image').name,'衣服.png');
  assert.equal(t.posts[3].body.has('new_scene_image'),false);
  t.setFile('referenceVideo','another-video.mp4','video/mp4');
  assert.equal($('generateBtn').disabled,true);assert.equal($('whiteModelBtn').disabled,true);
  t.close();

  const restored=setup({restore:true,savedThreshold:0.4});await tick();
  assert.equal(restored.$('dynamicAnchors'),null);
  assert.match(restored.$('sourcePreview').src,/saved\/file\/source/);
  assert.equal(restored.$('mosaicBtn').disabled,false);
  assert.equal(restored.$('faceScoreThreshold').value,'0.4');
  assert.match(restored.$('faceThresholdStatus').textContent,/0.40/);
  for (const value of ['', '0.29', '0.91']) {
    restored.input('faceScoreThreshold',value);
    assert.equal(restored.$('mosaicBtn').disabled,true);
  }
  restored.input('faceScoreThreshold','0.35');
  assert.equal(restored.$('mosaicBtn').disabled,false);
  assert.equal(restored.$('whiteModelBtn').disabled,true);
  assert.equal(restored.$('generateBtn').disabled,true);
  assert.match(restored.$('faceThresholdStatus').textContent,/当前视频仍是旧结果/);
  assert.equal(restored.posts.length,0,'changing threshold must not submit local or paid work');
  restored.$('mosaicBtn').click();await tick();
  assert.equal(restored.posts[0].body.get('source_job_id'),'saved');
  assert.equal(restored.posts[0].body.has('reference_video'),false);
  assert.equal(restored.posts[0].body.get('face_score_threshold'),'0.35');
  assert.equal(restored.$('faceScoreThreshold').value,'0.35');
  assert.equal(restored.$('whiteModelBtn').disabled,true,'new mosaic needs fresh review');
  restored.close();
  const unsupported=setup({restore:true,thresholdSupported:false});await tick();
  assert.equal(unsupported.$('mosaicBtn').disabled,true);
  assert.equal(unsupported.$('faceScoreThreshold').disabled,true);
  assert.match(unsupported.$('faceThresholdStatus').textContent,/后台尚未加载灵敏度/);unsupported.close();
  const old=setup({oldServer:true});await tick();
  assert.equal(old.$('mosaicBtn').disabled,true);assert.match(old.$('mosaicReason').textContent,/后台尚未加载/);old.close();
  console.log('Wardrobe dynamic DOM workflow: upload without target selection, preview gates, media mentions, retry identity and restore passed.');
}
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={setup,tick};
