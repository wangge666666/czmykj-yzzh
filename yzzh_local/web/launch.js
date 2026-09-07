/* Login handoff only. No uploads or generation are started by opening a project. */
(() => {
  'use strict';
  const state = new URLSearchParams(location.hash.slice(1)).get('state');
  history.replaceState(null, '', location.pathname);
  const loginOrigin = document.querySelector('meta[name="yzzh-login-origin"]').content;
  const nonce = document.querySelector('meta[name="yzzh-launch-nonce"]').content;
  const status = document.getElementById('launch-status');
  document.title = '正在打开米哟无限复刻';
  const heading = document.querySelector('h1');
  if (heading) heading.textContent = '正在打开米哟无限复刻';
  const parent = window.opener;
  let accepted = false;
  if (!parent || !/^[a-f0-9]{64}$/.test(state || '')) {
    status.textContent = '请回 CZMIYOU 统一登录后的「我的应用」，点击米哟无限复刻。';
    return;
  }
  const timer = setTimeout(() => {accepted = true; status.textContent = '连接已过期，请回统一登录页面重新点击米哟无限复刻。';}, 55000);
  window.addEventListener('message', async event => {
    if (accepted || event.origin !== loginOrigin || event.source !== parent
        || event.data?.type !== 'YZZH_LOGIN' || event.data.state !== state
        || typeof event.data.token !== 'string' || !event.data.token || event.data.token.length > 8192) return;
    accepted = true;
    clearTimeout(timer);
    status.textContent = '正在验证米哟无限复刻账号与时间卡…';
    try {
      const response = await fetch('/_plugin/launch/accept', {method:'POST',
        headers:{'Content-Type':'application/json','X-Yzzh-Request':'1'},
        body:JSON.stringify({nonce,token:event.data.token})});
      const result = await response.json();
      if (!response.ok || result.logged_in !== true) throw Error('LOGIN_NOT_ACCEPTED');
      parent.postMessage({type:'YZZH_OPENED',state}, loginOrigin);
      window.opener = null;
      location.replace('/');
    } catch (_) {
      parent.postMessage({type:'YZZH_LOGIN_FAILED',state}, loginOrigin);
      status.textContent = '未能完成账号验证，已有任务未删除。请返回统一登录页面重新登录，再点击米哟无限复刻。';
    }
  });
  parent.postMessage({type:'YZZH_READY',state,protocol:1}, loginOrigin);
})();
