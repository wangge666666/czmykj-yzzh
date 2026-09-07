'use strict';

// Offline interaction regression: run the complete portal script without a
// browser, real account, filesystem settings, or provider connection.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../yzzh_local/web/portal.js'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(resolve => setImmediate(resolve));
const settings = (hasKey, extra = {}) => ({
  configured: hasKey, fields: {ARK_API_KEY: hasKey}, model: 'fixture-model',
  bucket: '', image_model: '', performance_model: '', upload_mode: 'temporary',
  session_revision: 'fixture-context', ...extra,
});
const platformSettings = (extra = {}) => ({
  mode: 'platform', owner_id: 71, session_revision: 'fixture-context', ready: true, configured: true, fields: {},
  capabilities: {video: true, image: true, analysis: true, assets: true, media: true},
  models: {video: 'fixture-video', white: 'fixture-white', image: 'fixture-image', analysis: 'fixture-analysis'},
  projects: ['wardrobe', 'virtual', 'real'], balance: 120.5, licensed: true, message: '平台服务已就绪', ...extra,
});
const approval = (id, upload = false) => ({
  id, source: upload ? '上传本次白模参考' : '生成白模视频',
  summary: upload ? {destination: 'Litterbox 临时素材托管', bytes: 1048576} :
    {destination: '视频模型服务', cost: '由模型账号按本次请求计费'},
});

function element() {
  const classes = new Set();
  return {
    children: [], dataset: {}, value: '', textContent: '', hidden: false,
    classList: {
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
      contains: name => classes.has(name),
    },
    append(...children) {
      this.children.push(...children);
      if (this.form) {
        const register = child => {
          if (child.name) this.form.elements[child.name] = child;
          for (const nested of child.children || []) register(nested);
        };
        children.forEach(register);
      }
    },
    replaceChildren(...children) { this.children = children; },
    prepend(...children) { this.children.unshift(...children); },
  };
}

