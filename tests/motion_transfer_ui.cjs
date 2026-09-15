// Run with Node and jsdom on NODE_PATH. All network calls are fixture responses.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web/motion_transfer.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'web/motion_transfer.js'), 'utf8');
const key = 'depthflow.motionTransfer';
const clone = value => JSON.parse(JSON.stringify(value));
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const actor = (id, status = 'Active') => ({ id, uri: `asset://${id}`, name: id, status, asset_type: 'Image', group_id: 'group-existing', url: `/fixtures/${id}.png` });
const project = (id, roles = []) => ({
  id, status: 'succeeded', stage: '就绪', progress: 100, logs: [], outputs: [], request_ids: [],
  source_origin: 'white', white_url: '/fixtures/white.mp4',
  draft: { mode: 'cast', roles, scene_url: '/fixtures/scene.png', prompt: '参考@视频1，在@图片1中表演。' },
});
const role = a => ({ uri: a.uri, name: a.name, role: '原片左侧人物' });

function setup({ saved = {}, jobs, library, initial = 'project-a' } = {}) {
  jobs ||= { 'project-a': project('project-a'), 'project-b': project('project-b') };
  library ||= { configured: true, upload_ready: true, groups: [{ id: 'group-existing', name: '已有组', group_type: 'AIGC' }, { id: 'group-real', name: '真人核验组', group_type: 'Human' }], assets: [] };
  const dom = new JSDOM(html, { url: 'http://localhost/projects/motion-transfer', runScripts: 'outside-only' });
  const w = dom.window, $ = id => w.document.getElementById(id), errors = [], posts = [], timers = new Map();
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.URL.createObjectURL = () => 'blob:uploaded-character'; w.URL.revokeObjectURL = () => {};
  w.addEventListener('error', e => errors.push(e.error));
  let timerId = 0, releaseUpload, uploadFailure, failLibrary = false;
  w.setTimeout = (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; };
  w.clearTimeout = id => timers.delete(id);
  w.localStorage.setItem(key, initial);
  for (const [name, value] of Object.entries(saved)) w.localStorage.setItem(name, value);
  w.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push({ url, body: options.body });
      if (url === '/api/character-library/groups') {
        const data = JSON.parse(options.body);
        library.groups.push({ id: 'group-created', name: data.name, group_type: 'AIGC' });
        return { ok: true, json: async () => ({ group_id: 'group-created' }) };
      }
      if (url === '/api/character-library/assets') {
        return new Promise(resolve => { releaseUpload = () => {
          if (uploadFailure) return resolve({ ok: false, status: 400, json: async () => ({ error: uploadFailure }) });
          const a = actor(`uploaded-${posts.filter(p => p.url.endsWith('/assets')).length}`, 'Processing');
          a.name = options.body.get('name'); a.group_id = options.body.get('group_id'); library.assets.push(a);
          resolve({ ok: true, json: async () => ({ uri: a.uri, asset_id: a.id, status: a.status }) });
        }; });
      }
      if (url.endsWith('/draft') || url.endsWith('/generate')) {
        const data = options.body, job = jobs[data.get('project_id')];
        Object.assign(job.draft, { mode: data.get('mode'), roles: JSON.parse(data.get('roles')), prompt: data.get('prompt') });
        return { ok: true, json: async () => clone(job) };
      }
      throw new Error(`Unexpected POST ${url}`);
    }
    if (url === '/api/character-library') {
      if (failLibrary) throw new Error('网络暂不可用');
      return { ok: true, json: async () => clone(library) };
    }
    return { ok: true, json: async () => url.endsWith('/projects') ? { projects: Object.values(jobs) } : clone(jobs[url.split('/').at(-1)] || jobs[initial]) };
  };
  const files = new WeakMap();
  const valueDescriptor = Object.getOwnPropertyDescriptor(w.HTMLInputElement.prototype, 'value');
  for (const el of w.document.querySelectorAll('input[type=file]')) {
    Object.defineProperty(el, 'files', { get: () => files.get(el) || [] });
    Object.defineProperty(el, 'value', { get() { return valueDescriptor.get.call(this); }, set(v) { valueDescriptor.set.call(this, v); if (!v) files.delete(this); } });
  }
  const change = id => $(id).dispatchEvent(new w.Event('change', { bubbles: true }));
  const input = (id, value) => { $(id).value = value; $(id).dispatchEvent(new w.Event('input', { bubbles: true })); };
  const setFile = (name = '新人物.png', type = 'image/png', size = 100) => {
    const file = new w.File(['image fixture'], name, { type });
    Object.defineProperty(file, 'size', { value: size }); files.set($('roleUploadFile'), [file]); change('roleUploadFile');
  };
  const prepareUpload = async () => {
    $('uploadRole').click(); await tick(); setFile(); $('roleUploadConsent').checked = true; change('roleUploadConsent');
  };
  w.eval(script);
  return { w, $, library, jobs, posts, timers, change, input, setFile, prepareUpload,
    release: () => releaseUpload(), failure: message => { uploadFailure = message; }, offline: value => { failLibrary = value; },
    storage: () => Object.fromEntries(Object.keys(w.localStorage).map(k => [k, w.localStorage.getItem(k)])),
    close: () => { assert.deepEqual(errors, []); w.close(); },
  };
}

