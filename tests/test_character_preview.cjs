'use strict';

// Execute the complete character-library UI with a synthetic DOM and read-only
// responses. No real account, browser state, image or provider is accessed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/character_library.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function element() {
  let html = '';
  return {
    value: '', dataset: {}, hidden: false, listeners: {}, textContent: '', children: [],
    classList: {toggle() {}, add() {}, remove() {}},
    addEventListener(name, listener, capture) { this.listeners[name] = {listener, capture}; },
    dispatchEvent() {}, append(child) {this.children.push(child);}, prepend(child) {this.children.unshift(child);},
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; this.firstElementChild = {outerHTML: value}; },
  };
}

const library = () => ({configured: true, groups: [
  {id: 'group-a', name: 'synthetic A', group_type: 'AIGC'},
  {id: 'group-b', name: 'synthetic B', group_type: 'AIGC'},
], assets: [{id: 'asset-fixture01', uri: 'asset://asset-fixture01', group_id: 'group-a',
  name: 'Synthetic role', status: 'Active', url: '/api/plugin-remote/synthetic-private-key'}]});

async function fixture(initial = library()) {
  const nodes = new Map();
  const panel = element();
  panel.querySelector = selector => {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  };
  panel.querySelectorAll = () => [];
  const form = element(), host = element(), person = element();
  host.closest = () => form;
  host.querySelector = () => null;
  person.value = 'asset://asset-fixture01';
  const selectedFile = Object.freeze({name: 'synthetic-selected.png', size: 100});
  const fileNode = panel.querySelector('[data-character-upload-file]');
  fileNode.files = [selectedFile];
  const calls = [], toasts = [], replies = [initial], navigation = [];
  const document = {
    hidden: false,
    querySelector(selector) {
      return {'#characters': host, '#personAsset': person}[selector] || null;
    },
    createElement: name => name === 'section' ? panel : element(),
  };
  const window = {
    location: {pathname: '/projects/wardrobe', reload: () => navigation.push('reload')},
    toast: (message, isError) => toasts.push({message, isError}),
    setTimeout: () => 1, clearTimeout() {},
  };
  const fetch = async (url, options = {}) => {
    calls.push({url, options});
    assert.equal(options.method || 'GET', 'GET', 'Only a read is allowed');
    assert.match(url, /^\/api\/character-library\?_/);
    const next = await replies.shift();
    if (next instanceof Error) throw next;
    assert.ok(next, 'No unexpected retry');
    return {ok: true, status: 200, json: async () => next};
  };
  vm.runInNewContext(source, {window, document, fetch, console,
    localStorage: {getItem: () => '', setItem() {}, removeItem() {}}, Event: class {}},
    {filename: 'character_library.js'});
  await settle();
  const click = async selector => {
    await panel.listeners.click.listener({target: {closest: candidate => candidate === selector ? {} : null}});
    await settle();
  };
  const failImage = () => {
    const image = {dataset: {characterPreview: 'asset://asset-fixture01'},
      matches: selector => selector === 'img[data-character-preview]',
      replaceWith(node) {this.replacement = node;}};
    panel.listeners.error.listener({target: image});
    return image;
  };
  return {nodes, panel, person, fileNode, selectedFile, calls, replies, navigation, toasts, click, failImage, window};
}

test('failed image becomes an explicit placeholder and never triggers automatic requests', async () => {
  const f = await fixture();
  const image = f.failImage();
  assert.equal(f.panel.listeners.error.capture, true);
  assert.match(image.replacement.outerHTML, /预览不可用/);
  assert.match(image.replacement.outerHTML, /重试预览/);
  assert.equal(f.nodes.get('[data-character-preview-state]').hidden, false);
  assert.match(f.nodes.get('[data-character-preview-state]').textContent, /1 张角色预览未加载/);
  assert.equal(f.calls.length, 1);
  assert.equal(f.person.value, 'asset://asset-fixture01');
});

test('explicit retry reads once while pending and preserves filters, selection and file', async () => {
  const f = await fixture();
  f.failImage();
  const filter = f.nodes.get('[data-character-group-filter]');
  const targetGroup = f.nodes.get('[data-character-upload-group]');
  const name = f.nodes.get('[data-character-upload-name]') || f.panel.querySelector('[data-character-upload-name]');
  filter.value = 'group-a'; targetGroup.value = 'group-b'; name.value = 'unsaved synthetic name';
  let resolve;
  f.replies.push(new Promise(done => {resolve = done;}));
  await f.click('[data-character-preview-retry]');
  await f.click('[data-character-preview-retry]');
  assert.equal(f.calls.length, 2);
  const fresh = library(); fresh.assets[0].url = '/api/plugin-remote/fresh-synthetic-key';
  resolve(fresh); await settle();
  assert.match(f.nodes.get('[data-character-assets]').innerHTML, /fresh-synthetic-key/);
  assert.equal(f.nodes.get('[data-character-preview-state]').hidden, true);
  assert.equal(filter.value, 'group-a');
  assert.equal(targetGroup.value, 'group-b');
  assert.equal(name.value, 'unsaved synthetic name');
  assert.equal(f.person.value, 'asset://asset-fixture01');
  assert.equal(f.fileNode.files[0], f.selectedFile);
  assert.deepEqual(f.navigation, []);
});

test('a failed library refresh keeps cards and user inputs, with a visible retry message', async () => {
  const f = await fixture();
  const assets = f.nodes.get('[data-character-assets]');
  const before = assets.innerHTML;
  f.nodes.get('[data-character-group-filter]').value = 'group-a';
  f.replies.push(new Error('synthetic network failure'));
  await f.click('[data-character-refresh]');
  assert.equal(assets.innerHTML, before);
  assert.equal(f.nodes.get('[data-character-group-filter]').value, 'group-a');
  assert.equal(f.person.value, 'asset://asset-fixture01');
  assert.equal(f.fileNode.files[0], f.selectedFile);
  assert.match(f.nodes.get('[data-character-state]').textContent, /角色库刷新失败/);
  assert.equal(f.calls.length, 2);
  assert.deepEqual(f.navigation, []);
});

test('a missing or unsupported preview is never shown as successful', async () => {
  const initial = library(); initial.assets[0].url = '';
  initial.assets[0].preview_error = '此角色图片域名尚未支持安全预览。';
  const f = await fixture(initial);
  const html = f.nodes.get('[data-character-assets]').innerHTML;
  assert.match(html, /预览不可用/);
  assert.match(html, /重试预览/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /尚未支持安全预览/);
});
