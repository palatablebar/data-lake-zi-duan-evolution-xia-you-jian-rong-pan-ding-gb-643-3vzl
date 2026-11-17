/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async 'register-points'() {
    const result = await request('/api/recovery/points', {});
    return `已登记 ${result.points.length} 个恢复点`;
  },

  async 'freeze-latest'() {
    const points = await fetch('/api/recovery/points').then((r) => r.json());
    const target = points.points[points.points.length - 1];
    if (!target) return '没有可冻结的恢复点';
    const result = await request(`/api/recovery/points/${target.point_id}/freeze`, {});
    return `恢复点 ${result.point_id} 已冻结`;
  },

  async 'request-rollback'() {
    return submitRollback(false);
  },

  async 'retry-rollback'() {
    return submitRollback(false);
  },

  async 'resume-rollback'() {
    return submitRollback(true);
  },

  async 'interrupt-rollback'() {
    const orders = await fetch('/api/recovery/rollbacks').then((r) => r.json());
    const target = orders.orders[0];
    if (!target) return '还没有回滚单';
    const result = await request(`/api/recovery/rollbacks/${target.order_id}/interrupt`, {});
    return `回滚单 ${result.order_id} 已标记中断`;
  },
};

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    const error = new Error(message);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function splitList(id) {
  const node = document.getElementById(id);
  if (!node) return [];
  return node.value.split(',').map((item) => item.trim()).filter(Boolean);
}

async function submitRollback(resume) {
  const body = {
    migration_id: (document.getElementById('rb-migration') || {}).value || 'wave-2026-09',
    target_release_seq: Number((document.getElementById('rb-seq') || {}).value || 1),
    consumers: splitList('rb-consumers'),
    shared_reports: splitList('rb-reports'),
    resume,
  };
  const result = await request('/api/recovery/rollbacks', body);
  if (result.blockers && result.blockers.length) {
    throw new Error(result.blockers.map((item) => item.code).join('、'));
  }
  return `回滚单 ${result.order_id} 状态 ${result.status}（第 ${result.attempts} 次）`;
}
function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  let ok = false;
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    ok = true;
    if (name === 'run' || name.startsWith('register-points') ||
        name.includes('rollback') || name.startsWith('freeze')) {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    const blockers = error.payload && error.payload.detail && error.payload.detail.blockers;
    flash(target, blockers ? blockers.map((item) => item.code).join('、') : error.message, true);
  } finally {
    button.disabled = false;
  }
  return ok;
});