async function fixture(options = {}) {
  const calls = [], navigations = [], listeners = new Map(), intervals = [];
  const selectors = new Map();
  for (const selector of ['.overlay', '.status', '.account', '.login', '.pending',
    '.settings', '.configured', '.entry', '.login-destination', '.unified-login',
    '.unified-login a', '.tos-fields', '.close', '.fields', '.identity-fields',
    '.model-fields', '.logout', '.readiness-status', '.readiness-checks', '.readiness-check',
    '.settings-view', '.approval-view', '.panel-title', '.billing-info', '.approval-explanation',
    '.platform-status-view', '.platform-status', '.platform-capabilities', '.platform-balance', '.platform-logout',
    '.agent-workbench-link', '.top-status', '.top-account', '.top-expiry', '.top-balance',
    '.top-capabilities', '.top-local', '.top-details', '.top-notice']) selectors.set(selector, element());
  const form = selectors.get('.settings');
  form.elements = {MEDIA_UPLOAD_MODE: Object.assign(element(), {name: 'MEDIA_UPLOAD_MODE'})};
  for (const selector of ['.fields', '.identity-fields', '.model-fields', '.tos-fields']) {
    selectors.get(selector).form = form;
  }
  const shadow = {
    innerHTML: '', activeElement: null,
    querySelector(selector) {
      assert.ok(selectors.has(selector), `Unexpected panel selector: ${selector}`);
      return selectors.get(selector);
    },
  };
  const badge = element();
  badge.textContent = 'API 未配置';
  const selectedFile = Object.freeze({name: 'synthetic-selected.mp4', size: 321});
  const selectedFiles = [selectedFile];
  const page = new Map([
    ['#arkBadge', options.noBadge ? null : badge],
    ['#referenceVideo', Object.assign(element(), {files: selectedFiles})],
    ['#prompt', Object.assign(element(), {value: '保留这段用户已经填写的提示词'})],
    ['#modeInput', Object.assign(element(), {value: 'clothing'})],
    ['meta[name="yzzh-owner"]', {content: '71'}],
    ['meta[name="yzzh-context"]', {content: 'fixture-context'}],
    ['meta[name="yzzh-service-mode"]', {content: options.serviceMode || options.initialSettings?.mode || 'byok'}],
  ]);
  const body = element();
  const document = {
    body, title: 'fixture',
    querySelector: selector => page.get(selector) || null,
    querySelectorAll: () => [],
    createTreeWalker: () => ({nextNode: () => null}),
    getElementById: id => page.get(`#${id}`) || null,
    addEventListener: (name, callback) => listeners.set(name, callback),
    createElement: () => Object.assign(element(), {attachShadow: () => shadow}),
  };
  const location = {
    origin: 'http://127.0.0.1:7871', pathname: options.pathname || '/projects/wardrobe', search: '', hash: '',
    get href() { return this.origin + this.pathname; },
    set href(value) { navigations.push(value); },
    reload: () => navigations.push('reload'),
    assign: value => navigations.push(value), replace: value => navigations.push(value),
  };
  let savedOnce = false, releaseState, releaseReadiness;
  const publicState = {
    health: {login_url: 'https://account.fixture.invalid/api/auth/login'},
    account: {user_id: 71, username: 'fixture', licensed: true, subscription_end: '2030-01-01'},
    settings: options.initialSettings || settings(false),
  };
  const approvals = copy(options.approvals || {pending: [], unresolved: []});
  const response = (status, data) => ({
    ok: status >= 200 && status < 300, status,
    json: async () => copy(data), clone() { return this; },
  });
  const nativeFetch = async (input, init = {}) => {
    const url = new URL(String(input), location.href);
    const method = init.method || 'GET';
    calls.push({path: url.pathname, origin: url.origin, method, body: init.body});
    assert.equal(url.origin, location.origin, 'No remote request is permitted');
    if (url.pathname === '/api/state' && method === 'GET') {
      if (options.pauseStateAfterSave && savedOnce) {
        return new Promise(resolve => { releaseState = () => resolve(response(200, publicState)); });
      }
      return response(200, publicState);
    }
    if (url.pathname === '/_plugin/approvals' && method === 'GET') {
      return response(200, approvals);
    }
    if (url.pathname === '/_plugin/decision' && method === 'POST') {
      const decision = JSON.parse(init.body);
      const index = approvals.pending.findIndex(item => item.id === decision.id);
      assert.notEqual(index, -1, 'Only an explicitly pending request can be decided');
      approvals.pending.splice(index, 1);
      return response(200, {ok: true});
    }
    if (url.pathname === '/_plugin/reconcile' && method === 'POST') {
      if (options.receiptError) return response(404, {error: 'REQUEST_NOT_FOUND'});
      approvals.unresolved.splice(0);
      return response(200, {confirmed:true, task_id:'original-platform-task', message:'平台已确认原视频任务，没有重新生成。'});
    }
    if (url.pathname === '/api/plugin-readiness' && method === 'GET') {
      const result = options.readiness || {ready: true, status: 'ready', checks: [], message: '人脸打码、深度处理、视频工具已就绪。'};
      if (options.pauseReadiness) return new Promise(resolve => {
        releaseReadiness = () => resolve(response(200, result));
      });
      return response(200, result);
    }
    if (url.pathname === '/api/settings' && method === 'POST') {
      if (options.saveError) return response(options.saveStatus || 400, {error: options.saveError});
      const saved = options.savedSettings || settings(true);
      publicState.settings = copy(saved);
      savedOnce = true;
      return response(200, saved);
    }
    if (url.pathname === '/api/logout' && method === 'POST') {
      publicState.account = null;
      return response(200, {logged_out: true});
    }
    throw Error(`Unexpected request: ${method} ${url.pathname}`);
  };
  class Storage {
    constructor() { this.values = new Map(Object.entries(options.initialStorage || {})); }
    getItem(key) { return this.values.get(key) ?? null; }
    setItem(key, value) { this.values.set(key, String(value)); }
    removeItem(key) { this.values.delete(key); }
  }
  class FormData {
    constructor(target) { this.entries = Object.entries(target.elements).map(([name, input]) => [name, input.value]); }
    [Symbol.iterator]() { return this.entries[Symbol.iterator](); }
  }
  const window = {fetch: nativeFetch};
  const sandbox = {window, document, location, Storage, FormData, URL, URLSearchParams,
    NodeFilter: {SHOW_TEXT: 4}, MutationObserver: class { observe() {} },
    Headers, Request, history: {replaceState: (...args) => navigations.push(args)},
    setInterval: callback => { intervals.push(callback); return intervals.length; }};
  vm.runInNewContext(source, sandbox, {filename: 'portal.js'});
  listeners.get('DOMContentLoaded')();
  await settle();
  return {
    badge, form, calls, navigations, shadow, selectors, storage: new Storage(),
    state: () => publicState,
    approvals: () => approvals,
    releaseState() { assert.equal(typeof releaseState, 'function'); releaseState(); },
    releaseReadiness() { assert.equal(typeof releaseReadiness, 'function'); releaseReadiness(); },
    async refresh() { await intervals[0](); },
    async save() {
      form.elements.ARK_API_KEY.value = 'fixture-key-not-real';
      form.elements.TOS_ACCESS_KEY.value = 'fixture-ak-not-real';
      form.elements.TOS_SECRET_KEY.value = 'fixture-sk-not-real';
      await form.onsubmit({preventDefault() {}});
    },
    assertPreserved(expectedNavigations = []) {
      assert.equal(page.get('#referenceVideo').files, selectedFiles);
      assert.equal(page.get('#referenceVideo').files[0], selectedFile);
      assert.equal(page.get('#prompt').value, '保留这段用户已经填写的提示词');
      assert.equal(page.get('#modeInput').value, 'clothing');
      assert.deepEqual(navigations, expectedNavigations);
      assert.ok(calls.every(call => call.origin === location.origin &&
        ['/api/state', '/api/settings', '/api/logout', '/_plugin/approvals', '/_plugin/decision', '/_plugin/reconcile', '/api/plugin-readiness'].includes(call.path)));
    },
  };
}

