/* Social Crawler - front-end controller (no build step, no framework). */

const POLL_MS = 2000;
const PAGE_SIZE = 25;
/* Threads is the adapter whose payload shape has been verified end to end,
   so it is the sensible default rather than whichever sorts first. */
const DEFAULT_PLATFORM = 'threads';

const PLATFORM_META = {
  threads:   { color: 'var(--p-threads)',   mark: '@'  },
  instagram: { color: 'var(--p-instagram)', mark: 'IG' },
  facebook:  { color: 'var(--p-facebook)',  mark: 'f'  },
  tiktok:    { color: 'var(--p-tiktok)',    mark: 'TT' },
};

const TERMINAL = ['completed', 'partial', 'failed', 'cancelled'];

const state = {
  platforms: [],
  selected: new Set(),
  activeJobId: null,
  pollTimer: null,
  resultJobId: null,
  offset: 0,
  total: 0,
};

/* ----------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* body was not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

const $ = (id) => document.getElementById(id);

function toast(message, kind = 'ok') {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 4600);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/* A metric that is null was never reported by the platform - which is not the
   same as zero, so it must not render as "0". */
function metric(value) {
  return value === null || value === undefined
    ? '<span class="nil">—</span>'
    : Number(value).toLocaleString('vi-VN');
}

function truncate(text, max = 160) {
  const clean = String(text ?? '').replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('vi-VN');
}

function platformMeta(key) {
  return PLATFORM_META[key] || { color: 'var(--muted)', mark: '?' };
}

function platformLabel(key) {
  const found = state.platforms.find((p) => p.platform === key);
  return found ? found.label : key;
}

function platformMark(key, className = 'pmark') {
  const meta = platformMeta(key);
  return `<span class="${className}" style="background:${meta.color}">${escapeHtml(meta.mark)}</span>`;
}

const STATUS_CLASS = {
  completed: 'ok', valid: 'ok', running: 'warn', logging_in: 'warn',
  pending: 'idle', saved: 'idle', missing: 'idle', partial: 'warn',
  failed: 'err', invalid: 'err', error: 'err', needs_auth: 'err', cancelled: 'idle',
  blocked: 'err',
};

const STATUS_TEXT = {
  completed: 'xong', partial: 'một phần', failed: 'lỗi', cancelled: 'đã huỷ',
  running: 'đang chạy', pending: 'chờ', needs_auth: 'cần đăng nhập',
  blocked: 'bị chặn',
  valid: 'hợp lệ', invalid: 'hết hạn', saved: 'đã lưu', missing: 'chưa có',
  logging_in: 'đang đăng nhập', error: 'lỗi',
};

function badge(status) {
  const label = STATUS_TEXT[status] || status;
  return `<span class="badge ${STATUS_CLASS[status] || 'idle'}">${escapeHtml(label)}</span>`;
}

/* ---------------------------------------------------------------- platforms */

async function loadPlatforms() {
  state.platforms = await api('/api/platforms');
  const container = $('platforms');
  container.innerHTML = '';

  const preferred = state.platforms.some((p) => p.platform === DEFAULT_PLATFORM)
    ? DEFAULT_PLATFORM
    : (state.platforms[0] || {}).platform;

  state.platforms.forEach((platform) => {
    const meta = platformMeta(platform.platform);
    const label = document.createElement('label');
    label.className = 'chip';
    label.style.setProperty('--c', meta.color);
    label.innerHTML =
      `<input type="checkbox" value="${platform.platform}" />` +
      `<span class="mark">${escapeHtml(meta.mark)}</span>` +
      `${escapeHtml(platform.label)}`;

    const input = label.querySelector('input');
    if (platform.platform === preferred) {
      input.checked = true;
      label.classList.add('on');
      state.selected.add(platform.platform);
    }

    input.addEventListener('change', () => {
      label.classList.toggle('on', input.checked);
      if (input.checked) state.selected.add(platform.platform);
      else state.selected.delete(platform.platform);
    });

    container.appendChild(label);
  });
}

/* ----------------------------------------------------------------- sessions */

