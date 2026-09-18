const tg = window.Telegram?.WebApp;
const state = { data: null, page: 'today' };

if (tg) {
  tg.ready();
  tg.expand();
  tg.enableClosingConfirmation?.();
}

const $ = (s) => document.querySelector(s);
const content = $('#content');
const modal = $('#modal');
const modalBody = $('#modal-body');

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

function haptic(type = 'light') {
  try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {}
}

function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(window.__toast); window.__toast = setTimeout(() => el.classList.remove('show'), 2500);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить запрос');
  return data;
}

async function load() {
  if (!tg?.initData) {
    content.innerHTML = `<section class="card"><h2>Открой приложение из Telegram</h2><p>Mini App работает внутри Telegram и использует защищённую авторизацию Telegram.</p></section>`;
    return;
  }
  try {
    state.data = await api('/api/state');
    render();
  } catch (e) {
    content.innerHTML = `<section class="card"><h2>Не удалось загрузить данные</h2><p>${esc(e.message)}</p><button class="primary-button" onclick="load()" style="margin-top:14px">Повторить</button></section>`;
  }
}

function render() {
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === state.page));
  if (state.page === 'today') renderToday();
  else if (state.page === 'progress') renderProgress();
  else if (state.page === 'history') renderHistory();
  else renderMore();
}

function dateLabel() {
  return new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date());
}

function renderToday() {
  const d = state.data;
  const r = d.reading;
  if (!r) {
    content.innerHTML = `<div class="date">${dateLabel()}</div><section class="card empty"><div style="font-size:42px">📖</div><h2>Чтение ещё не задано</h2><p>Администратор пока не установил сегодняшнее чтение.</p></section>`;
    return;
  }
  const status = d.my_reflection?.status;
  const statusHtml = status ? `<span class="status ${status === 'published' ? 'success' : status === 'rejected' ? 'danger' : ''}">${statusLabel(status)}</span>` : '';
  content.innerHTML = `
    <div class="date">${dateLabel()}</div>
    <section class="hero">
      <div class="hero-label">Сегодня читаем</div>
      <h1>${esc(r.title)}</h1>
      <div class="reference">${esc(r.reference)}</div>
      <button class="read-button" onclick="openReading()">Начать чтение <span>→</span></button>
    </section>
    <div class="grid">
      <section class="card">
        <h2>Вопрос дня</h2>
        <p class="question">${esc(r.question)}</p>
        <div class="action-row">
          <button class="primary-button" onclick="openReflection()">${status ? 'Моё размышление' : 'Поделиться мыслью'}</button>
          <button class="secondary-button" onclick="showOthers()">Что думают другие</button>
        </div>
        ${statusHtml ? `<div style="margin-top:12px">${statusHtml}</div>` : ''}
      </section>
      <section class="card">
        <h2>Твой день</h2>
        <p>${d.viewed_today ? '✓ Чтение уже открыто сегодня.' : 'Открой чтение, чтобы отметить сегодняшний день.'}</p>
      </section>
    </div>`;
}

function statusLabel(status) { return ({pending:'На модерации', published:'Опубликовано анонимно', rejected:'Отклонено'})[status] || status; }

function openReading() {
  const r = state.data.reading;
  api('/api/mark-read', { method: 'POST' }).then(() => { state.data.viewed_today = true; }).catch(() => {});
  haptic('light');
  const text = r.text ? `<div class="reading-text">${esc(r.text)}</div>` : `<p>Текст отрывка пока не добавлен в систему.</p><p><strong>${esc(r.reference)}</strong></p><p style="color:var(--muted)">Администратор может добавить текст отрывка через панель управления.</p>`;
  openModal(`<h2>${esc(r.title)}</h2><div class="status">${esc(r.reference)}</div><div class="card" style="margin-top:16px">${text}</div><div class="form-actions"><button class="primary-button" onclick="markAndClose()">✓ Прочитано</button></div>`);
}

function markAndClose() { state.data.viewed_today = true; closeModal(); render(); toast('День отмечен'); haptic('success'); }

function openReflection() {
  const current = state.data.my_reflection;
  if (current) {
    const action = current.status === 'published' ? `<button class="secondary-button" onclick="openEditRequest()">Попросить изменить</button>` : '';
    openModal(`<h2>Моё размышление</h2><div style="margin-bottom:12px">${statusBadge(current.status)}</div><div class="card"><p style="white-space:pre-wrap;color:var(--text);line-height:1.55">${esc(current.text)}</p></div><div class="form-actions">${action}<button class="ghost-button" onclick="closeModal()">Закрыть</button></div>`);
    return;
  }
  openModal(`<h2>Моё размышление</h2><p style="color:var(--muted);line-height:1.5">Напиши несколько предложений о сегодняшнем чтении. Публикация проходит модерацию и будет анонимной.</p><textarea id="reflection-text" maxlength="4000" placeholder="Что ты понял, заметил или о чём задумался?"></textarea><div class="form-actions"><button class="primary-button" onclick="sendReflection()">Отправить на модерацию</button></div>`);
}

