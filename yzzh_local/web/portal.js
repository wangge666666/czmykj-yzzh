/* Additive bridge: the original page markup, CSS and workflows stay intact. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const owner = document.querySelector('meta[name="yzzh-owner"]')?.content || '';
  const context = document.querySelector('meta[name="yzzh-context"]')?.content || '';
  const bootstrapServiceMode = document.querySelector('meta[name="yzzh-service-mode"]')?.content === 'platform' ? 'platform' : 'byok';
  let session = new URLSearchParams(location.hash.slice(1)).get('session') || '';
  if (session) history.replaceState(null, '', location.pathname + location.search);
  const headers = () => ({'X-Yzzh-Request':'1', 'X-Yzzh-Owner':owner, 'X-Yzzh-Context':context,
    ...(session ? {'X-Yzzh-Session':session} : {})});
  const ready = session ? nativeFetch('/session', {method:'POST', headers:{...headers(), 'Content-Type':'application/json'}, body:'{}'})
    .then(r => {if (!r.ok) throw Error('请从 Agent 的“打开工作台”入口进入。'); session = '';}) : Promise.resolve();
  // Isolate old UI draft/workspace keys as well as backend data. Do not erase
  // the historical unscoped keys: they still belong to the original web app.
  const storage = {get:Storage.prototype.getItem, set:Storage.prototype.setItem, remove:Storage.prototype.removeItem};
  const key = name => String(name).startsWith('depthflow')
    ? `yzzh.${owner || 'guest'}.${bootstrapServiceMode === 'platform' ? 'platform.' : ''}${name}` : name;
  Storage.prototype.getItem = function(name) { return storage.get.call(this, key(name)); };
  Storage.prototype.setItem = function(name, value) { return storage.set.call(this, key(name), value); };
  Storage.prototype.removeItem = function(name) { return storage.remove.call(this, key(name)); };
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return nativeFetch(input, options);
    await ready;
    const merged = new Headers(input instanceof Request ? input.headers : options.headers);
    for (const [name,value] of Object.entries(headers())) merged.set(name,value);
    const response = await nativeFetch(input, {...options, headers:merged});
    if ([401,403,409].includes(response.status)) {
      const data = await response.clone().json().catch(() => ({}));
      if (['LOGIN_REQUIRED','PRODUCT_4_LICENSE_REQUIRED','ACCOUNT_CHANGED_REFRESH','LOCAL_SESSION_REQUIRED'].includes(data.error)) show(data.error);
    }
    return response;
  };
  const messages = {
    INVALID_LOGIN:'请填写账号、密码，并选择登录身份。',
    INVALID_LOGIN_ROLE:'请选择你在 CZMIYOU 账号中心使用的登录身份。',
    LOGIN_CREDENTIALS_INVALID:'账号或密码不正确，请核对后重新输入。',
    LOGIN_ROLE_NOT_APPROVED:'该账号没有所选身份，或该身份尚未审批通过。请选择平时登录使用的身份；仍失败请联系管理员。',
    LOGIN_REJECTED:'账号中心未通过登录验证。请核对账号、密码和登录身份；若仍失败，请在账号中心确认账号状态。',
    LICENSE_SERVICE_UNAVAILABLE:'暂时无法连接或验证账号中心，请稍后再试；不会删除已有任务。',
    LICENSE_INVALID_RESPONSE:'账号中心返回的数据不符合预期，请联系管理员检查接口兼容性。',
    LOGIN_REQUIRED:'请登录 CZMIYOU 后使用原工作流程。',
    LOCAL_SESSION_REQUIRED:'请从 Codex / WorkBuddy 的“打开工作台”工具进入。',
    ACCOUNT_CHANGED_REFRESH:'账号已切换。此页面已停止操作，请刷新；不会借用新账号的素材或 Key。',
    PRODUCT_4_LICENSE_REQUIRED:'没有有效的产品 4 时间卡；已有结果保留。',
    ORIGINAL_TASK_RUNNING_KEEP_SETTINGS:'任务正在运行，已保存的配置仍然保留，无需重新填写 Key。请等待任务结束后再修改配置。',
    PREVIOUS_REQUEST_UNCERTAIN_CHECK_PROVIDER:'上一请求结果不明。请在供应商控制台核对；插件已暂停新的云端请求，避免重复消费。',
    PREVIOUS_UPLOAD_UNCERTAIN:'上一次临时素材上传的回执未确认，素材可能已上传。这条记录不是视频生成结果。新的云端请求已暂停，不会自动重传；请让 Codex 检查上传记录。',
    SETTINGS_CHANGED_REPLAN_REQUIRED:'配置已变化，旧请求确认失效。请检查后重新操作。',
    TEMP_UPLOAD_UNAVAILABLE:'素材上传服务暂时不可用，尚未提交视频生成。请稍后再试。',
    TEMP_LINK_NOT_READABLE:'素材读取检查未通过，尚未提交视频生成。请稍后重试。',
  };
  let shadow, dialog, status, account, form, pending, settingsForm, configured, button, state;
  let readinessKey = '', readinessEpoch = 0, readinessPending = false;
  let panelView = 'settings', attentionSignature = '', hasAttention = false;
  let serviceMode = bootstrapServiceMode;
  const platformMode = () => serviceMode === 'platform';
  const platformFeatures = [['video','视频生成'], ['image','图片生成'], ['analysis','内容分析'], ['assets','角色素材库'], ['media','素材上传']];
  const platformReady = settings => settings?.ready === true && platformFeatures.every(([name]) => settings.capabilities?.[name] === true);
  function show(message, view = 'settings') {
    if (!shadow) return;
    panelView = view;
    shadow.querySelector('.settings-view').hidden = view !== 'settings';
    shadow.querySelector('.approval-view').hidden = view !== 'approval';
    shadow.querySelector('.panel-title').textContent = view === 'approval' ? '确认本次操作' : platformMode() ? '账号与服务状态' : '账号与创作设置';
    if (message) status.textContent = messages[message] || message;
    else status.textContent = '';
    dialog.hidden = false;
  }
  function syncApiBadge(settings) {
    const badge = document.querySelector('#arkBadge');
    if (settings?.mode === 'platform') {
      if (!badge) return;
      const ready = platformReady(settings);
      badge.textContent = ready ? '平台服务已就绪' : '平台服务待配置';
      badge.classList.toggle('ready', ready);
      return;
    }
    const ready = settings?.fields?.ARK_API_KEY;
    if (!badge || typeof ready !== 'boolean') return;
    // Match the original API badge: Key presence, not model access or the
    // broader upload configuration. Use public metadata; never reload drafts
    // or call /api/config, which can resume saved workflow tasks.
    badge.textContent = ready ? 'API 已配置' : 'API 未配置';
    badge.classList.toggle('ready', ready);
  }
  function clearAccountState(message) {
    hasAttention = false; attentionSignature = ''; pending.replaceChildren();
    settingsForm.hidden = true; form.hidden = true;
    shadow.querySelector('.platform-status-view').hidden = true;
    shadow.querySelector('.platform-status').textContent = message;
    shadow.querySelector('.platform-capabilities').replaceChildren();
    shadow.querySelector('.platform-balance').textContent = '';
    configured.textContent = '';
    for (const [name,,type] of fields) if (type === 'password') settingsForm.elements[name].value = '';
    const badge = document.querySelector('#arkBadge');
    if (badge) {badge.textContent = platformMode() ? '平台服务待验证' : 'API 未配置'; badge.classList.toggle('ready', false);}
    clearReadiness(message);
  }
  function renderPlatform(settings) {
    shadow.querySelector('.platform-status-view').hidden = false;
    const ready = platformReady(settings);
    shadow.querySelector('.platform-status').textContent = ready
      ? '平台服务已就绪，三个项目共用平台提供的模型、角色库和素材服务。'
      : '平台服务尚未配置完整，请联系管理员完成服务配置。';
    const list = shadow.querySelector('.platform-capabilities'); list.replaceChildren();
    for (const [name,label] of platformFeatures) {
      const item = document.createElement('li');
      item.textContent = `${label}：${settings.capabilities?.[name] === true ? '已就绪' : '待配置'}`;
      list.append(item);
    }
    const balance = settings.balance;
    shadow.querySelector('.platform-balance').textContent = typeof balance === 'number' && Number.isFinite(balance)
      ? `平台账户余额：¥ ${balance.toFixed(2)}` : '平台账户余额：暂未获取';
    for (const [name,,type] of fields) if (type === 'password') settingsForm.elements[name].value = '';
  }
  async function api(path, data) {
    await ready;
    const response = await nativeFetch(path, {method:data === undefined ? 'GET':'POST', headers:{...headers(), ...(data === undefined ? {}:{'Content-Type':'application/json'})}, body:data === undefined ? undefined:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw Error(messages[result.error] || result.error || '请求未完成');
    return result;
  }
  function clearReadiness(message) {
    readinessKey = ''; readinessEpoch++; readinessPending = false;
    shadow.querySelector('.readiness-status').textContent = message;
    shadow.querySelector('.readiness-checks').replaceChildren();
    shadow.querySelector('.readiness-check').disabled = true;
  }
  async function checkReadiness(force = false) {
    if (!readinessKey || readinessPending) return;
    const epoch = readinessEpoch;
    readinessPending = true;
    const action = shadow.querySelector('.readiness-check');
    const text = shadow.querySelector('.readiness-status');
    action.disabled = true; text.textContent = '正在检查本地模型与视频工具…';
    try {
      const result = await api('/api/plugin-readiness' + (force ? '?recheck=1' : ''));
      if (epoch !== readinessEpoch) return;
      text.textContent = result.message || '自检未完成，请重新检查。';
      if (result.status === 'incomplete' && !hasAttention) show('本地基础功能尚未就绪，请查看“本地基础自检”中的修复提示。');
      const list = shadow.querySelector('.readiness-checks'); list.replaceChildren();
      for (const check of result.checks || []) {
        const row = document.createElement('li');
        row.textContent = `${check.label}：${check.ready ? '已就绪' : check.message}`;
        list.append(row);
      }
    } catch (e) {
      if (epoch === readinessEpoch) text.textContent = '本地自检暂时无法完成，请稍后重新检查；仍失败请让 Codex 检查插件连接。';
    } finally {
      if (epoch === readinessEpoch) {readinessPending = false; action.disabled = false;}
    }
  }
  const fields = [
    ['ARK_API_KEY','方舟 API Key','password'], ['ARK_MODEL','默认视频模型 ID','text'],
    ['TOS_ACCESS_KEY','人物库 Access Key','password'], ['TOS_SECRET_KEY','人物库 Secret Key','password'],
    ['TOS_BUCKET','自己的北京 TOS 桶名','text'], ['ARK_IMAGE_MODEL','图片模型 ID（可空，使用原默认值）','text'],
    ['ARK_PERFORMANCE_MODEL','表演分析模型 ID（可空，使用原默认值）','text']
  ];
  function virtualPortraitLabels() {
    if (!['/', '/projects/real-long-video'].includes(location.pathname)) return;
    // This original engine already lists AIGC groups, not LivenessFace. Correct
    // display labels only; preserve routes, saved task IDs, consent and inputs.
    const rename = text => text.replaceAll('真实人物', '写实虚拟人像')
      .replaceAll('锁定真人身份', '锁定虚拟人像身份')
      .replaceAll('真人重绘处理流程', '虚拟人像重绘处理流程')
      .replaceAll('授权真人图', '虚拟人像图')
      .replaceAll('Authorized real-person Asset redraw', 'AIGC portrait Asset redraw')
      .replaceAll('Authorized person · Ark Character Asset', 'AIGC portrait · Ark Character Asset');
    const update = () => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (node.parentElement?.closest('script,style,textarea,input,pre,[contenteditable]')) continue;
        const text = rename(node.nodeValue);
        if (text !== node.nodeValue) node.nodeValue = text;
      }
      for (const input of document.querySelectorAll('input[placeholder]')) {
        const text = rename(input.placeholder);
        if (text !== input.placeholder) input.placeholder = text;
      }
      const title = rename(document.title);
      if (title !== document.title) document.title = title;
    };
    update();
    new MutationObserver(update).observe(document.body, {childList:true,subtree:true,characterData:true});
  }
  async function refresh() {
    try {
      state = await api('/api/state');
      const current = state.account;
      const settings = state.settings || {};
      if (settings.mode === 'platform') serviceMode = 'platform';
      else if (current) serviceMode = 'byok';
      account.textContent = current ? `${current.username} · 产品 4 · ${current.licensed ? '时间卡有效至 ' + current.subscription_end : '无有效时间卡或待重验'}` : '未登录 CZMIYOU';
      shadow.querySelector('.login-destination').textContent = platformMode()
        ? `通过 CZMIYOU 账号中心登录：${new URL(state.health.login_url).origin}`
        : `登录信息只发往：${state.health.login_url}。不要把密码或 Key 发到聊天中。`;
      shadow.querySelector('.billing-info').textContent = platformMode()
        ? '时间卡控制插件使用权，云端操作按平台账户和管理员配置的价格计费。'
        : '沿用原来的创作流程。时间卡控制插件使用权，生成费用由自己的模型账号承担，不扣 CZMIYOU 余额。';
      shadow.querySelector('.approval-explanation').textContent = platformMode()
        ? '本次操作使用平台统一提供的服务。请核对下面的素材去向或平台计费信息，再决定是否继续。'
        : '本次操作将沿用已保存在本机的配置，无需重新填写 API Key。请核对下面的素材去向或生成费用后决定是否继续。';
      shadow.querySelector('.agent-workbench-link').hidden = platformMode();
      button.textContent = current ? (platformMode() ? '账号与服务状态' : '账号与创作设置') : '登录并开始创作';
      if (panelView === 'settings') shadow.querySelector('.panel-title').textContent = platformMode() ? '账号与服务状态' : '账号与创作设置';
      if (current && (String(current.user_id) !== owner || settings.session_revision !== context ||
          (platformMode() && String(settings.owner_id) !== owner))) {
        clearAccountState('账号已切换，请刷新后重新检查。');
        show('ACCOUNT_CHANGED_REFRESH'); return;
      }
      form.hidden = true; // The customer-facing login is the existing CZMIYOU portal.
      shadow.querySelector('.unified-login').hidden = !!current;
      shadow.querySelector('.unified-login a').href = new URL(state.health.login_url).origin;
      settingsForm.hidden = !current || platformMode();
      shadow.querySelector('.platform-status-view').hidden = !current || !platformMode();
      if (!current) {clearAccountState('登录后自动检查本地基础功能。'); return;}
      if (!readinessKey) {
        readinessKey = `${owner}:${context}`;
        checkReadiness();
      }
      syncApiBadge(settings);
      if (platformMode()) {
        renderPlatform(settings);
      } else {
      for (const [name,,type] of fields) {
        if (type === 'password') settingsForm.elements[name].placeholder = settings.fields?.[name] === true
          ? '已保存在本机，无需重复填写；留空保留原值' : '尚未配置';
      }
      configured.textContent = (state.settings.configured ? '模型配置已保存，可以回到原页面继续操作（实际权限以平台验证为准）。' : '填写自己的方舟 API Key 即可保存；模型可在高级设置调整。') + ' 密钥只保存本机，留空保留原值。';
      for (const [name,value] of [['ARK_MODEL',state.settings.model], ['TOS_BUCKET',state.settings.bucket], ['ARK_IMAGE_MODEL',state.settings.image_model], ['ARK_PERFORMANCE_MODEL',state.settings.performance_model]]) {
        if (shadow.activeElement !== settingsForm.elements[name]) settingsForm.elements[name].value = value || (name === 'ARK_MODEL' ? 'doubao-seedance-2-0-260128' : '');
      }
      if (shadow.activeElement !== settingsForm.elements.MEDIA_UPLOAD_MODE && !settingsForm.dataset.uploadEdited) {
        settingsForm.elements.MEDIA_UPLOAD_MODE.value = state.settings.upload_mode || 'temporary';
      }
      shadow.querySelector('.tos-fields').hidden = settingsForm.elements.MEDIA_UPLOAD_MODE.value !== 'tos';
      }
      const approvals = await api('/_plugin/approvals');
      hasAttention = !!(approvals.pending.length || approvals.unresolved.length);
      const signature = JSON.stringify([serviceMode, approvals.pending.map(item => [item.id, item.state]), approvals.unresolved.map(item => [item.id, item.state, item.diagnosis])]);
      if (signature === attentionSignature) return;
      attentionSignature = signature;
      pending.replaceChildren();
      for (const item of approvals.unresolved) {
        const text = document.createElement('p');
        text.textContent = item.diagnosis === 'temporary_upload_uncertain'
          ? `临时素材上传 ${item.id} 的回执未确认，素材可能已上传。这条记录不是视频生成结果。新云端请求已暂停，记录会保留；不会自动重传，请让 Codex 检查上传记录。`
          : platformMode() ? `请求 ${item.id} 状态未确认。请核对平台任务记录和账单；不会自动重发。`
          : `请求 ${item.id} 状态未确认。请核对供应商控制台；不会自动重发。`;
        pending.append(text);
      }
      for (const item of approvals.pending) {
        const section = document.createElement('section');
        const platformUpload = platformMode() && item.summary.operation === 'media.upload' && item.summary.kind === 'upload';
        const upload = platformUpload || item.summary.destination === 'Litterbox 临时素材托管';
        const title = document.createElement('h3'); title.textContent = upload ? '确认上传本次素材' : '确认当前云端操作'; section.append(title);
        const description = document.createElement('p');
        description.textContent = platformUpload
          ? `${(item.summary.bytes / 1048576).toFixed(1)} MB 素材将上传至平台素材服务，用于本次创作。保存期限由平台管理。${item.summary.cost || ''}`
          : upload ? `${(item.summary.bytes / 1048576).toFixed(1)} MB 素材将临时上传至 Litterbox，供人物审核或视频生成读取。持链接可访问，服务声明约 3 天过期，插件不能提前删除。请确认你有权上传，且不包含保密素材。`
          : `${item.summary.destination}：${item.summary.cost}`;
        section.append(description);
        const advanced = document.createElement('details'); const caption = document.createElement('summary'); caption.textContent = '查看本次请求详情';
        const detail = document.createElement('pre'); detail.textContent = JSON.stringify({原步骤:item.source, ...item.summary}, null, 2);
        advanced.append(caption, detail); section.append(advanced);
        const needsAssetConsent = platformMode() && item.summary.operation === 'assets.CreateAsset';
        let assetConsent;
        if (needsAssetConsent) {
          const label = document.createElement('label'); label.className = 'asset-consent';
          assetConsent = document.createElement('input'); assetConsent.type = 'checkbox'; assetConsent.checked = false;
          const text = document.createElement('span'); text.textContent = '我确认拥有该虚拟人物素材及本次用途的使用权，并同意提交平台审核';
          label.append(assetConsent, text); section.append(label);
        }
        for (const approved of [true,false]) {
          const action = document.createElement('button'); action.textContent = approved ? (upload ? '同意上传，继续' : '确认本次操作及可能的费用') : '取消，不发送';
          let sending = false;
          action.disabled = approved && needsAssetConsent;
          if (approved && needsAssetConsent) assetConsent.onchange = () => {action.disabled = sending || assetConsent.checked !== true;};
          action.onclick = async () => {
            if (sending) return;
            if (approved && needsAssetConsent && assetConsent.checked !== true) {show('请先确认本次虚拟人物素材的使用权和审核授权。', 'approval'); return;}
            sending = true; action.disabled = true;
            try {
              const decision = {owner:Number(owner), session:context, id:item.id, approved};
              if (approved && needsAssetConsent) decision.compliance_confirmed = true;
              await api('/_plugin/decision', decision); await refresh();
            } catch(e) {show(e.message, 'approval');}
            finally {sending = false; action.disabled = approved && needsAssetConsent && assetConsent.checked !== true;}
          };
          section.append(action);
        }
        pending.append(section);
      }
      if (hasAttention) show('', 'approval');
      else if (panelView === 'approval') {dialog.hidden = true; status.textContent = '';}
    } catch (e) {status.textContent = e.message;}
  }
  document.addEventListener('DOMContentLoaded', () => {
    virtualPortraitLabels();
    const host = document.createElement('div'); host.id = 'yzzh-plugin-panel'; document.body.append(host);
    shadow = host.attachShadow({mode:'open'});
    shadow.innerHTML = `<style>
      :host{font:14px/1.5 system-ui,sans-serif;color:#192332}button,input{font:inherit}button{cursor:pointer;padding:9px 14px;border:1px solid #cbd4e0;border-radius:9px;background:white;color:#182638;margin:5px 5px 5px 0}button:hover{background:#eef3fc}button:disabled{opacity:.5;cursor:wait}
      .entry{position:fixed;right:20px;bottom:18px;z-index:2147483600;box-shadow:0 4px 18px #142d5140;background:#17293e;color:white;border:0}
      .overlay{position:fixed;inset:0;z-index:2147483601;background:#12233970;display:grid;place-items:center;padding:24px}.overlay[hidden]{display:none}.panel{background:#fff;border-radius:16px;box-shadow:0 10px 60px #101e3a55;width:min(680px,95vw);max-height:85vh;overflow:auto;padding:24px;box-sizing:border-box}
      h2{font-size:21px;margin:0 0 10px}h3{font-size:16px}p{color:#586578}label{display:block;margin:9px 0}input{display:block;box-sizing:border-box;width:100%;padding:8px;border:1px solid #bcc9d9;border-radius:7px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5fa;padding:12px;font:12px/1.6 monospace}section{border-top:1px solid #d8e0eb;margin-top:16px;padding-top:12px}.status{color:#944800}a{color:#2753a0}.close{float:right}details{margin:14px 0;border:1px solid #d8e0eb;border-radius:10px;padding:12px}summary{cursor:pointer;font-weight:600}.upload-info{padding:12px;background:#f1f6fc;border-radius:10px}.tos-fields[hidden]{display:none}
      select{display:block;box-sizing:border-box;width:100%;padding:8px;border:1px solid #bcc9d9;border-radius:7px;background:white;color:#192332;font:inherit}
      .asset-consent{display:flex;align-items:flex-start;gap:8px}.asset-consent input{width:18px;flex:none;margin-top:4px;padding:0}
    </style><button class="entry" type="button">登录并开始创作</button>
    <div class="overlay" hidden><div class="panel" role="dialog" aria-modal="true" aria-label="插件账号与设置">
      <button class="close" type="button">返回创作</button><h2 class="panel-title">账号与创作设置</h2><div class="account"></div>
      <p class="billing-info">沿用原来的创作流程。时间卡控制插件使用权，生成费用由自己的模型账号承担，不扣 CZMIYOU 余额。</p>
      <p class="status" role="status"></p>
      <div class="settings-view">
      <p class="login-destination"></p>
      <div class="unified-login"><p>使用原来的 CZMIYOU 统一登录页。登录后，在「我的应用」点击「米哟衣装智换」即可进入本地插件。</p><a class="login-link" href="https://mch39t7vkw.coze.site">前往 CZMIYOU 统一登录 →</a></div>
      <form class="login" hidden><label>账号<input name="username" required autocomplete="username"></label><label>密码<input name="password" type="password" required autocomplete="current-password"></label><label>登录身份<select name="role" required><option value="" selected disabled>请选择与账号中心一致的身份</option><option value="customer">客户</option><option value="staff">员工</option><option value="agent">代理</option><option value="partner">校企合作</option><option value="admin">管理员</option></select></label><button>登录 CZMIYOU</button></form>
      <form class="settings" hidden><h3>我的模型服务</h3><p class="configured"></p><div class="fields"></div>
        <div class="upload-info"><b>素材由插件自动上传</b><p>不用建桶、不用复制公网地址。上传前会确认素材去向；审核通过的人物 ID 自动复用。</p></div>
        <details class="identity-settings"><summary>火山素材库连接 · 虚拟人像入库时使用</summary><p>使用 AIGC 虚拟人像库。这里填写素材库连接凭据，不需要真人认证或存储桶。虚拟人像审核通过后，自动复用人物 ID。</p><div class="identity-fields"></div></details>
        <details><summary>高级设置 · 模型与上传方式</summary><div class="model-fields"></div><label>素材上传方式<select name="MEDIA_UPLOAD_MODE"><option value="temporary">自动临时上传 · 无需存储配置</option><option value="tos">使用自己的北京 TOS</option></select></label><p>自动上传使用第三方 Litterbox，文件通过链接可访问，服务声明约 3 天过期，插件不能提前删除。不适合保密素材；每次实际上传仍需确认。</p><div class="tos-fields" hidden><p>使用上方人物库 AK/SK 作为 TOS 凭据，还需具备对应桶的权限。</p></div></details>
        <button>保存并继续创作</button><button class="logout" type="button">退出账号</button></form>
      <section class="platform-status-view" hidden><h3>平台服务状态</h3><p class="platform-status" role="status"></p><ul class="platform-capabilities"></ul><p class="platform-balance"></p><button class="platform-logout" type="button">退出账号</button></section>
      <section><b>本地基础自检</b><p class="readiness-status" role="status">登录后自动检查本地基础功能。</p><ul class="readiness-checks"></ul><button class="readiness-check" type="button" disabled>重新检查</button><p>仅检查本地人脸打码、深度处理和视频工具；不读取你的素材，不下载模型。供应商权限、上传服务和付费生成需另行验证。OCR、人声分离及其他大型模型属于可选扩展。</p><a class="agent-workbench-link" href="/agent-workbench">查看 Agent 单段任务与预览 →</a></section>
      </div>
      <div class="approval-view" hidden><p class="approval-explanation">本次操作将沿用已保存在本机的配置，无需重新填写 API Key。请核对下面的素材去向或生成费用后决定是否继续。</p><div class="pending"></div></div>
    </div></div>`;
    dialog = shadow.querySelector('.overlay'); status = shadow.querySelector('.status'); account = shadow.querySelector('.account');
    form = shadow.querySelector('.login'); pending = shadow.querySelector('.pending'); settingsForm = shadow.querySelector('.settings');
    configured = shadow.querySelector('.configured'); button = shadow.querySelector('.entry');
    shadow.querySelector('.readiness-check').onclick = () => checkReadiness(true);
    button.onclick = () => {show('', hasAttention ? 'approval' : 'settings'); refresh();}; shadow.querySelector('.close').onclick = () => {dialog.hidden = true;};
    for (const [name,label,type] of fields) {
      const row = document.createElement('label'); row.textContent = label;
      const input = document.createElement('input'); input.name=name; input.type=type; input.autocomplete='off'; row.append(input);
      const target = name === 'ARK_API_KEY' ? '.fields' : name === 'TOS_BUCKET' ? '.tos-fields' : name.startsWith('TOS_') ? '.identity-fields' : '.model-fields';
      shadow.querySelector(target).append(row);
    }
    settingsForm.elements.MEDIA_UPLOAD_MODE.onchange = () => {settingsForm.dataset.uploadEdited = '1'; shadow.querySelector('.tos-fields').hidden = settingsForm.elements.MEDIA_UPLOAD_MODE.value !== 'tos';};
    form.onsubmit = async event => {event.preventDefault(); const submit = form.querySelector('button'); submit.disabled=true;
      try {await api('/api/login', Object.fromEntries(new FormData(form))); location.reload();} catch(e) {show(e.message);} finally {form.elements.password.value=''; submit.disabled=false;}};
    settingsForm.onsubmit = async event => {event.preventDefault();
      if (platformMode()) {
        for (const [name,,type] of fields) if (type === 'password') settingsForm.elements[name].value = '';
        show('平台服务由管理员统一配置，无需保存个人密钥。', hasAttention ? 'approval' : 'settings'); return;
      }
      if (hasAttention) {show('当前任务正在等待确认，已保存配置无需重填。请先处理本次请求。', 'approval'); return;}
      try {
      const saved = await api('/api/settings',{expected_owner:Number(owner),expected_session:context,values:Object.fromEntries(new FormData(settingsForm))});
      syncApiBadge(saved);
      delete settingsForm.dataset.uploadEdited;
      show('已保存在本机，API 配置状态已更新。未上传素材或发起付费请求。'); await refresh();
    } catch(e) {show(e.message);} finally {for (const [name,,type] of fields) if(type==='password') settingsForm.elements[name].value='';}};
    const logout = async () => {try {await api('/api/logout',{}); clearAccountState('已退出账号，请重新登录。'); location.reload();} catch(e) {show(e.message);}};
    shadow.querySelector('.logout').onclick = logout;
    shadow.querySelector('.platform-logout').onclick = logout;
    refresh(); setInterval(refresh,2500);
  });
})();
