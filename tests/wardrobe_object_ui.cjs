// DOM integration checks with mocked HTTP; never uploads or submits model work.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const tick = async () => { for (let n=0;n<8;n++) await new Promise(resolve => setImmediate(resolve)); };
async function setup({restore=false,uploadRecord=null,mode='dynamic_object',workflow=null,oldServer=false,extraVideo=false}={}) {
  const namespace=mode==='dynamic_scene'?'scene':'object';
  const dom = new JSDOM(read('web/wardrobe.html'), {url:`http://localhost/projects/wardrobe#${mode}`, runScripts:'outside-only'});
  const w=dom.window, $=id=>w.document.getElementById(id), posts=[], jobs={}, files=new WeakMap(), errors=[];
  if(workflow) w.localStorage.setItem(`depthflow.${namespace}.workflow`,workflow);
  w.localStorage.setItem(`depthflow.${namespace==='scene'?'object':'scene'}.workflow`,'references');
  w.addEventListener('error', event=>errors.push(event.error));
  w.setTimeout=()=>1;w.clearTimeout=()=>{}; w.URL.createObjectURL=()=> 'blob:fixture';w.URL.revokeObjectURL=()=>{};
  w.HTMLMediaElement.prototype.pause=function(){};
  const inputProto=w.HTMLInputElement.prototype, value=Object.getOwnPropertyDescriptor(inputProto,'value');
  Object.defineProperty(inputProto,'files',{get(){return files.get(this)||[];}});
  Object.defineProperty(inputProto,'value',{get(){return value.get.call(this);},set(v){if(this.type==='file')files.delete(this);value.set.call(this,v);}});
  const actor=w.document.createElement('input');actor.id='personAsset';$('characters').append(actor);
  const change=id=>$(id).dispatchEvent(new w.Event('change',{bubbles:true}));
  const input=(id,v)=>{$(id).value=v;$(id).dispatchEvent(new w.Event('input',{bubbles:true}));};
  const setFile=(id,name='ref.png')=>{files.set($(id),[new w.File(['fixture'],name)]);change(id);};
  const make=(id,workflow,extra={})=>({id,kind:`wardrobe_prepare_${mode}`,status:'succeeded',progress:100,logs:[],wardrobe_dynamic:{workflow_version:2,[`${namespace}_workflow`]:workflow,requests:{},...(workflow==='library'?{video_asset:'asset://asset-video123456',video_name:'原片'}:{})},...extra});
  if(restore)jobs.restored=make('restored','references',{has_white_model:true,white_model_url:'/api/jobs/restored/file/white_model',white_model_revision:1,wardrobe_mosaic:{face_score_threshold:.4}});
  let lost=false, failedSelection=false;
  w.fetch=async(url,options={})=>{
    if(url.startsWith(`/static/wardrobe_${namespace}.html`))return {ok:true,text:async()=>read(`web/wardrobe_${namespace}.html`)};
    let body;
    if(options.method==='POST'){
      posts.push({url,body:options.body});const data=options.body;
      if(url.endsWith('/mosaic'))body=make('mosaic','references',{has_mosaic:true,mosaic_url:'/api/jobs/mosaic/file/mosaic',source_url:'/api/jobs/mosaic/file/source',wardrobe_mosaic:{face_score_threshold:Number(data.get('face_score_threshold'))}});
      else if(url.endsWith('/white-model'))body={...jobs[data.get('source_job_id')],has_white_model:true,white_model_url:'/api/jobs/mosaic/file/white_model',white_model_revision:2};
      else if(url.endsWith('/import-white'))body=make('imported','references',{has_white_model:true,white_model_url:'/api/jobs/imported/file/white_model',white_model_revision:1});
      else if(url.endsWith('/select-video')){if(failedSelection){failedSelection=false;return {ok:false,status:400,json:async()=>({error:'所选视频暂不可用'})};}const newer=data.get('video_asset')==='asset://asset-videoNEW123';body=make(newer?'selected-new':'selected','library');Object.assign(body.wardrobe_dynamic,{video_asset:data.get('video_asset'),video_name:newer?'新原片':'原片',video_group_id:'group-123456789'});}
      else if(url.endsWith('/upload-video')){uploadRecord={request_id:data.get('request_id'),status:'submitted',asset_status:'Active',uri:'asset://asset-videoNEW123',name:data.get('name')};body=uploadRecord;}
      else if(url.endsWith('/generate')){
        if(lost){lost=false;throw Error('响应丢失');}
        body={id:'output',kind:`wardrobe_generate_${mode}`,status:'succeeded',progress:100,output_url:'/api/jobs/output/file/output',logs:[]};
      }else throw Error('Unexpected POST '+url);
      jobs[body.id]=body;
    }else if(url==='/api/config')body={ark_ready:true,wardrobe_dynamic_white_prompt:'只将主角变白，@视频1为打码视频',wardrobe_dynamic_modes:{dynamic_scene:{workflow_version:oldServer?1:2,white_prompt:'绿底白模，@视频1为打码视频'}}};
    else if(url===`/api/wardrobe-${namespace}/latest-upload`)body=uploadRecord || {request_id:''};
    else if(url.startsWith(`/api/wardrobe-${namespace}/upload-status/`))body=uploadRecord;
    else if(url.startsWith(`/api/wardrobe-${namespace}/latest`))body=restore&&url.endsWith('references')?jobs.restored:{id:''};
    else if(url===`/api/wardrobe-${namespace}/video-library`)body={groups:[{id:'group-123456789',name:'角色组',group_type:'AIGC'},{id:'group-empty123',name:'没有视频的组',group_type:'AIGC'}],assets:[{uri:'asset://asset-video123456',group_id:'group-123456789',name:'运动原片',status:'Active',url:'https://example.com/source.mp4'},{uri:'asset://asset-video789012',group_id:'group-123456789',name:'待审核',status:'Processing'},...(extraVideo?[{uri:'asset://asset-videoNEW123',group_id:'group-123456789',name:'新原片',status:'Active',url:'https://example.com/new-source.mp4'}]:[])]};
    else if(url.startsWith('/api/jobs/'))body=jobs[url.split('/').at(-1)];
    else throw Error('Unexpected GET '+url);
    return {ok:true,status:200,json:async()=>body};
  };
  w.eval(read('web/wardrobe_object.js')); w.eval(read('web/wardrobe.js')); await tick();
  return {w,$,posts,setFile,input,change,lose:()=>{lost=true;},failSelection:()=>{failedSelection=true;},
    confirm:async()=>{$('paidConfirmForm').dispatchEvent(new w.Event('submit',{cancelable:true,bubbles:true}));await tick();},
    close:()=>{assert.deepEqual(errors,[]);w.close();}};
}
async function run(){
  const t=await setup(),{$,w}=t;
  assert.equal($('source').classList.contains('hidden'),true);
  assert.equal($('objectReferenceSource').classList.contains('hidden'),false);
  assert.equal($('objectGenerateBtn').disabled,true);
  t.setFile('objectOriginal','original.mp4');$('objectMosaic').click();await tick();
  assert.equal(t.posts[0].body.get('mode'),'dynamic_object');assert.equal(t.posts[0].body.get('face_score_threshold'),'0.55');
  assert.equal($('objectMakeWhite').disabled,true);
  $('objectMosaicReviewed').checked=true;t.change('objectMosaicReviewed');$('objectMakeWhite').click();await tick();
  assert.equal(t.posts.length,1);await t.confirm();assert.equal(t.posts.length,2);
  t.input('personAsset','asset://asset-person123456');t.setFile('objectClothing');
  $('objectWhiteReviewed').checked=true;t.change('objectWhiteReviewed');t.input('objectPrompt','将杯子换成透明玻璃杯');
  assert.equal($('objectGenerateBtn').disabled,false);
  $('objectSpecial').checked=true;t.change('objectSpecial');assert.equal($('objectGenerateBtn').disabled,true);
  t.setFile('objectItem');t.input('objectPrompt','换成 @');
  assert.equal($('objectMentions').children.length,4);$('objectMentions').children[3].click();
  assert.equal($('objectPrompt').value,'换成 @图片3');assert.equal($('objectGenerateBtn').disabled,false);
  t.lose();$('objectGenerateBtn').click();await tick();await t.confirm();const key=t.posts[2].body.get('request_id');
  $('objectGenerateBtn').click();await tick();await t.confirm();assert.equal(t.posts[3].body.get('request_id'),key);
  assert.equal(t.posts[3].body.get('person_asset'),'asset://asset-person123456');assert.equal(t.posts[3].body.has('clothing_image'),true);
  t.input('objectThreshold','.35');assert.equal($('objectGenerateBtn').disabled,true);assert.equal($('objectMakeWhite').disabled,true);
  w.document.querySelector('[data-object-workflow="library"]').click();await tick();
  assert.equal($('objectActorReferences').classList.contains('hidden'),true);assert.equal($('objectPrepare').classList.contains('hidden'),true);
  assert.equal($('objectVideoAssets').children.length,0);assert.equal($('objectVideoAsset').disabled,true);
  t.input('objectVideoGroup','group-empty123');t.change('objectVideoGroup');assert.equal($('objectVideoAsset').disabled,true);
  t.input('objectVideoGroup','group-123456789');t.change('objectVideoGroup');assert.equal($('objectVideoAsset').options.length,3);
  t.input('objectVideoAsset','asset://asset-video789012');t.change('objectVideoAsset');assert.equal($('objectVideoAssets').children[0].querySelector('button').disabled,true);
  t.input('objectVideoAsset','asset://asset-video123456');t.change('objectVideoAsset');
  assert.equal($('objectVideoAssets').children.length,1);assert.match($('objectVideoAssets').querySelector('video').src,/source.mp4$/);
  const preview=$('objectVideoAssets').querySelector('video');
  assert.equal(preview.autoplay,true);assert.equal(preview.muted,true);assert.equal(preview.preload,'auto');
  assert.equal(preview.playsInline,true);
  assert.equal($('objectNewVideoUpload').classList.contains('hidden'),true,'upload form is hidden in library browsing mode');
  assert.equal($('objectNewVideoUpload').previousElementSibling.classList.contains('generation-settings'),true);
  preview.dispatchEvent(new w.Event('loadeddata'));assert.match($('objectVideoAssets').textContent,/画面已加载/);
  $('objectRefreshVideos').click();await tick();assert.equal($('objectVideoAssets').querySelector('video'),preview,'status refresh must not recreate a playing preview');
  preview.dispatchEvent(new w.Event('error'));assert.match($('objectVideoAssets').textContent,/预览加载失败/);
  $('objectRefreshVideos').click();await tick();assert.notEqual($('objectVideoAssets').querySelector('video'),preview,'a failed preview must reload on refresh');
  $('objectVideoAssets').children[0].querySelector('button').click();await tick();
  t.input('objectPrompt','将杯子换成玻璃杯');assert.equal($('objectBindings').children.length,1);assert.equal($('objectGenerateBtn').disabled,false);
  $('objectGenerateBtn').click();await tick();await t.confirm();let submitted=t.posts.at(-1).body;
  assert.equal(submitted.has('person_asset'),false);assert.equal(submitted.has('clothing_image'),false);assert.equal(submitted.has('replacement_image'),false);
  t.setFile('objectItem');t.input('objectPrompt','替换为 @');assert.equal($('objectMentions').children.length,2);
  $('objectMentions').children[1].click();assert.equal($('objectPrompt').value,'替换为 @图片1');
  $('objectClearItem').click();assert.equal($('objectGenerateBtn').disabled,true,'removed image mention must not submit');
  t.input('objectPrompt','将杯子换成玻璃杯');assert.equal($('objectGenerateBtn').disabled,false);
  t.input('objectVideoGroup','group-empty123');t.change('objectVideoGroup');assert.equal($('objectGenerateBtn').disabled,true,'changing group must invalidate the previous bound video');
  assert.equal($('objectVideoName').closest('#objectNewVideoUpload').id,'objectNewVideoUpload');
  t.close();
  const r=await setup({restore:true});assert.equal(r.$('objectThreshold').value,'0.4');
  assert.equal(r.$('objectGenerateBtn').disabled,true,'restored white needs explicit review and references');
  r.setFile('objectWhiteUpload','white.mp4');r.$('objectImportWhite').click();await tick();
  assert.equal(r.posts.length,1);assert.match(r.posts[0].url,/import-white$/);assert.equal(r.$('paidConfirmModal').hidden,true);
  r.close();console.log('Object workflows: masking, white import, two reference layouts, video preview/select, @ binding, special image, retry and restore passed.');
  const quota=await setup({uploadRecord:{request_id:'quota-record-000001',status:'quota_full',error:'角色库共享素材额度已满，本次新增入库被拒绝。'}});
  assert.match(quota.$('objectUploadStatus').textContent,/共享素材额度已满/);
  assert.doesNotMatch(quota.$('objectUploadStatus').textContent,/未确认/);
  assert.equal(quota.posts.length,0);quota.close();
  const reused=await setup({uploadRecord:{request_id:'reuse-record-000001',status:'reused',asset_status:'Active',uri:'asset://asset-video123456',existing_name:'原片',deduplicated:true}});
  assert.match(reused.$('objectUploadStatus').textContent,/内容完全一致/);
  assert.equal(reused.$('objectReuseVideo').classList.contains('hidden'),false);
  reused.$('objectReuseVideo').click();await tick();
  assert.equal(reused.posts[0].url,'/api/wardrobe-object/select-video');reused.close();
  console.log('Upload quota recovery: definitive rejection, no automatic POST, verified duplicate reuse passed.');
}
module.exports={setup,tick};
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});
