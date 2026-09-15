const assert=require('node:assert/strict');
const {setup,tick}=require('./wardrobe_object_ui.cjs');

async function run(){
  for(const mode of ['dynamic_object','dynamic_scene']){
    const t=await setup({mode,workflow:'library',extraVideo:true,uploadRecord:{request_id:'old-upload-record-0001',status:'reused',asset_status:'Active',uri:'asset://asset-video123456',existing_name:'旧原片',deduplicated:true}}),{$}=t;
    const namespace=mode==='dynamic_scene'?'scene':'object';
    t.input('objectVideoGroup','group-123456789');t.change('objectVideoGroup');
    t.input('objectVideoAsset','asset://asset-video123456');t.change('objectVideoAsset');
    $('objectVideoAssets').querySelector('button').click();await tick();
    t.input('objectPrompt','保持动作，更换指定目标');assert.equal($('objectGenerateBtn').disabled,false);
    assert.equal($('objectVideoAssets').querySelector('button').textContent,'当前已使用此原片');
    $('objectChooseUpload').click();await tick();
    assert.equal($('objectNewVideoUpload').classList.contains('hidden'),false);
    assert.equal($('objectVideoAssetField').classList.contains('hidden'),true);
    assert.equal($('objectGenerateBtn').disabled,true,'pending replacement must not submit the old video');
    assert.equal($('objectReuseVideo').classList.contains('hidden'),true,'old receipt must not look like the newly selected file');
    t.setFile('objectLibraryUpload','新的原片.mov');
    assert.equal($('objectVideoName').value,'新的原片');assert.equal($('objectUploadVideo').disabled,false);
    assert.match($('objectUploadPreview').src,/blob:/);
    t.setFile('objectLibraryUpload','另一个视频.mp4');assert.equal($('objectVideoName').value,'另一个视频');
    t.input('objectVideoName','自定义名称');t.setFile('objectLibraryUpload','第三份.mp4');assert.equal($('objectVideoName').value,'自定义名称');
    $('objectCancelVideoChange').click();await tick();
    assert.equal($('objectGenerateBtn').disabled,false);
    assert.equal($('objectVideoAsset').value,'asset://asset-video123456');
    assert.equal($('objectPrompt').value,'保持动作，更换指定目标');
    $('objectChooseLibrary').click();await tick();
    t.input('objectVideoAsset','asset://asset-videoNEW123');t.change('objectVideoAsset');
    assert.match($('objectVideoAssets').querySelector('video').src,/new-source.mp4$/);
    t.failSelection();$('objectVideoAssets').querySelector('button').click();await tick();
    assert.match($('toast').textContent,/暂不可用/);assert.equal($('objectGenerateBtn').disabled,true);
    $('objectCancelVideoChange').click();assert.equal($('objectVideoAsset').value,'asset://asset-video123456');
    $('objectChooseUpload').click();await tick();
    $('objectUploadVideo').click();await tick();
    let post=t.posts.at(-1);assert.equal(post.url,`/api/wardrobe-${namespace}/upload-video`);
    assert.equal(post.body.get('reference_video').name,'第三份.mp4');assert.equal(post.body.get('name'),'自定义名称');
    assert.equal($('objectReuseVideo').classList.contains('hidden'),false);
    $('objectReuseVideo').click();await tick();
    assert.equal(t.posts.at(-1).body.get('video_asset'),'asset://asset-videoNEW123');
    assert.equal($('objectVideoAsset').value,'asset://asset-videoNEW123');
    assert.match($('objectSelectedVideo').textContent,/新原片/);
    assert.equal($('objectGenerateBtn').disabled,false);
    assert.equal($('objectPrompt').value,'保持动作，更换指定目标');
    $('objectGenerateBtn').click();await tick();await t.confirm();
    assert.equal(t.posts.at(-1).body.get('source_job_id'),'selected-new','generation must use the new bound original');
    t.close();
  }
  console.log('Both video-library workflows: visible upload/switch actions, auto naming, preview, cancel, failed replacement rollback, upload receipt isolation and new original binding passed.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
