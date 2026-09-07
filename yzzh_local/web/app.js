let session = new URLSearchParams(location.hash.slice(1)).get('session') || '';
history.replaceState(null, '', location.pathname);
const el = (id) => document.getElementById(id);
let settingsContext = null;
async function request(path, body) {
  const headers = {'X-Yzzh-Request': '1'};
  if (session) headers['X-Yzzh-Session'] = session;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}
const errors = {LOGIN_REQUIRED:'请先登录 CZMIYOU。', PRODUCT_4_LICENSE_REQUIRED:'当前没有有效的衣装智换时间卡；可以取回已有结果。', LICENSE_SERVICE_UNAVAILABLE:'暂时无法验证授权，请稍后再试。已有任务与文件不会删除。', LOGIN_REJECTED:'登录失效或账号密码不正确，请重新登录。', PROVIDER_SETTINGS_REQUIRED:'请先填写方舟 API Key；模型和可选存储在高级设置中调整。', SETTINGS_CHANGED_REPLAN_REQUIRED:'账号或模型配置已变化，请重新创建方案并确认。', SUBMIT_UNCERTAIN_CHECK_PROVIDER_CONSOLE:'提交结果不明，且未取得任务 ID。请到供应商控制台核对，不要重复生成。', REFERENCE_VIDEO_FORMAT_UNSUPPORTED:'参考视频不符合要求，请检查 MP4/MOV、时长 2–15 秒、帧率 24–60 及分辨率后重新规划。'};
Object.assign(errors, {"INVALID_LOGIN":"请填写账号、密码，并选择登录身份。","INVALID_LOGIN_ROLE":"请选择你在 CZMIYOU 账号中心使用的登录身份。","LOGIN_CREDENTIALS_INVALID":"账号或密码不正确，请核对后重新输入。","LOGIN_ROLE_NOT_APPROVED":"该账号没有所选身份，或该身份尚未审批通过。请选择平时登录使用的身份；仍失败请联系管理员。","LOGIN_REJECTED":"账号中心未通过登录验证。请核对账号、密码和登录身份；若仍失败，请在账号中心确认账号状态。","LICENSE_SERVICE_UNAVAILABLE":"暂时无法连接或验证账号中心，请稍后再试；不会删除已有任务。","LICENSE_INVALID_RESPONSE":"账号中心返回的数据不符合预期，请联系管理员检查接口兼容性。"});
function message(text) { el('message').textContent = errors[text] || text; }
async function act(name, args) {
  try { const data = await request('/api/call', {name, arguments: args}); message(JSON.stringify(data, null, 2)); await refresh(); }
  catch (error) { message(error.message); }
}
function button(parent, label, action) {
  const b = document.createElement('button'); b.textContent = label; b.onclick = action; parent.append(b);
}
async function refresh() {
  try {
    const state = await request('/api/state');
    el('login').hidden = true;
    el('unified-login').hidden = !!state.account;
    el('unified-login').href = new URL(state.health.login_url).origin;
    el('health').textContent = `本地服务已连接 · ${state.health.version} · 自带 Key 模式（真实业务待验收）`;
    el('login-destination').textContent = `账号验证地址：${state.health.login_url || ''}。密码只发往此地址。`;
    el('account').textContent = state.account ? `${state.account.username} · 产品 4 · ${state.account.licensed ? '时间卡有效至 ' + state.account.subscription_end : '无有效授权或授权待重验；已有结果可取回'}` : '请先登录并验证产品 4 时间卡。';
    const settings = state.settings;
    if (settingsContext?.expected_session !== settings?.session_revision) {
      delete el('settings').dataset.uploadEdited;
      for (const name of ['ARK_API_KEY','TOS_ACCESS_KEY','TOS_SECRET_KEY']) el('settings').elements[name].value = '';
    }
    settingsContext = settings ? {expected_owner:settings.owner_id, expected_session:settings.session_revision} : null;
    el('settings-status').textContent = settings ? `${settings.configured ? '模型配置已保存（尚未验证实际权限）' : '请填写模型 Key；其他选项见高级设置。'} · ${settings.upload?.storage || '自动上传'}` : '登录后配置。';
    for (const input of el('settings').elements) input.disabled = !state.account;
    for (const [name, value] of [['ARK_MODEL',settings?.model || 'doubao-seedance-2-0-260128'], ['TOS_BUCKET',settings?.bucket || '']]) {
      const input = el('settings').elements[name];
      if (document.activeElement !== input) input.value = value;
    }
    if (!el('settings').dataset.uploadEdited) el('settings').elements.MEDIA_UPLOAD_MODE.value = settings?.upload_mode || 'temporary';
    el('tos-settings').hidden = el('settings').elements.MEDIA_UPLOAD_MODE.value !== 'tos';
    el('projects').replaceChildren(); el('approval').replaceChildren(); el('empty').hidden = state.projects.length > 0;
    for (const project of state.projects) {
      const card = document.createElement('article');
      const title = document.createElement('h3'); title.textContent = project.name; card.append(title);
      const status = document.createElement('p'); status.textContent = `状态：${project.state}${project.error ? ' / ' + project.error : ''}`; card.append(status);
      for (const [id, artifact] of Object.entries(project.artifacts)) {
        const row = document.createElement('div'); row.className = 'artifact';
        const label = document.createElement('span'); label.textContent = `${artifact.kind} · ${artifact.file}`; row.append(label);
        button(row, '预览', () => {if (artifact.kind === 'image') window.open(`/media/${project.id}/${id}`, '_blank', 'noopener'); else el('preview').src = `/media/${project.id}/${id}`;});
        if (artifact.kind !== 'image') for (const [op, text] of [['split','分镜'], ['mosaic','打码'], ['depth','深度']]) button(row, text, () => act('process', {project_id: project.id, artifact_id: id, operation: op}));
        card.append(row);
      }
      if (['provider_running','submit_uncertain','download_pending'].includes(project.state)) button(card, '查询供应商任务（不重发）', () => act('poll', {project_id: project.id}));
      if (project.state === 'approved') button(card, '提交已批准方案', () => act('submit', {project_id: project.id}));
      if (project.state === 'awaiting_approval' && project.plan) {
        const check = document.createElement('article'); const detail = document.createElement('pre');
        for (const reference of project.plan.references || []) {
          const label = document.createElement('p'); label.textContent = reference.name; check.append(label);
          const preview = document.createElement(reference.kind === 'image' ? 'img' : 'video');
          preview.className = 'reference'; preview.src = `/media/${project.id}/${reference.artifact_id}`;
          if (reference.kind === 'video') preview.controls = true; else preview.alt = reference.name;
          check.append(preview);
        }
        detail.textContent = JSON.stringify(project.plan, null, 2); check.append(detail);
        const info = document.createElement('p'); info.textContent = `素材上传至：${project.plan.destination?.storage || '请检查方案'}。${project.plan.destination?.retention || ''} 生成使用自己的方舟账号，不扣 CZMIYOU 余额。请确认素材权利、隐私及费用。`; check.append(info);
        for (const approved of [true,false]) button(check, approved ? '我已检查，同意上传与计费' : '拒绝，重新调整', async () => {
          try { await request('/api/approve', {project_id: project.id, plan_hash: project.plan.hash, approved}); await refresh(); }
          catch (error) {message(error.message);}
        });
        el('approval').append(check);
      }
      el('projects').append(card);
    }
  } catch (error) {message(error.message + '；请从插件的“打开工作台”工具进入。');}
}
el('login').onsubmit = async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form));
  try { const account = await request('/api/login', data); message(account.licensed ? '登录成功，产品 4 时间卡有效' : '登录成功，但当前无有效产品 4 时间卡；已有结果可取回'); await refresh(); }
  catch (error) {message(error.message);} finally {form.elements.password.value = '';}
};
el('logout').onclick = async () => {try {await request('/api/logout', {}); await refresh();} catch (e) {message(e.message);}};
el('settings').onsubmit = async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  try { await request('/api/settings', {...settingsContext, values: Object.fromEntries(new FormData(form))}); delete form.dataset.uploadEdited; message('已保存到本机，未上传或生成。旧方案需要重新检查和确认。'); await refresh(); }
  catch (e) {message(e.message);} finally {for (const name of ['ARK_API_KEY','TOS_ACCESS_KEY','TOS_SECRET_KEY']) form.elements[name].value = '';}
};
el('clear-settings').onclick = async () => {
  if (!window.confirm('清除本账号保存在本机的 Key 和模型配置？已有任务保留，但取回结果可能需要重新填写原供应商 Key。')) return;
  try {await request('/api/settings', {...settingsContext, clear:true}); message('本账号的本地配置已清除。'); await refresh();} catch(e) {message(e.message);}
};
el('import').onsubmit = (event) => {event.preventDefault(); act('import_video', Object.fromEntries(new FormData(event.currentTarget)));};
el('settings').elements.MEDIA_UPLOAD_MODE.onchange = () => {el('settings').dataset.uploadEdited = '1'; el('tos-settings').hidden = el('settings').elements.MEDIA_UPLOAD_MODE.value !== 'tos';};
el('refresh').onclick = refresh;
(async () => {if (session) {try {await request('/session', {}); session = '';} catch (e) {message(e.message);}} await refresh();})();