test('saving Key updates the existing badge before returning without resetting creation inputs', async () => {
  const f = await fixture();
  assert.equal(f.selectors.get('.agent-workbench-link').hidden, false);
  assert.equal(f.badge.classList.contains('ready'), false);
  await f.save();
  f.selectors.get('.close').onclick();
  assert.equal(f.selectors.get('.overlay').hidden, true);
  assert.equal(f.badge.textContent, 'API 已配置');
  assert.equal(f.badge.classList.contains('ready'), true);
  assert.match(f.selectors.get('.status').textContent, /状态已更新/);
  assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 1);
  assert.equal(f.form.elements.ARK_API_KEY.value, '');
  f.assertPreserved();
});

test('failed save cannot report a saved Key or change the badge', async () => {
  const f = await fixture({saveError: 'INVALID_SETTINGS'});
  await f.save();
  assert.equal(f.badge.textContent, 'API 未配置');
  assert.equal(f.badge.classList.contains('ready'), false);
  assert.match(f.selectors.get('.status').textContent, /INVALID_SETTINGS/);
  assert.doesNotMatch(f.selectors.get('.status').textContent, /状态已更新/);
  f.assertPreserved();
});

test('the successful save response updates the badge before the next state response arrives', async () => {
  const f = await fixture({pauseStateAfterSave: true});
  const saving = f.save();
  await settle();
  try {
    assert.equal(f.badge.textContent, 'API 已配置');
    assert.equal(f.badge.classList.contains('ready'), true);
  } finally {
    f.releaseState();
    await saving;
  }
  f.assertPreserved();
});

test('Key readiness follows the public Key flag even when model configuration is incomplete', async () => {
  const f = await fixture({savedSettings: settings(true, {configured: false, model: '', missing: ['ARK_MODEL']})});
  await f.save();
  assert.equal(f.badge.textContent, 'API 已配置');
  assert.equal(f.badge.classList.contains('ready'), true);
  f.assertPreserved();
});

test('refresh removes readiness when the server reports no Key and rejects truthy non-boolean flags', async () => {
  const f = await fixture();
  await f.save();
  for (const value of [false, 'true', 1]) {
    f.state().settings = settings(false, {fields: {ARK_API_KEY: value}});
    await f.refresh();
    assert.equal(f.badge.textContent, 'API 未配置');
    assert.equal(f.badge.classList.contains('ready'), false);
  }
  f.assertPreserved();
});

test('a different account or session cannot update this page with its Key state', async () => {
  const f = await fixture();
  f.state().settings = settings(true);
  f.state().account.user_id = 72;
  await f.refresh();
  assert.equal(f.badge.classList.contains('ready'), false);
  f.state().account.user_id = 71;
  f.state().settings.session_revision = 'another-session';
  await f.refresh();
  assert.equal(f.badge.classList.contains('ready'), false);
  f.assertPreserved();
});

test('pages without the original API badge can still save settings', async () => {
  const f = await fixture({noBadge: true});
  await f.save();
  assert.match(f.selectors.get('.status').textContent, /状态已更新/);
  f.assertPreserved();
});

test('local readiness runs once after login, does not repeat on polling or saving, and supports manual recheck', async () => {
  const f = await fixture();
  const count = () => f.calls.filter(call => call.path === '/api/plugin-readiness').length;
  assert.equal(count(), 1);
  assert.match(f.selectors.get('.readiness-status').textContent, /已就绪/);
  await f.refresh(); await f.refresh(); await f.save();
  assert.equal(count(), 1);
  await f.selectors.get('.readiness-check').onclick();
  assert.equal(count(), 2);
  f.assertPreserved();
});

