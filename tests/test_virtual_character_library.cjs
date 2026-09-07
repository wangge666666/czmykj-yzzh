const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/long_video.js'), 'utf8');
const section = source.slice(source.indexOf('function virtualLibraryOptions'), source.indexOf('function actorTemplate'));
const escape = source.slice(source.indexOf('function escapeHtml'),source.indexOf('function toast'));
function fixture() {
  const state={actorCount:2,libraryLoading:false,characterLibrary:{groups:[],assets:[]}};
  const selectedFiles=Object.freeze(['local-picture']);
  const nodes={};
  for(const id of ['refreshVirtualCharacterLibrary','virtualCharacterLibraryState','personAsset1','personAsset2','personLibrary1','personLibrary2']) nodes['#'+id]={value:'asset://asset-kept',files:selectedFiles};
  const replies=[],calls=[];
  const ctx=vm.createContext({state,$:key=>nodes[key],Date,api:async url=>{calls.push(url);const next=await replies.shift();if(next instanceof Error)throw next;return next;}});
  vm.runInContext(escape+section,ctx);
  return {state,nodes,replies,calls,ctx};
}
test('virtual role picker exposes only Active roles and retains a saved ID awaiting verification',()=>{
  const f=fixture();f.state.characterLibrary={groups:[{id:'g',name:'共享组'}],assets:[{uri:'asset://asset-active',name:'共享人物',group_id:'g',status:'Active'},{uri:'asset://asset-pending',name:'待审核',status:'Processing'}]};
  const options=vm.runInContext('virtualLibraryOptions("asset://asset-kept")',f.ctx);
  assert.match(options,/共享组 · 共享人物/);assert.doesNotMatch(options,/待审核/);assert.match(options,/已保存角色 · 待核验/);
});
test('refresh keeps actor IDs and file selections, rejects duplicate reads, and preserves cards on failure',async()=>{
  const f=fixture();let resolve;f.replies.push(new Promise(done=>resolve=done));
  const first=vm.runInContext('loadCharacterLibrary()',f.ctx);
  await vm.runInContext('loadCharacterLibrary()',f.ctx);assert.equal(f.calls.length,1);
  resolve({assets:[{uri:'asset://asset-active',name:'共享',status:'Active'}],groups:[],message:'已连接公司角色库'});await first;
  assert.match(f.nodes['#personLibrary1'].innerHTML,/共享/);
  assert.equal(f.nodes['#personAsset1'].value,'asset://asset-kept');assert.deepEqual(f.nodes['#personAsset1'].files,['local-picture']);
  const before=f.nodes['#personLibrary1'].innerHTML;f.replies.push(new Error('fixture offline'));
  await vm.runInContext('loadCharacterLibrary()',f.ctx);assert.equal(f.nodes['#personLibrary1'].innerHTML,before);
  assert.match(f.nodes['#virtualCharacterLibraryState'].textContent,/当前选择与本地素材已保留/);
});
