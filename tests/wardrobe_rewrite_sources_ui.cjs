const assert=require('node:assert/strict');
const {setup,tick}=require('./wardrobe_continuation_ui.cjs');
function change(t,id,value,type='input'){t.$(id).value=value;t.$(id).dispatchEvent(new t.w.Event(type,{bubbles:true}));}
function file(t,id,name,type){Object.defineProperty(t.$(id),'files',{configurable:true,value:[new t.w.File(['content'],name,{type})]});t.$(id).dispatchEvent(new t.w.Event('change',{bubbles:true}));}
async function run(){
 const t=await setup({workflow:'library'});
 assert.equal(t.$('videoLibraryPane').classList.contains('hidden'),false);
 assert.equal(t.$('actorReferencePane').classList.contains('hidden'),true);
 assert.match(t.rows()[0].querySelector('.references').textContent,/@视频1 · 角色库完整原片/);
 assert.doesNotMatch(t.rows()[0].querySelector('.references').textContent,/@图片/);
 t.review();assert.equal(t.$('generateBtn').disabled,false);
 t.$('chooseUpload').click();await tick();assert.equal(t.$('libraryUploadPane').classList.contains('hidden'),false);assert.equal(t.$('generateBtn').disabled,true);
 file(t,'libraryVideoFile','新原片.mp4','video/mp4');assert.equal(t.$('videoName').value,'新原片');assert.match(t.$('uploadVideoPreview').src,/blob:/);
 t.$('cancelSource').click();t.review();assert.equal(t.$('generateBtn').disabled,false);
 t.$('chooseLibrary').click();await tick();change(t,'videoAsset','asset://asset-videoNEW123','change');assert.match(t.$('candidateVideo').src,/new.mp4/);
 t.$('useVideoBtn').click();await tick();assert.equal(t.posts.at(-1).body.get('video_asset'),'asset://asset-videoNEW123');assert.equal(t.posts.at(-1).body.get('rewrite_workflow'),'library');
 assert.equal(t.$('prepareBtn'),null);t.input(0,'.range-prompt','参考 @视频1 将人物改为挥手');t.review();assert.equal(t.$('generateBtn').disabled,false);
 t.$('generateBtn').click();await tick();await t.confirm();assert.equal(t.posts.at(-1).body.has('person_asset'),false);assert.equal(t.posts.at(-1).body.has('clothing_image'),false);
 t.close();
 const r=await setup({workflow:'references'});
 assert.equal(r.$('mosaicReviewPane').classList.contains('hidden'),false);assert.equal(r.$('actorReferencePane').classList.contains('hidden'),false);
 r.review();assert.match(r.$('generateReason').textContent,/打码/);r.$('mosaicReviewed').checked=true;r.$('mosaicReviewed').dispatchEvent(new r.w.Event('input'));
 assert.match(r.$('generateReason').textContent,/人物/);change(r,'personAsset','asset://asset-person123456');assert.match(r.$('generateReason').textContent,/服装/);
 file(r,'clothingFile','衣服.png','image/png');assert.equal(r.$('generateBtn').disabled,false);assert.match(r.$('clothingPreview').src,/blob:/);
 assert.match(r.rows()[0].querySelector('.references').textContent,/@图片1 · 人物参考/);assert.match(r.rows()[0].querySelector('.references').textContent,/@图片2 · 服装参考/);
 r.input(0,'.range-prompt','根据 @视频1 和 @图片1 @图片2 改为转身');r.$('generateBtn').click();await tick();await r.confirm();
 assert.equal(r.posts.at(-1).body.get('person_asset'),'asset://asset-person123456');assert.equal(r.posts.at(-1).body.get('clothing_image').name,'衣服.png');assert.equal(r.posts.at(-1).body.get('mosaic_reviewed'),'true');
 change(r,'faceThreshold','.45');assert.equal(r.$('generateBtn').disabled,true);assert.equal(r.$('mosaicReviewPane').classList.contains('hidden'),true);
 r.$('mosaicBtn').click();await tick();assert.equal(Number(r.posts.at(-1).body.get('face_score_threshold')),.45);assert.equal(r.posts.at(-1).body.get('rewrite_workflow'),'references');
 assert.equal(r.$('mosaicReviewed').checked,false);r.close();
 console.log('Rewrite material UI: library selection, preview and replacement, upload naming, cancel, masked-video review, character/clothing references and actual submission binding passed.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