async function loadSessions() {
  const sessions = await api('/api/sessions');
  const container = $('sessions');
  container.innerHTML = sessions.map((session) => {
    const meta = state.platforms.find((p) => p.platform === session.platform);
    const optional = meta && meta.requires_login === false;
    const note = session.message
      || (session.saved_at ? `Lưu lúc ${formatDate(session.saved_at)}`
          : optional ? 'Không bắt buộc — crawl được khi chưa đăng nhập'
          : 'Chưa đăng nhập lần nào');
    return `
      <div class="session-row">
        <div class="session-meta">
          ${platformMark(session.platform)}
          <div class="session-text">
            <strong>${escapeHtml(platformLabel(session.platform))}</strong>
            <small>${escapeHtml(note)}</small>
          </div>
        </div>
        <div class="session-actions">
          ${badge(session.status)}
          <button class="btn-ghost" data-login="${session.platform}">Đăng nhập</button>
          <button class="btn-ghost" data-validate="${session.platform}">Kiểm tra</button>
          <button class="btn-ghost danger" data-clear="${session.platform}">Xoá</button>
        </div>
      </div>`;
  }).join('');
}

async function handleSessionClick(event) {
  const target = event.target.closest('button');
  if (!target) return;

  const { login, validate, clear } = target.dataset;
  if (!login && !validate && !clear) return;

  target.disabled = true;
  try {
    if (login) {
      await api(`/api/sessions/${login}/login`, { method: 'POST' });
      toast('Cửa sổ trình duyệt đã mở — hãy đăng nhập trong đó.');
      setTimeout(loadSessions, 4000);
    } else if (validate) {
      const result = await api(`/api/sessions/${validate}/validate`, { method: 'POST' });
      toast(`${platformLabel(validate)}: ${STATUS_TEXT[result.status] || result.status}`,
            result.status === 'valid' ? 'ok' : 'err');
    } else if (clear) {
      await api(`/api/sessions/${clear}`, { method: 'DELETE' });
      toast(`Đã xoá phiên ${platformLabel(clear)}`);
    }
    await loadSessions();
  } catch (error) {
    toast(error.message, 'err');
  } finally {
    target.disabled = false;
  }
}

/* --------------------------------------------------------------------- jobs */

