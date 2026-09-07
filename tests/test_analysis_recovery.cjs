const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

for (const project of ['long_video', 'real_long_video']) {
  const source = fs.readFileSync(path.join(__dirname, '../web', project + '.js'), 'utf8');
  test(project + ': review opens saved mappings without approval, upload or analysis calls', async () => {
    let calls = 0, scrolled = 0, focused = 0, step;
    const state = {lastJob: {cast_continuity: {manual_review_required: true}}};
    const start = source.indexOf('function openCastReview()');
    const after = source.indexOf('async function analyzePerformance()', start) + 1;
    const next = source.slice(after).search(/\n(?:async )?function /);
    const section = source.slice(start, next < 0 ? undefined : after + next);
    const ctx = vm.createContext({
      showWorkflowStep: value => {step = value;},
      state, $: id => id === '#shotGrid'
        ? {scrollIntoView: () => scrolled++}
        : {focus: () => focused++},
      api: () => {calls++; throw Error('Unexpected network');},
      window: {DepthFlowUI: {showStep: value => {step = value;}},
               confirm: () => {calls++; throw Error('Unexpected approval');}},
    });
    vm.runInContext(section, ctx);
    await vm.runInContext('analyzePerformance()', ctx);
    await vm.runInContext('analyzePerformance()', ctx);
    assert.equal(calls, 0);
    assert.equal(step, 'progress');
    assert.equal(scrolled, 2);
    assert.equal(focused, 2);
  });

  test(project + ': unresolved people remain unselected; reviewed mappings survive rendering', () => {
    const state = {actorCount: 2, manualCasts: new Set(), shotCasts: new Map(),
      lastJob: {cast_continuity: {manual_review_required: true}, shots: [
        {index: 1}, {index: 2, cast_confirmed: true, actor_ids: [2, 1]},
      ]}};
    const ctx = vm.createContext({state, detectedPeopleCount: () => 2, continuityCharacterFor: () => 0});
    vm.runInContext(source.slice(source.indexOf('function applyAutomaticCastSuggestions()'), source.indexOf('function castFor(')), ctx);
    vm.runInContext('applyAutomaticCastSuggestions()', ctx);
    assert.deepEqual(Array.from(state.shotCasts.get(1)), [0, 0]);
    assert.deepEqual(Array.from(state.shotCasts.get(2)), [2, 1]);
    state.manualCasts.add(1);
    state.shotCasts.set(1, [1, 2]);
    vm.runInContext('applyAutomaticCastSuggestions()', ctx);
    assert.deepEqual(Array.from(state.shotCasts.get(1)), [1, 2]);
  });
}