test('incomplete local basics open settings with repair guidance before the user starts processing', async () => {
  const f = await fixture({pauseReadiness: true, readiness: {ready: false, status: 'incomplete', message: '本地基础功能尚未就绪',
    checks: [{label: '人脸打码', ready: false, message: '基础模型缺失或损坏，请修复'}]}});
  f.selectors.get('.overlay').hidden = true;
  f.releaseReadiness(); await settle();
  assert.equal(f.selectors.get('.overlay').hidden, false);
  assert.match(f.selectors.get('.status').textContent, /本地基础功能尚未就绪/);
  assert.match(f.selectors.get('.readiness-status').textContent, /尚未就绪/);
  assert.match(f.selectors.get('.readiness-checks').children[0].textContent, /基础模型缺失或损坏/);
  f.assertPreserved();
});

test('a running local task defers readiness without opening the settings panel', async () => {
  const f = await fixture({pauseReadiness: true, readiness: {ready: false, status: 'busy', checks: [],
    message: '本地任务正在运行，请完成后点击重新检查。'}});
  f.selectors.get('.overlay').hidden = true;
  f.releaseReadiness(); await settle();
  assert.equal(f.selectors.get('.overlay').hidden, true);
  assert.match(f.selectors.get('.readiness-status').textContent, /重新检查/);
  assert.equal(f.selectors.get('.readiness-check').disabled, false);
  f.assertPreserved();
});

test('an in-flight readiness response cannot restore old readiness after account change or logout', async () => {
  for (const logout of [false, true]) {
    const f = await fixture({pauseReadiness: true});
    if (logout) f.state().account = null;
    else f.state().account.user_id = 72;
    await f.refresh();
    f.releaseReadiness(); await settle();
    assert.doesNotMatch(f.selectors.get('.readiness-status').textContent, /已就绪/);
    assert.equal(f.selectors.get('.readiness-check').disabled, true);
    f.assertPreserved();
  }
});

test('upload permission does not approve the later paid request or save model credentials', async () => {
  const f = await fixture({initialSettings: settings(true),
    approvals: {pending: [approval('fixture-upload', true)], unresolved: []}});
  const decisions = () => f.calls.filter(call => call.path === '/_plugin/decision');
  const findAction = text => {
    const section = f.selectors.get('.pending').children.find(child => child.children?.some(node => node.textContent === text));
    assert.ok(section, `Missing approval section with action: ${text}`);
    return section.children.find(child => child.textContent === text);
  };
  assert.equal(decisions().length, 0, 'Showing an approval must not send it');
  await findAction('同意上传，继续').onclick();
  assert.deepEqual(JSON.parse(decisions()[0].body), {
    owner: 71, session: 'fixture-context', id: 'fixture-upload', approved: true,
  });
  f.approvals().pending.push(approval('fixture-video'));
  await f.refresh();
  assert.equal(decisions().length, 1, 'The next request must remain unapproved');
  await findAction('取消，不发送').onclick();
  assert.deepEqual(JSON.parse(decisions()[1].body), {
    owner: 71, session: 'fixture-context', id: 'fixture-video', approved: false,
  });
  assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 0);
  assert.equal(f.form.elements.ARK_API_KEY.value, '');
  f.assertPreserved();
});

test('a generation approval opens a dedicated confirmation view without asking for credentials', async () => {
  const f = await fixture({initialSettings: settings(true),
    approvals: {pending: [approval('fixture-video')], unresolved: []}});
  assert.equal(f.selectors.get('.overlay').hidden, false);
  assert.equal(f.selectors.get('.settings-view').hidden, true);
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  assert.match(f.selectors.get('.panel-title').textContent, /确认|请求/);
  assert.equal(f.badge.textContent, 'API 已配置');
  assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  assert.equal(f.form.elements.ARK_API_KEY.value, '');
  f.assertPreserved();
});

test('saved credential fields show placeholders from public flags without receiving stored secrets', async () => {
  const f = await fixture({initialSettings: settings(true, {
    fields: {ARK_API_KEY: true, TOS_ACCESS_KEY: true, TOS_SECRET_KEY: true},
  })});
  f.selectors.get('.entry').onclick(); await settle();
  assert.equal(f.selectors.get('.settings-view').hidden, false);
  assert.equal(f.selectors.get('.approval-view').hidden, true);
  for (const name of ['ARK_API_KEY', 'TOS_ACCESS_KEY', 'TOS_SECRET_KEY']) {
    assert.equal(f.form.elements[name].value, '');
    assert.match(f.form.elements[name].placeholder, /已保存/);
  }
  f.state().settings.fields.TOS_SECRET_KEY = false;
  await f.refresh();
  assert.doesNotMatch(f.form.elements.TOS_SECRET_KEY.placeholder || '', /已保存/);
  assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  f.assertPreserved();
});

