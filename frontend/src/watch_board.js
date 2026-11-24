// 值班室持续拥堵盯警页面
const token = () => localStorage.getItem('airport_token');
const user = () => JSON.parse(localStorage.getItem('airport_user') || 'null');
const BASE = '/api/congestion-watch';
let timer = null;

async function api(path, options = {}) {
  const r = await fetch(BASE + path, {
    ...options,
    headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token() || ''}`},
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `请求失败 (${r.status})`);
  return d;
}

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const dt = v => v ? new Date(v).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
const actionLabel = {none: '留观', opened: '开单', cleared: '销警', archived: '只留档'};
const fStatusLabel = {queued: '排队中', running: '计算中', ready: '已就绪', failed: '失败', skipped: '已跳过', archived: '已留档', published: '已发布', blocked: '已阻断'};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bins(values, threshold) {
  return values.map(v => {
    const over = Number(v) > threshold;
    return `<span class="watch-bin ${over ? 'over' : ''}">${v}<small>${over ? '超' : ''}</small></span>`;
  }).join('');
}

function toast(host, msg, ok = true) {
  const t = el('p', ok ? 'watch-toast ok' : 'watch-toast err', msg);
  host.append(t);
  setTimeout(() => t.remove(), 3200);
}

// ------------------------------------------------------------- 值班控制
async function renderDuty(root, board, refresh) {
  const box = root.querySelector('#watch-duty');
  box.replaceChildren();
  const u = user();
  const head = el('div', 'panel-head');
  head.append(Object.assign(el('div'), {innerHTML: '<small class="eyebrow">DUTY SHIFT</small><h3>值班与交接</h3>'}));
  box.append(head);

  const s = board.active_shift;
  if (!s) {
    const form = el('form', 'watch-duty-form');
    form.innerHTML = `<label>班次编号<input name="shift_code" required minlength="2" placeholder="如 DUTY-AM-1121"></label>
      <label>开班说明（可选）<input name="note" maxlength="500" placeholder="开班备注"></label>
      <button class="primary" ${u.role === 'viewer' ? 'disabled' : ''}>开班值守</button>
      <p class="muted">${u.role === 'viewer' ? '观察员仅可查看，不能开班' : '开班后才能签收警单'}</p>`;
    form.onsubmit = async e => {
      e.preventDefault();
      const o = Object.fromEntries(new FormData(form));
      try { await api('/shifts', {method: 'POST', body: JSON.stringify(o)}); await refresh(); }
      catch (err) { toast(box, err.message, false); }
    };
    box.append(form);
    return;
  }

  const info = el('div', 'watch-shift-info');
  info.innerHTML = `<div><small>当班班次</small><strong>${esc(s.shift_code)}</strong>
    <span>${esc(s.owner_name)} · ${dt(s.started_at)} 起</span></div>
    <span class="watch-pill ${board.is_on_duty ? 'mine' : ''}">${board.is_on_duty ? '我是当班人' : '他人当班'}</span>`;
  box.append(info);

  if (u.role !== 'viewer') {
    const form = el('form', 'watch-duty-form');
    const candidates = await api('/handover-candidates');
    const opts = candidates.filter(c => c.id !== s.owner_id)
      .map(c => `<option value="${c.id}">${esc(c.display_name)}（${esc(c.username)} · ${c.role === 'supervisor' ? '主管' : '调度员'}）</option>`).join('');
    form.innerHTML = `<label>接班人<select name="to_user_id" required>${opts}</select></label>
      <label>交接说明<input name="note" maxlength="500" placeholder="未销警单将交给接班人重新签收"></label>
      <button class="primary" ${board.is_on_duty ? '' : 'disabled title="只有当班人可以发起交接"'}>交接下班</button>`;
    form.onsubmit = async e => {
      e.preventDefault();
      const o = Object.fromEntries(new FormData(form));
      o.to_user_id = Number(o.to_user_id);
      try { await api('/shifts/handover', {method: 'POST', body: JSON.stringify(o)}); await refresh(); }
      catch (err) { toast(box, err.message, false); }
    };
    box.append(form);
  }
}

// ------------------------------------------------------------- 未销警单
function renderOpenAlerts(root, board, refresh) {
  const box = root.querySelector('#watch-open-alerts');
  box.replaceChildren();
  const head = el('div', 'panel-head');
  head.append(Object.assign(el('div'), {innerHTML: `<small class="eyebrow">OPEN ALERTS</small><h3>未销警单（每波次一张）</h3>`}),
    el('span', 'count-pill', String(board.open_alerts.length)));
  box.append(head);
  const u = user();
  if (!board.open_alerts.length) { box.append(el('p', 'empty-inline', '当前没有未销警单')); return; }

  for (const a of board.open_alerts) {
    const basis = a.open_basis || {};
    const card = el('div', 'watch-alert-card');
    card.innerHTML = `
      <div class="watch-alert-top">
        <div><b class="mono">#${a.id} ${esc(a.wave_code)}</b><span>${esc(a.terminal)} · 开单于 ${dt(a.created_at)}</span></div>
        <span class="badge open">未销警</span>
      </div>
      <div class="watch-basis">
        <small>开单依据：${esc(basis.rule || '')}（阈值 ${a.open_basis?.threshold ?? board.threshold} 件）</small>
        <div class="watch-bins">${bins(basis.predicted_next_4 || [], board.threshold)}</div>
        <small class="muted">关联预测申请：${(basis.forecast_ids || []).join('、')}</small>
      </div>
      <div class="watch-alert-actions">
        <span class="watch-pill ${a.signed_by_current_shift ? 'signed' : 'unsigned'}">
          ${a.signed_by_current_shift ? '本班次已签收' : '本班次未签收'}</span>
        <button class="small-btn dark" data-basis="${a.id}">开单/销警依据与签收记录</button>
        ${board.is_on_duty && u.role !== 'viewer' && !a.signed_by_current_shift
          ? `<button class="small-btn" data-sign="${a.id}">当班签收</button>` : ''}
      </div>`;
    box.append(card);
  }
  box.querySelectorAll('[data-sign]').forEach(btn => {
    btn.onclick = async () => {
      const note = prompt('签收说明（可留空）', '已通知机动岗，持续盯防') ?? '';
      try { await api(`/alerts/${btn.dataset.sign}/signoff`, {method: 'POST', body: JSON.stringify({note})}); await refresh(); }
      catch (err) { toast(box, err.message, false); }
    };
  });
  box.querySelectorAll('[data-basis]').forEach(btn => { btn.onclick = () => showAlertDetail(Number(btn.dataset.basis), board.threshold); });
}

// ----------------------------------------------------- 警单详情（依据+各班签收）
async function showAlertDetail(alertId, threshold) {
  const d = await api(`/alerts/${alertId}`);
  const backdrop = el('div', 'modal-backdrop');
  const modal = el('div', 'modal watch-detail');
  const a = d.alert;
  const signoffRows = d.signoffs.map(g =>
    `<li><b>${esc(g.signer_name)}（${esc(g.signer_username)}）</b> · ${esc(g.shift_code)} · ${dt(g.signed_at)}
     <small>${esc(g.note || '无说明')}</small></li>`).join('') || '<li class="muted">暂无签收记录</li>';
  const evalRows = d.evaluations.map(e => `
    <tr>
      <td>#${e.forecast_id}</td>
      <td><div class="watch-bins mini">${bins(e.predicted_next_4, e.threshold)}</div></td>
      <td>${e.breach ? '相邻突破' : '未突破'}</td>
      <td>${e.consecutive_breach} / ${e.consecutive_low}</td>
      <td><span class="watch-action-tag ${e.action}">${actionLabel[e.action] || e.action}</span></td>
      <td>${dt(e.evaluated_at)}</td>
    </tr>`).join('');
  modal.innerHTML = `
    <button class="modal-x">×</button>
    <small class="eyebrow">ALERT #${a.id}</small>
    <h2>${esc(a.wave_code)} 警单依据</h2>
    <div class="watch-detail-grid">
      <section><h4>开单依据</h4>
        <p>${esc(a.open_basis?.rule || '—')}</p>
        <p class="muted">阈值 ${a.open_basis?.threshold ?? threshold} 件 · 关联预测 ${(a.open_basis?.forecast_ids || []).join('、')}</p>
        <div class="watch-bins">${bins(a.open_basis?.predicted_next_4 || [], threshold)}</div>
      </section>
      <section><h4>销警依据</h4>
        ${a.status === 'cleared'
          ? `<p>${esc(a.clear_basis?.rule || '—')}</p>
             <p class="muted">销警于 ${dt(a.cleared_at)} · 关联预测 ${(a.clear_basis?.forecast_ids || []).join('、')}</p>
             <div class="watch-bins">${bins(a.clear_basis?.predicted_next_4 || [], threshold)}</div>`
          : '<p class="muted">警单尚未销警</p>'}
      </section>
    </div>
    <h4>各班签收记录</h4><ul class="watch-signoffs">${signoffRows}</ul>
    <h4>预测判定流水</h4>
    <div class="table-panel"><table><thead><tr><th>预测</th><th>未来四档（件）</th><th>突破</th><th>连续突破/低位</th><th>动作</th><th>时间</th></tr></thead>
    <tbody>${evalRows}</tbody></table></div>`;
  backdrop.append(modal);
  document.body.append(backdrop);
  modal.querySelector('.modal-x').onclick = () => backdrop.remove();
  backdrop.onclick = e => { if (e.target === backdrop) backdrop.remove(); };
}

// ------------------------------------------------------------- 申请队列
async function renderQueue(root, board, refresh) {
  const box = root.querySelector('#watch-queue');
  box.replaceChildren();
  const head = el('div', 'panel-head');
  head.append(Object.assign(el('div'), {innerHTML: '<small class="eyebrow">FORECAST QUEUE</small><h3>预测申请（按申请顺序处理）</h3>'}));
  box.append(head);
  const u = user();
  if (!board.queue.length) box.append(el('p', 'empty-inline', '当前没有排队或计算中的申请'));
  for (const f of board.queue) {
    const row = el('div', 'watch-queue-row');
    row.innerHTML = `<div><b class="mono">#${f.id} ${esc(f.wave_code)}</b><span>${esc(f.terminal)} · 第 ${f.attempts + 1} 次尝试 · ${dt(f.created_at)} 申请</span></div>
      <span class="badge ${f.status}">${fStatusLabel[f.status] || f.status}</span>
      ${u.role === 'supervisor' ? `<button class="small-btn" data-skip="${f.id}">跳过卡住申请</button>` : ''}`;
    box.append(row);
  }
  box.querySelectorAll('[data-skip]').forEach(btn => {
    btn.onclick = async () => {
      const reason = prompt('跳过原因（至少 3 个字）', '数据源长时间无响应，先跳过');
      if (!reason) return;
      try { await api(`/forecasts/${btn.dataset.skip}/skip`, {method: 'POST', body: JSON.stringify({reason})}); await refresh(); }
      catch (err) { toast(box, err.message, false); }
    };
  });

  // 失败 / 跳过 / 留档的申请：重试入口（archived 不可重试）
  const all = await (await fetch('/api/forecasts', {headers: {Authorization: `Bearer ${token()}`}})).json();
  const stuck = all.filter(f => ['failed', 'skipped', 'archived'].includes(f.status)).slice(0, 8);
  if (stuck.length) {
    box.append(el('h4', 'watch-subhead', '卡住与留档申请'));
    for (const f of stuck) {
      const row = el('div', 'watch-queue-row');
      row.innerHTML = `<div><b class="mono">#${f.id} ${esc(f.code)}</b><span>${esc(f.failure_reason || '')}</span></div>
        <span class="badge ${f.status}">${fStatusLabel[f.status] || f.status}</span>
        ${f.status === 'archived'
          ? '<span class="muted">迟到数据只留档，不可重试</span>'
          : (u.role !== 'viewer' ? '<button class="small-btn dark" data-retry="' + f.id + '">重试</button>' : '')}`;
      box.append(row);
    }
    box.querySelectorAll('[data-retry]').forEach(btn => {
      btn.onclick = async () => {
        try { await fetch(`/api/forecasts/${btn.dataset.retry}/retry`, {method: 'POST', headers: {Authorization: `Bearer ${token()}`}}); await refresh(); }
        catch (err) { toast(box, err.message, false); }
      };
    });
  }
}

// ------------------------------------------------------------- 判定流水
function renderEvaluations(root, board, evaluations) {
  const box = root.querySelector('#watch-evals');
  box.replaceChildren();
  const head = el('div', 'panel-head');
  head.append(Object.assign(el('div'), {innerHTML: '<small class="eyebrow">EVALUATION LOG</small><h3>开单 / 销警判定依据流水</h3>'}));
  box.append(head);
  if (!evaluations.length) { box.append(el('p', 'empty-inline', '暂无判定记录')); return; }
  const rows = evaluations.map(e => `
    <tr>
      <td class="mono">${esc(e.wave_code)}<small>#${e.forecast_id}</small></td>
      <td><div class="watch-bins mini">${bins(e.predicted_next_4, e.threshold)}</div></td>
      <td>${e.breach ? '<span class="watch-risk">相邻两档超 ' + e.threshold + '</span>' : '未突破'}</td>
      <td>${e.consecutive_breach} / ${e.consecutive_low}</td>
      <td><span class="watch-action-tag ${e.action}">${actionLabel[e.action] || e.action}</span></td>
      <td>${dt(e.evaluated_at)}</td>
    </tr>`).join('');
  box.insertAdjacentHTML('beforeend', `<div class="table-panel"><table>
    <thead><tr><th>波次 / 预测</th><th>未来四档（件）</th><th>突破判定</th><th>连续突破/低位</th><th>动作</th><th>时间</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`);
}

// ------------------------------------------------------------- 警单台账
async function renderLedger(root) {
  const box = root.querySelector('#watch-ledger');
  box.replaceChildren();
  const head = el('div', 'panel-head');
  head.append(Object.assign(el('div'), {innerHTML: '<small class="eyebrow">ALERT LEDGER</small><h3>警单台账（含已销警）</h3>'}));
  box.append(head);
  const alerts = await api('/alerts');
  if (!alerts.length) { box.append(el('p', 'empty-inline', '暂无警单')); return; }
  const rows = alerts.map(a => `
    <tr>
      <td class="mono">#${a.id} ${esc(a.wave_code)}</td>
      <td><span class="badge ${a.status}">${a.status === 'open' ? '未销警' : '已销警'}</span></td>
      <td>${dt(a.created_at)}</td>
      <td>${a.status === 'cleared' ? dt(a.cleared_at) : '—'}</td>
      <td>${esc(a.open_basis?.rule || '—')}</td>
      <td>${esc(a.clear_basis?.rule || '—')}</td>
      <td><button class="small-btn" data-ledger="${a.id}">详情</button></td>
    </tr>`).join('');
  box.insertAdjacentHTML('beforeend', `<div class="table-panel"><table>
    <thead><tr><th>警单</th><th>状态</th><th>开单时间</th><th>销警时间</th><th>开单依据</th><th>销警依据</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table></div>`);
  box.querySelectorAll('[data-ledger]').forEach(btn => { btn.onclick = () => showAlertDetail(Number(btn.dataset.ledger), 200); });
}

export async function mountWatch(root) {
  if (timer) { clearInterval(timer); timer = null; }
  root.innerHTML = `
    <div class="toolbar"><div><small class="eyebrow">CONGESTION WATCH</small>
      <p class="toolbar-note">未来四档中相邻两档超过 200 件、且连续两笔预测突破即开单；连续两笔四档均在 200 件以内才销警。每波次只保留一张未销警单。</p></div>
      <button class="primary" id="watch-refresh">刷新</button></div>
    <div class="watch-metrics" id="watch-metrics"></div>
    <div class="watch-grid">
      <section class="panel" id="watch-duty"></section>
      <section class="panel" id="watch-queue"></section>
    </div>
    <section class="panel" id="watch-open-alerts" style="margin-top:18px"></section>
    <div class="watch-grid" style="margin-top:18px">
      <section class="panel" id="watch-evals"></section>
      <section class="panel" id="watch-ledger"></section>
    </div>`;

  async function refresh() {
    try {
      const board = await api('/board');
      const evaluations = await api('/evaluations');
      const m = root.querySelector('#watch-metrics');
      m.innerHTML = `
        <div class="metric"><small>未销警单</small><strong>${board.open_alerts.length}</strong><span>每波次至多一张</span></div>
        <div class="metric orange"><small>排队 / 计算中申请</small><strong>${board.queue.length}</strong><span>按申请顺序处理</span></div>
        <div class="metric"><small>判定阈值</small><strong>${board.threshold}</strong><span>件 · 相邻两档</span></div>
        <div class="metric ${board.is_on_duty ? '' : 'red'}"><small>当班状态</small><strong style="font-size:20px">${board.active_shift ? esc(board.active_shift.owner_name) : '未开班'}</strong><span>${board.is_on_duty ? '我是当班人，可签收' : '交接后需重新签收'}</span></div>`;
      await renderDuty(root, board, refresh);
      await renderQueue(root, board, refresh);
      renderOpenAlerts(root, board, refresh);
      renderEvaluations(root, board, evaluations);
      await renderLedger(root);
    } catch (err) {
      toast(root, err.message, false);
    }
  }

  root.querySelector('#watch-refresh').onclick = refresh;
  await refresh();
  timer = setInterval(refresh, 8000);
}

export function unmountWatch() {
  if (timer) { clearInterval(timer); timer = null; }
}
