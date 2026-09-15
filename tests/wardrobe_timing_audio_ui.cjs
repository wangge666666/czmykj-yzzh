// Local DOM checks; no cloud submissions or remote assets.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const read=file=>fs.readFileSync(path.join(__dirname,'..',file),'utf8');
const tick=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
(async()=>{
  const dom=new JSDOM('<section id="source"></section><section><video id="whiteVideo"></video></section>',{url:'http://localhost',runScripts:'outside-only'});
  const w=dom.window,d=w.document,selected=[],posts=[];
  w.eval(read('web/wardrobe_segments.js'));w.eval(read('web/wardrobe_audio.js'));
  const data={group_id:'group',duration:29,current:1,output_url:'',segments:[
    {job_id:'first',index:1,start:0,end:15,duration:15,finished:true},
    {job_id:'second',index:2,start:15,end:29,duration:14,finished:false}]};
  const job={id:'first',kind:'wardrobe_prepare_dynamic',has_white_model:true,wardrobe_segments:data};
  w.fetch=async(url,{body})=>{posts.push({url,id:body.get('source_job_id')});return{ok:true,json:async()=>job};};
  const options={anchor:'#source',onSelect:id=>selected.push(id)};
  w.DepthFlowSegments.render(job,options);
  assert.match(d.querySelector('#wardrobeSegments').textContent,/共 2 段/);
  assert.match(d.querySelector('[data-segment-index="2"]').textContent,/15.000–29.000/);
  d.querySelector('[data-segment-index="2"]').click();assert.deepEqual(selected,['second']);
  assert.equal(d.querySelector('#wardrobeSegments .secondary-button').disabled,true);
  data.current=2;data.segments[1].finished=true;data.output_url='/complete';data.output_revision='2';
  w.DepthFlowSegments.render({...job,id:'second'},options);
  assert.equal(d.querySelector('[data-segment-index="2"]').getAttribute('aria-pressed'),'true');
  const video=d.querySelector('#wardrobeSegments video');
  w.DepthFlowSegments.render({...job,id:'second'},options);
  assert.equal(d.querySelector('#wardrobeSegments video'),video,'polling does not restart preview');
  d.querySelector('#wardrobeSegments .secondary-button').click();await tick();
  assert.deepEqual(posts[0],{url:'/api/wardrobe-swap/segments/assemble',id:'second'});
  w.DepthFlowSegments.render(job,{...options,busy:true});
  assert.equal(d.querySelector('[data-segment-index="1"]').disabled,true);
  w.DepthFlowAudio.render(job,{anchor:'#whiteVideo',onRestore:id=>selected.push(id)});
  const repair=d.querySelector('#wardrobeAudioRepair button');assert.match(repair.textContent,/免费/);
  repair.click();await tick();
  assert.deepEqual(posts[1],{url:'/api/wardrobe-swap/restore-audio',id:'first'});
  assert.match(d.querySelector('#wardrobeAudioRepair p').textContent,/重新生成成片/);
  dom.window.close();
  console.log('Segment selection, current range, completion/merge, stable playback, busy locks and free audio repair passed.');
})().catch(error=>{console.error(error);process.exit(1);});