test('closing an existing approval survives polling, but each new request is shown', async () => {
  const f = await fixture({initialSettings: settings(true),
    approvals: {pending: [approval('fixture-upload', true)], unresolved: []}});
  f.selectors.get('.close').onclick();
  assert.equal(f.selectors.get('.overlay').hidden, true);
  await f.refresh(); await f.refresh();
  assert.equal(f.selectors.get('.overlay').hidden, true);
  f.approvals().pending = [approval('fixture-video')];
  await f.refresh();
  assert.equal(f.selectors.get('.overlay').hidden, false);
  assert.equal(f.selectors.get('.settings-view').hidden, true);
  f.approvals().pending = [];
  await f.refresh();
  assert.equal(f.selectors.get('.overlay').hidden, true, 'Finishing approvals must not open settings');
  assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  f.assertPreserved();
});

test('the account entry returns to a pending request and blocks settings submission during confirmation', async () => {
  const f = await fixture({initialSettings: settings(true),
    approvals: {pending: [approval('fixture-video')], unresolved: []}});
  f.selectors.get('.close').onclick();
  f.selectors.get('.entry').onclick(); await settle();
  assert.equal(f.selectors.get('.overlay').hidden, false);
  assert.equal(f.selectors.get('.settings-view').hidden, true);
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  await f.save();
  assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 0);
  assert.match(f.selectors.get('.status').textContent, /请求|确认/);
  assert.equal(f.badge.textContent, 'API 已配置');
  f.assertPreserved();
});

test('a busy response says the existing configuration is kept instead of asking to enter it again', async () => {
  const f = await fixture({initialSettings: settings(true), saveStatus: 409,
    saveError: 'ORIGINAL_TASK_RUNNING_KEEP_SETTINGS'});
  await f.save();
  assert.equal(f.badge.textContent, 'API 已配置');
  assert.match(f.selectors.get('.status').textContent, /保留/);
  assert.match(f.selectors.get('.status').textContent, /任务/);
  assert.equal(f.form.elements.ARK_API_KEY.value, '');
  f.assertPreserved();
});

test('unresolved provider requests use the approval view and cannot submit settings', async () => {
  const f = await fixture({initialSettings: settings(true), approvals: {
    pending: [], unresolved: [{id: 'fixture-uncertain', state: 'uncertain'}],
  }});
  assert.equal(f.selectors.get('.settings-view').hidden, true);
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  await f.save();
  assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  assert.match(f.selectors.get('.pending').children[0].textContent, /不会自动重发/);
  f.assertPreserved();
});

test('uncertain uploads have accurate wording without resolving or retrying the request', async () => {
  const unresolved = [{id: 'fixture-uncertain-upload', state: 'uncertain', diagnosis: 'temporary_upload_uncertain'}];
  const f = await fixture({approvals: {pending: [], unresolved}});
  const text = f.selectors.get('.pending').children.map(node => node.textContent).join('\n');
  assert.match(text, /临时素材上传/);
  assert.match(text, /素材可能已上传/);
  assert.match(text, /不是视频生成结果/);
  assert.doesNotMatch(text, /供应商控制台|重复消费/);
  assert.deepEqual(f.approvals().unresolved, unresolved);
  assert.ok(f.calls.every(call => call.method === 'GET'));
  f.assertPreserved();
});

test('a delayed readiness warning cannot replace a pending confirmation with the Key form', async () => {
  const f = await fixture({initialSettings: settings(true), pauseReadiness: true,
    approvals: {pending: [approval('fixture-video')], unresolved: []},
    readiness: {ready: false, status: 'incomplete', checks: [], message: '本地基础功能尚未就绪'},
  });
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  f.releaseReadiness(); await settle();
  assert.equal(f.selectors.get('.settings-view').hidden, true);
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  assert.match(f.selectors.get('.readiness-status').textContent, /尚未就绪/);
  assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  f.assertPreserved();
});

