const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..'),tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
async function setup({legacy=false,partial=false,workflow='library'}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'web/wardrobe_continuation.html'),'utf8'),{url:'http://localhost/projects/wardrobe-continuation',runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id),posts=[],errors=[];let lose=false;
 w.addEventListener('error',e=>errors.push(e.error));w.setTimeout=()=>1;w.clearTimeout=()=>{};
 w.HTMLMediaElement.prototype.load=function(){};w.HTMLElement.prototype.scrollIntoView=function(){};
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.URL.createObjectURL=()=> 'blob:video';w.URL.revokeObjectURL=()=>{};
 const values=[{start:2,end:4,prompt:'人物转身'},{start:7,end:9,prompt:'拿起杯子'}];
 const withMedia=(s,i)=>({...s,has_after:true,duration:s.end-s.start,generation_seconds:4,context_url:'/context'+i,start_frame_image_url:'/start'+i,end_frame_image_url:'/end'+i,status:'prepared',reusable:partial&&i===0,submitted_prompt:s.prompt,...(partial&&i===0?{output_url:'/clip0'}:{})});
 let job={id:'saved',workflow_version:legacy?1:2,status:partial?'failed':'succeeded',stage:'已准备',progress:100,logs:[],source_url:'/source',segments:legacy?undefined:values.map(withMedia),settings:{source_duration:10,fps:30,keep_seconds:5,prompt:'旧版剧情'},last_generation:{resolution:'720p',generate_audio:true}};
 if(workflow&&!legacy){job.rewrite_workflow=workflow;job.video_asset='asset://asset-video123456';job.video_group_id='group-123456789';if(workflow==='references')job.mosaic_url='/mosaic';}
 w.fetch=async(url,options={})=>{
  if(url.endsWith('/video-library'))return {ok:true,json:async()=>({groups:[{id:'group-123456789',name:'素材组'}],assets:[{uri:'asset://asset-video123456',group_id:'group-123456789',name:'原片',url:'https://example.test/original.mp4',status:'Active'},{uri:'asset://asset-videoNEW123',group_id:'group-123456789',name:'新原片',url:'https://example.test/new.mp4',status:'Active'}]})};
  if(url.includes('/upload-status/'))return {ok:true,json:async()=>({asset_status:'Active',uri:'asset://asset-videoNEW123'})};
  if(options.method==='POST'){
   posts.push({url,body:options.body});
   if(url.endsWith('/generate')){if(lose){lose=false;throw Error('响应中断');}const choices=JSON.parse(options.body.get('extra_references')||'[]'),uploads=options.body.getAll('extra_images');job={...job,generation_mode:'full_video',person_asset:options.body.get('person_asset')||'',clothing_url:options.body.has('clothing_image')?'/clothing':job.clothing_url,extra_references:choices.map((item,index)=>({...('upload' in item?{name:uploads[item.upload].name,sha256:uploads[item.upload].name}:job.extra_references[item.keep]),index,source_job_id:job.id,url:'/extras/'+index})),status:'succeeded',output_url:'/final.mp4',revision:'attempt1',segments:JSON.parse(options.body.get('segments')).map(s=>({...s,duration:s.end-s.start,has_after:s.end<10,generation_seconds:-1}))};}
   else if(url.endsWith('/prepare'))job={...job,workflow_version:2,id:'new-source',segments:JSON.parse(options.body.get('segments')).sort((a,b)=>a.start-b.start).map(withMedia)};
   else if(url.endsWith('/source'))job={...job,id:'bound-source',rewrite_workflow:options.body.get('rewrite_workflow'),video_asset:options.body.get('video_asset'),mosaic_url:options.body.get('rewrite_workflow')==='references'?'/new-mosaic':'',segments:[],settings:{source_duration:10,fps:30}};
  }
  return {ok:true,json:async()=>structuredClone(job)};
 };
 w.eval(fs.readFileSync(path.join(root,'web/wardrobe_continuation.js'),'utf8'));await tick();
 const rows=()=>[...$('rangeList').querySelectorAll('.range-card')];
 const input=(row,selector,value)=>{const node=rows()[row].querySelector(selector);node.value=value;node.dispatchEvent(new w.Event('input',{bubbles:true}));};
 return {w,$,posts,rows,input,lose:()=>lose=true,confirm:async()=>{$('paidDialog').returnValue='confirm';$('paidDialog').open=false;$('paidDialog').dispatchEvent(new w.Event('close'));await tick();},review:()=>{$('generateAudio').dispatchEvent(new w.Event('input'));},close:()=>{assert.deepEqual(errors,[]);w.close();}};
}
async function run(){
 const t=await setup(),{$}=t;
 assert.equal($('prepareBtn'),null);assert.equal($('rangesReviewed'),null);assert.equal(t.rows()[0].querySelector('.range-previews'),null);
 assert.equal($('generateBtn').disabled,false,'ready source can generate without any boundary preparation');
 t.input(0,'.range-start','1');assert.equal($('generateBtn').disabled,false,'changing seconds does not require preparing clips');
 t.input(1,'.range-prompt','换成 @图片3');assert.equal($('generateBtn').disabled,true);t.input(1,'.range-prompt','拿起杯子');
 $('generateBtn').click();await tick();assert.equal(t.posts.length,0);assert.match($('paidCost').textContent,/1 次/);assert.doesNotMatch($('paidRanges').textContent,/裁剪|拼接|取前/);
 t.lose();await t.confirm();const key=t.posts[0].body.get('request_id');assert.equal(t.posts[0].body.get('generation_mode'),'full_video');assert.equal(t.posts[0].body.has('ranges_reviewed'),false);
 $('generateBtn').click();await tick();await t.confirm();assert.equal(t.posts[1].body.get('request_id'),key);assert.equal(JSON.parse(t.posts[1].body.get('segments')).length,2);
 assert.equal($('outputCard').classList.contains('hidden'),false);assert.equal($('segmentResults').children.length,0);
 t.input(1,'.range-start','3');assert.equal($('generateBtn').disabled,true);assert.match($('generateReason').textContent,/重叠/);
 t.input(1,'.range-start','7');assert.equal($('generateBtn').disabled,false);t.close();
 const legacy=await setup({legacy:true});assert.equal(legacy.$('legacyNote').classList.contains('hidden'),false);assert.equal(legacy.$('generateBtn').disabled,true);assert.equal(legacy.$('prepareBtn'),null);legacy.close();
 console.log('Direct rewrite UI: no boundary step, direct generation after time edits, one paid request for multiple ranges, full output, idempotent retry and legacy history passed.');
}
module.exports={setup,tick};
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});