async function run() {
  const a = actor('existing');
  const t = setup({ jobs: { 'project-a': project('project-a', [role(a)]), 'project-b': project('project-b') } });
  const { $, w } = t; t.library.assets.push(a); await tick();
  await t.prepareUpload();
  assert.equal($('roleUploadDialog').open, true);
  assert.equal($('roleUploadPreview').getAttribute('src'), 'blob:uploaded-character');
  $('roleUploadPreviewButton').click(); assert.equal($('imageDialog').open, true); $('closeImage').click();
  assert.equal($('roleUploadName').value, '新人物');
  assert.equal($('roleUploadGroup').value, 'group-existing');
  assert.equal($('roleUploadGroup').querySelector('[value="group-real"]'), null, 'upload uses AIGC groups');
  t.setFile('bad.bmp', 'image/bmp'); assert.equal($('submitRoleUpload').disabled, true); assert.match($('roleUploadReason').textContent, /只支持/);
  t.setFile('huge.png', 'image/png', 31 * 1024 * 1024); assert.match($('roleUploadReason').textContent, /30 MB/);
  t.setFile(); assert.equal($('submitRoleUpload').disabled, true, 'changing image requires authorization confirmation');
  t.input('roleNewGroupName', '迁移人物组'); $('createRoleGroup').click(); await tick();
  assert.equal($('roleUploadGroup').value, 'group-created');
  $('roleUploadConsent').checked = true; t.change('roleUploadConsent');
  $('submitRoleUpload').click(); $('submitRoleUpload').click(); await tick();
  assert.equal(t.posts.filter(p => p.url.endsWith('/assets')).length, 1, 'double click uploads only once');
  assert.equal($('newProject').disabled, true); assert.equal($('closeRoleUpload').disabled, true);
  const cancel = new w.Event('cancel', { cancelable: true }); $('roleUploadDialog').dispatchEvent(cancel); assert.equal(cancel.defaultPrevented, true);
  const body = t.posts.find(p => p.url.endsWith('/assets')).body;
  assert.equal(body.get('group_id'), 'group-created'); assert.equal(body.get('asset_file').name, '新人物.png'); assert.equal(body.get('name'), '新人物');
  t.release(); await tick();
  assert.equal($('submitRoleUpload').disabled, true, 'successful upload clears file');
  assert.match($('uploadList').textContent, /正在入库审核/); assert.equal($('selectedRoles').children.length, 1);
  assert.ok([...t.timers.values()].some(timer => timer.delay === 15000), 'review polling scheduled');
  $('closeRoleUpload').click();
  t.library.assets.at(-1).status = 'Active'; $('refreshUploads').click(); await tick();
  assert.equal($('selectedRoles').children.length, 2); assert.match($('selectedRoles').textContent, /@图片3 · 新人物/);
  assert.equal($('selectedRoles').querySelector('[data-preview-role="1"] img').getAttribute('src'), '/fixtures/uploaded-1.png');
  $('prompt').value = '使用@图'; $('prompt').setSelectionRange(4, 4); t.input('prompt', $('prompt').value);
  assert.match($('mentionMenu').textContent, /@图片3 · 新人物/);
  $('saveDraft').click(); await tick();
  const draftPost = t.posts.find(p => p.url.endsWith('/draft'));
  assert.equal(JSON.parse(draftPost.body.get('roles'))[1].uri, 'asset://uploaded-1', 'generation draft sends the approved asset URI');
  assert.equal(t.posts.some(p => p.url.endsWith('/generate')), false, 'review never triggers paid generation');
  $('selectedRoles').querySelector('[data-remove-role="1"]').click(); $('refreshUploads').click(); await tick();
  assert.equal($('selectedRoles').children.length, 1, 'removed upload does not reappear on refresh');

  await t.prepareUpload(); $('submitRoleUpload').click(); t.release(); await tick(); $('closeRoleUpload').click();
  const saved = t.storage();
  t.library.assets.at(-1).status = 'Failed'; $('refreshUploads').click(); await tick();
  assert.match($('uploadList').textContent, /审核未通过/); assert.equal($('selectedRoles').children.length, 1);
  // Reload the pending snapshot; approval must return to its original project only.
  t.library.assets.at(-1).status = 'Active';
  const reloaded = setup({ saved, jobs: t.jobs, library: t.library, initial: 'project-a' }); await tick();
  assert.equal(reloaded.$('selectedRoles').children.length, 3, 'pending approval survives refresh');
  reloaded.$('history').value = 'project-b'; reloaded.change('history'); await tick();
  assert.equal(reloaded.$('selectedRoles').children.length, 0, 'no upload added to another project'); reloaded.close();

  await t.prepareUpload(); t.failure('图片无法识别，请更换'); $('submitRoleUpload').click(); t.release(); await tick();
  assert.match($('roleUploadStatus').textContent, /图片无法识别/); assert.equal($('roleUploadFile').files.length, 1, 'failed upload keeps form');
  assert.equal($('newProject').disabled, false); t.close();

  const many = Array.from({ length: 8 }, (_, i) => actor(`person-${i}`));
  const full = setup({ jobs: { 'project-a': project('project-a', many.map(role)) }, saved: { [`${key}.uploads`]: JSON.stringify([{ context: 'project-a', uri: 'asset://new-person', name: '新人', status: 'Processing', autoAdd: true, watch: true }]) } });
  full.library.assets.push(...many, actor('new-person')); await tick();
  assert.equal(full.$('selectedRoles').children.length, 8); assert.equal(full.$('uploadRole').disabled, true); assert.match(full.$('uploadList').textContent, /已满 8 位/);
  full.$('selectedRoles').querySelector('[data-remove-role="0"]').click(); full.$('uploadList').querySelector('[data-upload-select]').click();
  assert.equal(full.$('selectedRoles').children.length, 8); assert.match(full.$('selectedRoles').textContent, /new-person/); full.close();

  const pending = { context: 'project-a', uri: 'asset://reviewing', name: '待审核', status: 'Processing', autoAdd: true, watch: true };
  const stale = setup({ saved: { [`${key}.uploads`]: JSON.stringify([pending]) } });
  stale.library.assets.push(actor('reviewing')); stale.library.stale = true; await tick();
  assert.equal(stale.$('selectedRoles').children.length, 0, 'stale cache cannot approve a new upload');
  stale.offline(true); stale.$('refreshUploads').click(); await tick(); assert.match(stale.$('uploadReviewState').textContent, /网络暂不可用/);
  stale.offline(false); stale.library.stale = false; stale.jobs['project-a'].status = 'running';
  stale.$('refreshJob').click(); await tick(); stale.$('refreshUploads').click(); await tick();
  assert.equal(stale.$('selectedRoles').children.length, 0, 'running generation keeps role references fixed');
  stale.jobs['project-a'].status = 'succeeded'; stale.$('refreshJob').click(); await tick();
  assert.equal(stale.$('selectedRoles').children.length, 1, 'deferred approval attaches when job becomes idle'); stale.close();
  console.log('PASS: new character preview, validation, group creation, upload payload, duplicate submission guard, review polling, auto-selection, @ image references, draft asset URI, removal, review failure, reload recovery, project isolation, upload errors, 8-role limit, stale/offline review and generation lock. No external uploads or model calls.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