test('all three project pages show a shared platform account panel without requesting personal keys', async () => {
  for (const pathname of ['/projects/wardrobe', '/projects/long-video', '/projects/real-long-video']) {
    const f = await fixture({pathname, initialSettings: platformSettings()});
    assert.equal(f.selectors.get('.top-status').hidden, false, pathname);
    assert.equal(f.selectors.get('.entry').hidden, true);
    assert.equal(f.selectors.get('.top-account').textContent, 'fixture');
    assert.match(f.selectors.get('.top-expiry').textContent, /2030/);
    assert.equal(f.selectors.get('.top-balance').textContent, '余额 ¥ 120.50');
    assert.equal(f.selectors.get('.top-capabilities').children.length, 5);
    assert.match(f.selectors.get('.top-local').textContent, /已就绪/);
    f.selectors.get('.top-details').onclick(); await settle();
    assert.equal(f.selectors.get('.entry').textContent, '账号与服务状态', pathname);
    assert.equal(f.selectors.get('.panel-title').textContent, '账号与服务状态');
    assert.equal(f.form.hidden, true);
    assert.equal(f.selectors.get('.platform-status-view').hidden, false);
    assert.equal(f.selectors.get('.agent-workbench-link').hidden, true);
    assert.equal(f.badge.textContent, '平台服务已就绪');
    assert.equal(f.badge.classList.contains('ready'), true);
    assert.match(f.selectors.get('.platform-status').textContent, /三个项目共用平台/);
    assert.equal(f.selectors.get('.platform-capabilities').children.length, 5);
    assert.equal(f.selectors.get('.platform-balance').textContent, '平台账户余额：¥ 120.50');
    assert.doesNotMatch(f.selectors.get('.billing-info').textContent, /自己的模型|不扣 CZMIYOU/);
    assert.doesNotMatch(f.selectors.get('.platform-status').textContent, /填写|Key|密钥/);
    await f.save();
    assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 0);
    for (const name of ['ARK_API_KEY', 'TOS_ACCESS_KEY', 'TOS_SECRET_KEY']) assert.equal(f.form.elements[name].value, '');
    f.assertPreserved();
  }
});

test('platform readiness needs every capability and ready to be strictly true', async () => {
  for (const feature of ['video', 'image', 'analysis', 'assets', 'media']) {
    for (const value of [false, 'true', 1, undefined]) {
      const capabilities = {...platformSettings().capabilities, [feature]: value};
      const f = await fixture({initialSettings: platformSettings({capabilities})});
      assert.equal(f.badge.textContent, '平台服务待配置');
      assert.equal(f.badge.classList.contains('ready'), false);
      assert.equal(f.form.hidden, true);
      assert.match(f.selectors.get('.platform-status').textContent, /联系管理员/);
      assert.equal(f.selectors.get('.top-notice').hidden, false);
      assert.match(f.selectors.get('.top-notice').textContent, /待配置/);
      assert.doesNotMatch(f.selectors.get('.platform-status').textContent, /填写|Key|密钥/);
      f.assertPreserved();
    }
  }
  for (const ready of [false, 'true', 1, undefined]) {
    const f = await fixture({initialSettings: platformSettings({ready})});
    assert.equal(f.badge.classList.contains('ready'), false);
    assert.equal(f.form.hidden, true);
    f.assertPreserved();
  }
});

test('platform account or session changes clear readiness, balance, approval state, and password drafts', async () => {
  for (const change of ['account', 'session', 'owner', 'logout', 'logout-without-settings']) {
    const f = await fixture({initialSettings: platformSettings(),
      approvals: {pending: [approval('fixture-video')], unresolved: []}});
    f.form.elements.ARK_API_KEY.value = 'synthetic-stale-draft';
    if (change === 'account') f.state().account.user_id = 72;
    if (change === 'session') f.state().settings.session_revision = 'another-context';
    if (change === 'owner') f.state().settings.owner_id = 72;
    if (change === 'logout') f.state().account = null;
    if (change === 'logout-without-settings') {f.state().account = null; f.state().settings = null;}
    await f.refresh();
    assert.equal(f.badge.classList.contains('ready'), false);
    assert.equal(f.badge.textContent, '平台服务待验证');
    assert.equal(f.form.hidden, true);
    assert.equal(f.selectors.get('.platform-status-view').hidden, true);
    assert.equal(f.selectors.get('.platform-balance').textContent, '');
    assert.equal(f.selectors.get('.platform-capabilities').children.length, 0);
    assert.equal(f.selectors.get('.top-capabilities').children.length, 0);
    assert.equal(f.selectors.get('.top-account').textContent, '账号待验证');
    assert.equal(f.selectors.get('.top-balance').textContent, '余额待验证');
    assert.doesNotMatch(f.selectors.get('.top-local').textContent, /已就绪/);
    assert.equal(f.selectors.get('.pending').children.length, 0);
    assert.equal(f.form.elements.ARK_API_KEY.value, '');
    assert.ok(f.calls.every(call => call.method === 'GET'));
    f.assertPreserved();
  }
});

test('the platform panel has its own logout action that clears ready state before reloading', async () => {
  const f = await fixture({initialSettings: platformSettings()});
  await f.selectors.get('.platform-logout').onclick();
  assert.equal(f.badge.classList.contains('ready'), false);
  assert.equal(f.badge.textContent, '平台服务待验证');
  assert.equal(f.selectors.get('.platform-status-view').hidden, true);
  assert.equal(f.selectors.get('.platform-balance').textContent, '');
  assert.equal(f.calls.filter(call => call.path === '/api/logout' && call.method === 'POST').length, 1);
  assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 0);
  f.assertPreserved(['reload']);
});

