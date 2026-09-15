const assert=require('node:assert/strict');
const {setup,tick}=require('./wardrobe_continuation_ui.cjs');
function files(t,names){Object.defineProperty(t.$('extraReferenceFiles'),'files',{configurable:true,value:names.map(name=>new t.w.File(['image'],name,{type:'image/png'}))});t.$('extraReferenceFiles').dispatchEvent(new t.w.Event('change',{bubbles:true}));}
async function run(){
 for(const workflow of ['library','references']){
  const t=await setup({workflow});
  if(workflow==='references'){
   t.$('mosaicReviewed').checked=true;t.$('personAsset').value='asset://asset-person123456';
   Object.defineProperty(t.$('clothingFile'),'files',{configurable:true,value:[new t.w.File(['image'],'衣服.png',{type:'image/png'})]});
  }
  t.review();assert.equal(t.$('generateBtn').disabled,false,'optional images must not block a ready task');
  files(t,['物品.png','颜色.png','场景.png']);assert.equal(t.$('extraReferenceList').children.length,3);assert.match(t.$('extraReferenceList').querySelector('img').src,/blob:/);
  const first=workflow==='library'?1:3;
  assert.match(t.rows()[0].querySelector('.references').textContent,new RegExp('@图片'+first+' · 物品'));
  t.input(0,'.range-prompt',`替换为 @图片${first}，配色参考 @图片${first+1}，背景参考 @图片${first+2}`);
  t.$('extraReferenceList').children[1].querySelector('button').click();
  assert.match(t.rows()[0].querySelector('.range-prompt').value,/@已移除参考/);assert.match(t.rows()[0].querySelector('.range-prompt').value,new RegExp('背景参考 @图片'+(first+1)));
  assert.equal(t.$('generateBtn').disabled,true,'removed reference must not silently bind another image');
  t.input(0,'.range-prompt',`替换为 @图片${first}，背景参考 @图片${first+1}`);
  t.$('generateBtn').click();await tick();await t.confirm();
  const body=t.posts.at(-1).body;assert.deepEqual(body.getAll('extra_images').map(file=>file.name),['物品.png','场景.png']);
  assert.deepEqual(JSON.parse(body.get('extra_references')),[{upload:0},{upload:1}]);assert.match(t.$('extraReferenceList').querySelector('img').src,/\/extras\/0$/);
  t.close();
 }
 const p=await setup({workflow:'library',partial:true});p.review();assert.match(p.$('generateBtn').textContent,/完整改写视频/);files(p,['物品.png']);assert.doesNotMatch(p.$('generateBtn').textContent,/剩余 1 段/);
 files(p,Array.from({length:7},(_,i)=>i+'.png'));assert.equal(p.$('extraReferenceList').children.length,1);assert.match(p.$('notice').textContent,/最多添加 7/);p.close();
 console.log('Optional references: no-upload path, multi-image previews, both material binding layouts, safe removal/renumbering, real FormData order, restored previews, retry invalidation and maximum count passed.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
