'use strict';

function startupConnectionPlan(state = {}) {
  const settings = state.settings || {};
  const ssh = settings.ssh || {};
  const agent = settings.agent || {};
  const missingSsh = [];
  const auth = ['password', 'key', 'agent'].includes(ssh.auth) ? ssh.auth : 'password';

  if (!String(ssh.host || '').trim()) missingSsh.push('服务器地址');
  if (!String(ssh.username || '').trim()) missingSsh.push('用户名');
  if (auth === 'password' && ssh.passwordCredential?.saved !== true) missingSsh.push('已保存的 SSH 密码');
  if (auth === 'key' && !String(ssh.privateKeyPath || '').trim()) missingSsh.push('SSH 私钥');

  const missingAgent = [];
  if (!String(agent.executable || '').trim()) missingAgent.push('Code Agent 启动命令');

  return {
    missingSsh,
    missingAgent,
    settingsTab: missingSsh.length ? 'ssh' : missingAgent.length ? 'agent' : null,
    connectSsh: missingSsh.length === 0 && !['connected', 'connecting'].includes(state.ssh?.state),
    startAgent: missingAgent.length === 0 && !['running', 'starting'].includes(state.agent?.state),
  };
}

module.exports = { startupConnectionPlan };