test('platform approvals describe platform storage and billing while preserving individual decisions', async () => {
  const upload = {id: 'fixture-platform-upload', source: '准备白模参考', summary: {
    destination: '米哟平台', operation: 'media.upload', kind: 'upload', bytes: 1048576,
    cost: '素材存入平台；此步不创建付费生成任务。',
  }};
  const f = await fixture({initialSettings: platformSettings(), approvals: {pending: [upload], unresolved: []}});
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  assert.equal(f.selectors.get('.settings-view').hidden, true);
  assert.equal(f.form.hidden, true);
  assert.match(f.selectors.get('.approval-explanation').textContent, /平台统一提供/);
  assert.doesNotMatch(f.selectors.get('.approval-explanation').textContent, /Key|本机的配置/);
  let section = f.selectors.get('.pending').children[0];
  assert.match(section.children[1].textContent, /上传至平台素材服务/);
  assert.doesNotMatch(section.children[1].textContent, /Litterbox|3 天/);
  await section.children.find(child => child.textContent === '同意上传，继续').onclick();
  f.approvals().pending.push({id: 'fixture-platform-generation', source: '生成白模', summary: {
    destination: '米哟平台', operation: 'video.create', kind: 'generation',
    cost: '由米哟账户按管理员配置的通道和价格计费，本次请求仅执行一次。',
  }});
  await f.refresh();
  section = f.selectors.get('.pending').children[0];
  assert.match(section.children[1].textContent, /米哟账户.*计费/);
  assert.equal(f.calls.filter(call => call.path === '/_plugin/decision').length, 1, 'Upload permission must not approve generation');
  await f.save();
  assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 0);
  assert.equal(f.selectors.get('.approval-view').hidden, false);
  assert.equal(f.form.hidden, true);
  f.assertPreserved();
});

test('platform unresolved requests point to platform records and keep unknown balance distinct from zero', async () => {
  const f = await fixture({initialSettings: platformSettings({balance: null}),
    approvals: {pending: [], unresolved: [{id: 'fixture-platform-uncertain', state: 'uncertain'}]}});
  assert.match(f.selectors.get('.pending').children[0].textContent, /平台任务记录和账单/);
  assert.doesNotMatch(f.selectors.get('.pending').children[0].textContent, /供应商控制台/);
  assert.match(f.selectors.get('.platform-balance').textContent, /暂未获取/);
  f.state().settings.balance = 0;
  await f.refresh();
  assert.equal(f.selectors.get('.platform-balance').textContent, '平台账户余额：¥ 0.00');
  assert.ok(f.calls.every(call => call.method === 'GET'));
  f.assertPreserved();
});

test('platform character submission requires an explicit unchecked consent for each new request', async () => {
  const item = id => ({id, source: '提交虚拟人物审核', summary: {
    destination: '米哟平台', operation: 'assets.CreateAsset', kind: 'asset', cost: '按平台配置处理本次请求。',
  }});
  const f = await fixture({initialSettings: platformSettings(), approvals: {pending: [item('fixture-asset-one')], unresolved: []}});
  let section = f.selectors.get('.pending').children[0];
  let label = section.children.find(child => child.className === 'asset-consent');
  assert.ok(label);
  let checkbox = label.children.find(child => child.type === 'checkbox');
  let approve = section.children.find(child => child.textContent === '确认本次操作及可能的费用');
  assert.equal(checkbox.checked, false);
  assert.equal(approve.disabled, true);
  assert.match(label.children.find(child => child.textContent).textContent, /拥有该虚拟人物素材.*使用权.*同意提交平台审核/);
  await approve.onclick();
  assert.equal(f.calls.filter(call => call.path === '/_plugin/decision').length, 0, 'An unchecked checkbox cannot authorize a request');
  checkbox.checked = true; checkbox.onchange();
  assert.equal(approve.disabled, false);
  await f.refresh();
  assert.equal(f.selectors.get('.pending').children[0], section, 'Polling must preserve the current checkbox choice');
  await approve.onclick();
  assert.deepEqual(JSON.parse(f.calls.find(call => call.path === '/_plugin/decision').body), {
    owner: 71, session: 'fixture-context', id: 'fixture-asset-one', approved: true, compliance_confirmed: true,
  });
  f.approvals().pending.push(item('fixture-asset-two'));
  await f.refresh();
  section = f.selectors.get('.pending').children[0];
  label = section.children.find(child => child.className === 'asset-consent');
  checkbox = label.children.find(child => child.type === 'checkbox');
  approve = section.children.find(child => child.textContent === '确认本次操作及可能的费用');
  assert.equal(checkbox.checked, false, 'A prior consent must not authorize a different asset request');
  assert.equal(approve.disabled, true);
  await section.children.find(child => child.textContent === '取消，不发送').onclick();
  const decisions = f.calls.filter(call => call.path === '/_plugin/decision');
  assert.deepEqual(JSON.parse(decisions[1].body), {
    owner: 71, session: 'fixture-context', id: 'fixture-asset-two', approved: false,
  });
  assert.equal(f.calls.filter(call => call.path === '/api/settings').length, 0);
  f.assertPreserved();
});