function statusBadge(s) { return `<span class="status ${s==='published'?'success':s==='rejected'?'danger':''}">${statusLabel(s)}</span>`; }

async function sendReflection() {
  const text = $('#reflection-text')?.value?.trim();
  if (!text) return toast('Напиши хотя бы несколько слов');
  try {
    await api('/api/reflections', { method:'POST', body:{ text } });
    state.data.my_reflection = { text, status:'pending' };
    closeModal(); render(); toast('Отправлено на модерацию'); haptic('success');
  } catch (e) { toast(e.message); }
}

function openEditRequest() {
  const r = state.data.my_reflection;
  openModal(`<h2>Попросить изменить</h2><p style="color:var(--muted);line-height:1.5">Опиши администратору, что именно нужно изменить. Сам опубликованный текст останется без изменений до решения администратора.</p><textarea id="edit-text" maxlength="2000" placeholder="Например: исправить ошибку в одном предложении..."></textarea><div class="form-actions"><button class="primary-button" onclick="sendEditRequest()">Отправить просьбу</button></div>`);
}

async function sendEditRequest() {
  const text = $('#edit-text')?.value?.trim();
  const id = state.data.my_reflection?.id;
  if (!text || !id) return toast('Опиши просьбу');
  try { await api('/api/edit-request', { method:'POST', body:{ reflection_id:id, text } }); closeModal(); toast('Просьба отправлена администратору'); }
  catch(e) { toast(e.message); }
}

function showOthers() {
  const rows = state.data.others || [];
  const body = rows.length ? rows.map((x,i)=>`<div class="reflection"><span class="status">Размышление ${i+1}</span><p>${esc(x.text)}</p></div>`).join('') : '<div class="empty">Пока нет опубликованных размышлений.</div>';
  openModal(`<h2>Что думают другие</h2>${body}`);
}

function renderProgress() {
  const p = state.data.progress;
  content.innerHTML = `<div class="date">Твой путь</div><h1 class="section-title">Прогресс</h1><div class="stat-grid"><div class="stat"><strong>${p.days}</strong><span>дней чтения</span></div><div class="stat"><strong>${p.reflections}</strong><span>размышлений</span></div><div class="stat"><strong>${p.published}</strong><span>опубликовано</span></div></div><section class="card" style="margin-top:12px"><h2>${state.data.viewed_today ? 'Сегодня уже отмечено' : 'Сегодня ещё впереди'}</h2><p>${state.data.viewed_today ? 'Ты открыл сегодняшнее чтение. Продолжай завтра.' : 'Открой сегодняшнее чтение на главном экране, чтобы отметить день.'}</p></section>`;
}

function renderHistory() {
  const rows = state.data.history || [];
  const body = rows.length ? rows.map(x=>`<div class="timeline-item"><div class="timeline-dot"></div><div><h3>${esc(x.title)}</h3><p>${esc(x.description)}</p>${x.reference ? `<div class="status" style="margin-top:9px">${esc(x.reference)}</div>` : ''}</div></div>`).join('') : '<div class="empty">История пока наполняется.</div>';
  content.innerHTML = `<div class="date">Люди, события и места</div><h1 class="section-title">Библия — это история</h1><section class="card">${body}</section>`;
}

function renderMore() {
  content.innerHTML = `<div class="date">Настройки</div><h1 class="section-title">Ещё</h1><div class="grid"><section class="card"><h2>Библия на каждый день</h2><p>Ежедневное чтение, вопрос дня и анонимные размышления — в одном спокойном пространстве.</p></section><section class="card"><h2>Уведомления</h2><p>Бот присылает сообщение с сегодняшним чтением автоматически.</p></section><section class="card"><h2>Анонимность</h2><p>В опубликованных размышлениях не показываются имя, username или Telegram ID автора.</p></section></div>`;
}

function openModal(html) { modalBody.innerHTML = html; modal.classList.remove('hidden'); }
function closeModal() { modal.classList.add('hidden'); modalBody.innerHTML=''; }
$('#close-modal').onclick = closeModal;
$('.modal-backdrop').onclick = closeModal;
$('#refresh').onclick = () => { haptic('light'); load(); };

document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => { haptic('light'); state.page = btn.dataset.page; render(); }));

load();