async function submitCrawl(event) {
  event.preventDefault();
  if (state.selected.size === 0) {
    toast('Chọn ít nhất một nền tảng.', 'err');
    return;
  }

  const button = $('crawl-btn');
  button.disabled = true;
  button.classList.add('loading');
  try {
    const job = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        keyword: $('keyword').value.trim(),
        platforms: [...state.selected],
        search_filter: $('search-filter').value,
        max_posts: Number($('max-posts').value),
        max_comments_per_post: Number($('max-comments').value),
        include_comments: $('include-comments').checked,
      }),
    });
    toast(`Job #${job.id} đã vào hàng đợi.`);
    state.activeJobId = job.id;
    renderJob(job);
    startPolling();
    await loadJobOptions();
  } catch (error) {
    toast(error.message, 'err');
  } finally {
    button.disabled = false;
    button.classList.remove('loading');
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(pollActiveJob, POLL_MS);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollActiveJob() {
  if (!state.activeJobId) return stopPolling();
  try {
    const job = await api(`/api/jobs/${state.activeJobId}`);
    renderJob(job);
    refreshWorker();

    if (TERMINAL.includes(job.status)) {
      stopPolling();
      toast(`Job #${job.id}: ${STATUS_TEXT[job.status] || job.status}`,
            job.status === 'failed' ? 'err' : 'ok');
      state.resultJobId = job.id;
      $('result-job').value = String(job.id);
      state.offset = 0;
      await Promise.all([loadPosts(), loadStats(), loadSessions()]);
    }
  } catch (error) {
    stopPolling();
    toast(error.message, 'err');
  }
}

function renderJob(job) {
  const done = TERMINAL.includes(job.status);
  $('cancel-btn').hidden = done;

  const targets = job.targets.map((target) => {
    const total = target.posts_found || job.max_posts || 1;
    const percent = Math.min(100, Math.round((target.posts_crawled / total) * 100));
    const meta = platformMeta(target.platform);
    const running = target.status === 'running';
    const note = target.error || target.message || '';

    return `
      <div class="target ${running ? 'running' : ''}" style="--c:${meta.color}">
        <div class="target-head">
          <span class="target-name">${platformMark(target.platform)}${escapeHtml(platformLabel(target.platform))}</span>
          ${badge(target.status)}
        </div>
        <div class="target-stats">
          <b>${target.posts_crawled}</b>/${target.posts_found} bài ·
          <b>${target.comments_crawled}</b> comment${target.posts_failed ? ` · <b>${target.posts_failed}</b> lỗi` : ''}
        </div>
        ${note ? `<div class="target-msg ${target.error ? 'error' : ''}">${escapeHtml(truncate(note, 200))}</div>` : ''}
        <div class="bar"><i style="width:${percent}%"></i></div>
      </div>`;
  }).join('');

  $('job-progress').innerHTML = `
    <div class="job-head">
      <span class="job-title">Job #${job.id} · <em>${escapeHtml(job.keyword)}</em></span>
      ${badge(job.status)}
    </div>
    ${targets}
    ${job.error ? `<div class="target-msg error" style="margin-top:10px">${escapeHtml(job.error)}</div>` : ''}`;
}

async function cancelJob() {
  if (!state.activeJobId) return;
  try {
    const job = await api(`/api/jobs/${state.activeJobId}/cancel`, { method: 'POST' });
    renderJob(job);
    toast(`Đã yêu cầu huỷ job #${job.id}.`);
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function loadJobOptions() {
  const { items } = await api('/api/jobs?limit=50');
  const select = $('result-job');
  const current = select.value;

  select.innerHTML = '<option value="">Tất cả job</option>';
  items.forEach((job) => {
    const option = document.createElement('option');
    option.value = String(job.id);
    option.textContent = `#${job.id} · ${truncate(job.keyword, 26)}`;
    select.appendChild(option);
  });
  if (current) select.value = current;
}

/* ------------------------------------------------------------------ results */

function skeletonRows(count = 5) {
  const cells = Array.from({ length: 9 }, () => '<td><span></span></td>').join('');
  return Array.from({ length: count }, () => `<tr class="skeleton">${cells}</tr>`).join('');
}

async function loadPosts() {
  const body = $('posts-body');
  body.innerHTML = skeletonRows();

  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.offset) });
  if (state.resultJobId) params.set('job_id', String(state.resultJobId));

  const data = await api(`/api/posts?${params.toString()}`);
  state.total = data.total;

  if (data.items.length === 0) {
    body.innerHTML = `<tr class="row-empty"><td colspan="9">
      Chưa có dữ liệu — chạy một lần crawl để bắt đầu.
    </td></tr>`;
  } else {
    body.innerHTML = data.items.map((post) => `
      <tr data-post="${post.id}">
        <td><span class="plat-cell">${platformMark(post.platform)}${escapeHtml(platformLabel(post.platform))}</span></td>
        <td class="account">${escapeHtml(post.account) || '<span class="nil">—</span>'}</td>
        <td class="content">${escapeHtml(truncate(post.content, 118)) || '<span class="nil">(không có text)</span>'}</td>
        <td class="num">${metric(post.metrics.likes)}</td>
        <td class="num">${metric(post.metrics.comments)}</td>
        <td class="num">${metric(post.metrics.reposts)}</td>
        <td class="num">${metric(post.metrics.shares)}</td>
        <td class="num">${metric(post.metrics.views)}</td>
        <td><span class="src-tag ${post.metrics.source === 'api' ? 'api' : ''}">${escapeHtml(post.metrics.source)}${post.metrics.approximate ? '~' : ''}</span></td>
      </tr>`).join('');
  }

  const from = data.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + PAGE_SIZE, data.total);
  $('page-info').textContent = `${from}–${to} / ${data.total}`;
  $('prev-page').disabled = state.offset === 0;
  $('next-page').disabled = state.offset + PAGE_SIZE >= data.total;
}