test('platform drafts use a separate namespace before any old BYOK draft can be restored', async () => {
  const initialStorage = {
    'depthflowWardrobeDraft': 'historical-unscoped-draft',
    'yzzh.71.depthflowWardrobeDraft': 'byok-owner-draft',
    'yzzh.71.platform.depthflowWardrobeDraft': 'platform-owner-draft',
    'unrelated-preference': 'preserved',
  };
  const f = await fixture({serviceMode: 'platform', initialSettings: platformSettings(), initialStorage});
  assert.equal(f.storage.getItem('depthflowWardrobeDraft'), 'platform-owner-draft');
  f.storage.setItem('depthflowWardrobeDraft', 'new-platform-draft');
  assert.equal(f.storage.values.get('yzzh.71.platform.depthflowWardrobeDraft'), 'new-platform-draft');
  assert.equal(f.storage.values.get('yzzh.71.depthflowWardrobeDraft'), 'byok-owner-draft');
  assert.equal(f.storage.values.get('depthflowWardrobeDraft'), 'historical-unscoped-draft');
  f.storage.removeItem('depthflowWardrobeDraft');
  assert.equal(f.storage.getItem('depthflowWardrobeDraft'), null, 'Platform must not fall back to BYOK or unscoped saved tasks');
  assert.equal(f.storage.values.get('yzzh.71.depthflowWardrobeDraft'), 'byok-owner-draft');
  assert.equal(f.storage.getItem('unrelated-preference'), 'preserved');
  f.assertPreserved();
});

test('explicit BYOK mode preserves the existing owner namespace and leaves platform drafts intact', async () => {
  const f = await fixture({serviceMode: 'byok', initialStorage: {
    'yzzh.71.depthflowLongDraft': 'existing-byok-draft',
    'yzzh.71.platform.depthflowLongDraft': 'preserved-platform-draft',
  }});
  assert.equal(f.storage.getItem('depthflowLongDraft'), 'existing-byok-draft');
  f.storage.setItem('depthflowLongDraft', 'updated-byok-draft');
  assert.equal(f.storage.values.get('yzzh.71.depthflowLongDraft'), 'updated-byok-draft');
  assert.equal(f.storage.values.get('yzzh.71.platform.depthflowLongDraft'), 'preserved-platform-draft');
  f.storage.removeItem('depthflowLongDraft');
  assert.equal(f.storage.getItem('depthflowLongDraft'), null);
  assert.equal(f.storage.values.get('yzzh.71.platform.depthflowLongDraft'), 'preserved-platform-draft');
  f.assertPreserved();
});


test('platform receipt is queried only by explicit click and never approves or resubmits a task', async () => {
  const f = await fixture({initialSettings:platformSettings(), approvals:{pending:[],unresolved:[
    {id:'synthetic-original-request',state:'uncertain',service_mode:'platform'}]}});
  assert.ok(f.calls.every(call => call.method === 'GET'));
  const button = f.selectors.get('.pending').children.find(child => child.textContent === '查询原请求');
  assert.ok(button);
  await button.onclick();
  const writes = f.calls.filter(call => call.method === 'POST');
  assert.equal(writes.length,1);
  assert.equal(writes[0].path,'/_plugin/reconcile');
  assert.deepEqual(JSON.parse(writes[0].body),{owner:71,session:'fixture-context',id:'synthetic-original-request'});
  assert.match(f.selectors.get('.status').textContent,/original-platform-task/);
  f.assertPreserved();
});

test('a missing platform receipt leaves the original request visible and does not retry automatically', async () => {
  const f = await fixture({initialSettings:platformSettings(),receiptError:true, approvals:{pending:[],unresolved:[
    {id:'synthetic-missing-request',state:'uncertain',service_mode:'platform'}]}});
  const button = f.selectors.get('.pending').children.find(child => child.textContent === '查询原请求');
  await button.onclick();
  await f.refresh();
  assert.equal(f.calls.filter(call => call.method === 'POST').length,1);
  assert.match(f.selectors.get('.pending').children[0].textContent,/synthetic-missing-request/);
  assert.equal(button.disabled,false);
  f.assertPreserved();
});