async function openPost(postId) {
  try {
    const { post, comments } = await api(`/api/posts/${postId}`);

    const metrics = [
      ['Likes', post.metrics.likes], ['Comments', post.metrics.comments],
      ['Reposts', post.metrics.reposts], ['Shares', post.metrics.shares],
      ['Views', post.metrics.views], ['Saves', post.metrics.saves],
    ].map(([label, value]) => `
      <div class="metric-box"><span>${label}</span><strong>${metric(value)}</strong></div>`).join('');

    const rendered = comments.map((comment) => `
      <div class="comment ${comment.depth > 1 ? 'reply' : ''} ${comment.is_author_reply ? 'author-reply' : ''}">
        <div class="comment-top">
          <b>${escapeHtml(comment.author)}</b>
          ${comment.is_author_reply ? '<span class="tag-author">tác giả</span>' : ''}
          <small>${metric(comment.like_count)} likes · ${formatDate(comment.posted_at)}</small>
        </div>
        <p>${escapeHtml(comment.content)}</p>
      </div>`).join('')
      || '<p class="empty-state" style="padding:18px">Không có comment nào được thu thập.</p>';

    $('modal-body').innerHTML = `
      <h2 class="modal-title">${platformMark(post.platform)}${escapeHtml(post.account)}</h2>
      <div class="metric-grid">${metrics}</div>
      <div class="post-text">${escapeHtml(post.content) || '<span class="nil">(không có text)</span>'}</div>
      <div class="modal-meta">
        <span>Đăng: ${formatDate(post.posted_at)}</span>
        <span>Nguồn số liệu: <b>${escapeHtml(post.metrics.source)}</b>${post.metrics.approximate ? ' (làm tròn)' : ' (chính xác)'}</span>
        <a href="${escapeHtml(post.url)}" target="_blank" rel="noopener noreferrer">Mở bài gốc ↗</a>
      </div>
      <h3 class="comments-head">Comments (${comments.length})</h3>
      ${rendered}`;
    $('modal').hidden = false;
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function exportData(format) {
  try {
    const response = await fetch('/api/exports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format, job_id: state.resultJobId || null }),
    });
    if (!response.ok) throw new Error(`Export thất bại (${response.status})`);

    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = match ? match[1] : `export.${format}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    toast('Đã tải file export.');
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function loadStats() {
  const stats = await api('/api/stats');
  const format = (value) => Number(value || 0).toLocaleString('vi-VN');
  $('kpi-posts').textContent = format(stats.posts);
  $('kpi-comments').textContent = format(stats.comments);
  $('kpi-authors').textContent = format(stats.authors);
  $('kpi-jobs').textContent = format(stats.jobs);
}

async function refreshWorker() {
  const pill = $('worker-pill');
  try {
    const worker = await api('/api/worker');
    if (!worker.worker_alive) {
      pill.className = 'worker-pill down';
      $('worker-text').textContent = 'worker dừng';
    } else if (worker.running) {
      pill.className = 'worker-pill busy';
      $('worker-text').textContent = `đang chạy job #${worker.running}`;
    } else {
      pill.className = 'worker-pill live';
      $('worker-text').textContent = worker.queued ? `${worker.queued} job chờ` : 'sẵn sàng';
    }
  } catch (_) {
    pill.className = 'worker-pill down';
    $('worker-text').textContent = 'mất kết nối';
  }
}

/* --------------------------------------------------------------------- init */

function wireEvents() {
  $('crawl-form').addEventListener('submit', submitCrawl);
  $('sessions').addEventListener('click', handleSessionClick);
  $('cancel-btn').addEventListener('click', cancelJob);
  $('modal-close').addEventListener('click', () => { $('modal').hidden = true; });
  $('modal').addEventListener('click', (event) => {
    if (event.target.id === 'modal') $('modal').hidden = true;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') $('modal').hidden = true;
  });

  $('posts-body').addEventListener('click', (event) => {
    const row = event.target.closest('tr[data-post]');
    if (row) openPost(Number(row.dataset.post));
  });

  $('result-job').addEventListener('change', (event) => {
    state.resultJobId = event.target.value ? Number(event.target.value) : null;
    state.offset = 0;
    loadPosts().catch((error) => toast(error.message, 'err'));
  });

  $('prev-page').addEventListener('click', () => {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    loadPosts().catch((error) => toast(error.message, 'err'));
  });
  $('next-page').addEventListener('click', () => {
    if (state.offset + PAGE_SIZE < state.total) {
      state.offset += PAGE_SIZE;
      loadPosts().catch((error) => toast(error.message, 'err'));
    }
  });

  document.querySelectorAll('[data-export]').forEach((button) => {
    button.addEventListener('click', () => exportData(button.dataset.export));
  });
}

async function resumeRunningJob() {
  const { items } = await api('/api/jobs?limit=5');
  const active = items.find((job) => ['pending', 'running'].includes(job.status));
  if (active) {
    state.activeJobId = active.id;
    renderJob(active);
    startPolling();
  }
}

async function init() {
  wireEvents();
  try {
    await loadPlatforms();
    await Promise.all([loadSessions(), loadStats(), loadJobOptions(), loadPosts(), refreshWorker()]);
    await resumeRunningJob();
    const needLogin = state.platforms.filter((p) => p.requires_login).map((p) => p.label);
    $('form-hint').textContent = needLogin.length
      ? `${needLogin.join(', ')} cần đăng nhập một lần trước khi crawl. `
        + 'TikTok crawl được khi chưa đăng nhập, nhưng cần cửa sổ hiển thị (không headless).'
      : 'Sẵn sàng crawl.';
  } catch (error) {
    toast(`Không kết nối được backend: ${error.message}`, 'err');
  }
  setInterval(refreshWorker, 8000);
}

init();
