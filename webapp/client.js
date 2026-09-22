const tg = window.Telegram?.WebApp;
const state = { data: null, page: 'today', archive: null };

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor?.('secondary_bg_color');
  tg.enableClosingConfirmation?.();
}

const $ = (s) => document.querySelector(s);
const content = $('#content');
const modal = $('#modal');
const modalBody = $('#modal-body');
const offlineBanner = $('#offline-banner');

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

// Отрывки часто вставляют с номерами стихов в начале строки ("12 И сказал...").
// Оборачиваем номер в отдельный span, чтобы он визуально не сливался с текстом.
function formatPassage(text) {
  return esc(text)
    .split('\n')
    .map(line => line.replace(/^(\d{1,3})(\s+)/, '<span class="verse-number">$1</span>$2'))
    .join('\n');
}

function haptic(type = 'light') {
  try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {}
}

function notifyHaptic(kind) {
  try { tg?.HapticFeedback?.notificationOccurred(kind); } catch (_) {}
}

function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(window.__toast); window.__toast = setTimeout(() => el.classList.remove('show'), 2500);
}

function setButtonBusy(btn, busy, busyLabel) {
  if (!btn) return;
  if (busy) {
    btn.dataset.label = btn.dataset.label || btn.textContent;
    btn.textContent = busyLabel || 'Секунду…';
    btn.disabled = true;
  } else {
    btn.textContent = btn.dataset.label || btn.textContent;
    btn.disabled = false;
  }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (networkError) {
    offlineBanner.classList.remove('hidden');
    throw new Error('Нет соединения с сервером. Проверь интернет и попробуй ещё раз.');
  }
  offlineBanner.classList.add('hidden');
  let data = {};
  try { data = await response.json(); } catch (_) { /* пустой ответ */ }
  if (!response.ok) throw new Error(data.error || `Не удалось выполнить запрос (${response.status})`);
  return data;
}

async function load() {
  if (!tg || !tg.initData) {
    content.innerHTML = `<section class="card empty"><div style="font-size:42px">📖</div><h2>Открой приложение из Telegram</h2><p>Mini App работает внутри Telegram-клиента и использует его защищённую авторизацию. Открой бота в приложении Telegram и нажми «Открыть» ещё раз.</p></section>`;
    return;
  }
  content.innerHTML = `<div class="skeleton hero-skeleton"></div><div class="skeleton line"></div><div class="skeleton card-skeleton"></div>`;
  try {
    state.data = await api('/api/state');
    render();
  } catch (e) {
    content.innerHTML = `<section class="card empty"><div style="font-size:42px">⚠️</div><h2>Не удалось загрузить данные</h2><p>${esc(e.message)}</p><button class="primary-button" id="retry-load" style="margin-top:14px">Повторить</button></section>`;
    $('#retry-load')?.addEventListener('click', load);
  }
}

function render() {
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === state.page));
  if (state.page === 'today') renderToday();
  else if (state.page === 'progress') renderProgress();
  else if (state.page === 'history') renderHistory();
  else renderMore();
}

function dateLabel(iso) {
  const d = iso ? new Date(iso + 'T00:00:00') : new Date();
  return new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' }).format(d);
}

function weekDayLabel(iso) {
  return new Intl.DateTimeFormat('ru-RU', { weekday: 'short' }).format(new Date(iso + 'T00:00:00')).replace('.', '');
}

function weekStrip() {
  const week = state.data.progress.week || [];
  if (!week.length) return '';
  const cells = week.map(d => {
    const cls = ['week-day'];
    if (d.today) cls.push('is-today');
    if (d.read) cls.push('is-read');
    else if (d.has_reading) cls.push('is-missed');
    else cls.push('is-empty');
    return `<div class="${cls.join(' ')}" title="${esc(d.date)}"><span class="week-dow">${esc(weekDayLabel(d.date))}</span><span class="week-dot"></span></div>`;
  }).join('');
  return `<div class="week-strip">${cells}</div>`;
}

function renderToday() {
  const d = state.data;
  const r = d.reading;
  if (!r) {
    content.innerHTML = `<div class="date">${dateLabel()}</div>${weekStrip()}<section class="card empty"><div style="font-size:42px">📖</div><h2>Чтение ещё не задано</h2><p>Администратор пока не установил сегодняшнее чтение. Загляни чуть позже.</p></section>`;
    return;
  }
  const status = d.my_reflection?.status;
  const statusHtml = status ? `<span class="status ${status === 'published' ? 'success' : status === 'rejected' ? 'danger' : ''}">${statusLabel(status)}</span>` : '';
  const streak = d.progress?.streak || 0;
  const streakHtml = streak > 0 ? `<div class="streak-pill">🔥 ${streak} ${daysWord(streak)} подряд</div>` : '';
  content.innerHTML = `
    <div class="date">${dateLabel()}</div>
    ${weekStrip()}
    <section class="hero">
      <div class="hero-label">Сегодня читаем</div>
      <h1>${esc(r.title)}</h1>
      <div class="reference">${esc(r.reference)}</div>
      ${streakHtml}
      <button class="read-button" id="open-reading-btn">Начать чтение <span>→</span></button>
    </section>
    <div class="grid">
      <section class="card">
        <h2>Вопрос дня</h2>
        <p class="question">${esc(r.question)}</p>
        <div class="action-row">
          <button class="primary-button" id="open-reflection-btn">${status ? 'Моё размышление' : 'Поделиться мыслью'}</button>
          <button class="secondary-button" id="show-others-btn">Что думают другие</button>
        </div>
        ${statusHtml ? `<div style="margin-top:12px">${statusHtml}</div>` : ''}
      </section>
      <section class="card">
        <h2>Твой день</h2>
        <p>${d.viewed_today ? '✓ Чтение уже открыто сегодня.' : 'Открой чтение, чтобы отметить сегодняшний день.'}</p>
      </section>
    </div>`;
  $('#open-reading-btn').onclick = () => openReading(r, today());
  $('#open-reflection-btn').onclick = openReflection;
  $('#show-others-btn').onclick = showOthers;
}

function daysWord(n) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return 'день';
  if ([2, 3, 4].includes(mod10) && ![12, 13, 14].includes(mod100)) return 'дня';
  return 'дней';
}

function today() { return state.data.today; }

function statusLabel(status) { return ({pending:'На модерации', published:'Опубликовано анонимно', rejected:'Отклонено'})[status] || status; }

function openReading(reading, dateIso, alreadyViewed) {
  const isToday = dateIso === today();
  if (isToday && !alreadyViewed) {
    api('/api/mark-read', { method: 'POST', body: { date: dateIso } })
      .then((res) => { state.data.viewed_today = true; if (typeof res.streak === 'number') state.data.progress.streak = res.streak; })
      .catch(() => {});
  }
  haptic('light');
  const text = reading.text
    ? `<div class="reading-text">${formatPassage(reading.text)}</div>`
    : `<p>Текст отрывка пока не добавлен в систему.</p><p><strong>${esc(reading.reference)}</strong></p><p style="color:var(--muted)">Администратор может добавить текст отрывка через панель управления.</p>`;
  const doneButton = isToday ? `<button class="primary-button" id="mark-done-btn">✓ Прочитано</button>` : '';
  openModal(`<h2>${esc(reading.title)}</h2><div class="status">${esc(reading.reference)}</div><div class="card" style="margin-top:16px">${text}</div><div class="form-actions">${doneButton}</div>`);
  $('#mark-done-btn')?.addEventListener('click', markAndClose);
}

function markAndClose() { state.data.viewed_today = true; closeModal(); render(); toast('День отмечен'); notifyHaptic('success'); }

function openReflection() {
  const current = state.data.my_reflection;
  if (current) {
    let action = '';
    if (current.status === 'published') {
      action = current.edit_pending
        ? `<div class="status" style="margin-bottom:10px">Просьба об изменении уже отправлена</div>`
        : `<button class="secondary-button" id="edit-request-btn">Попросить изменить</button>`;
    }
    openModal(`<h2>Моё размышление</h2><div style="margin-bottom:12px">${statusBadge(current.status)}</div><div class="card"><p style="white-space:pre-wrap;color:var(--text);line-height:1.55">${esc(current.text)}</p></div><div class="form-actions">${action}<button class="ghost-button" id="close-reflection-btn">Закрыть</button></div>`);
    $('#edit-request-btn')?.addEventListener('click', openEditRequest);
    $('#close-reflection-btn')?.addEventListener('click', closeModal);
    return;
  }
  openModal(`<h2>Моё размышление</h2><p style="color:var(--muted);line-height:1.5">Напиши несколько предложений о сегодняшнем чтении. Публикация проходит модерацию и будет анонимной.</p><textarea id="reflection-text" maxlength="4000" placeholder="Что ты понял, заметил или о чём задумался?"></textarea><div class="form-actions"><button class="primary-button" id="send-reflection-btn">Отправить на модерацию</button></div>`);
  $('#send-reflection-btn').onclick = sendReflection;
}

function statusBadge(s) { return `<span class="status ${s==='published'?'success':s==='rejected'?'danger':''}">${statusLabel(s)}</span>`; }

async function sendReflection() {
  const btn = $('#send-reflection-btn');
  const text = $('#reflection-text')?.value?.trim();
  if (!text) return toast('Напиши хотя бы несколько слов');
  setButtonBusy(btn, true, 'Отправляем…');
  try {
    await api('/api/reflections', { method:'POST', body:{ text } });
    state.data.my_reflection = { text, status:'pending', edit_pending: false };
    closeModal(); render(); toast('Отправлено на модерацию'); notifyHaptic('success');
  } catch (e) { toast(e.message); setButtonBusy(btn, false); }
}

function openEditRequest() {
  openModal(`<h2>Попросить изменить</h2><p style="color:var(--muted);line-height:1.5">Опиши администратору, что именно нужно изменить. Опубликованный текст останется прежним до решения администратора.</p><textarea id="edit-text" maxlength="2000" placeholder="Например: исправить ошибку в одном предложении..."></textarea><div class="form-actions"><button class="primary-button" id="send-edit-btn">Отправить просьбу</button></div>`);
  $('#send-edit-btn').onclick = sendEditRequest;
}

async function sendEditRequest() {
  const btn = $('#send-edit-btn');
  const text = $('#edit-text')?.value?.trim();
  const id = state.data.my_reflection?.id;
  if (!text || !id) return toast('Опиши просьбу');
  setButtonBusy(btn, true, 'Отправляем…');
  try {
    await api('/api/edit-request', { method:'POST', body:{ reflection_id:id, text } });
    state.data.my_reflection.edit_pending = true;
    closeModal(); toast('Просьба отправлена администратору');
  } catch(e) { toast(e.message); setButtonBusy(btn, false); }
}

function showOthers() {
  const rows = state.data.others || [];
  const body = rows.length ? rows.map((x,i)=>`<div class="reflection"><span class="status">Размышление ${i+1}</span><p>${esc(x.text)}</p></div>`).join('') : '<div class="empty">Пока нет опубликованных размышлений.</div>';
  openModal(`<h2>Что думают другие</h2>${body}`);
}

function renderProgress() {
  const p = state.data.progress;
  content.innerHTML = `<div class="date">Твой путь</div><h1 class="section-title">Прогресс</h1>
    ${weekStrip()}
    <div class="stat-grid">
      <div class="stat"><strong>${p.days}</strong><span>дней чтения</span></div>
      <div class="stat"><strong>${p.streak}</strong><span>дней подряд</span></div>
      <div class="stat"><strong>${p.published}</strong><span>опубликовано</span></div>
    </div>
    <section class="card" style="margin-top:12px"><h2>Лучшая серия</h2><p>${p.best_streak} ${daysWord(p.best_streak)} подряд без пропуска. ${state.data.viewed_today ? 'Сегодня уже отмечено — продолжай завтра.' : 'Открой сегодняшнее чтение, чтобы не прерывать серию.'}</p></section>
    <section class="card" style="margin-top:12px"><h2>Архив чтений</h2><p>Все прошлые дни остаются доступны — можно вернуться и перечитать.</p><button class="secondary-button" id="open-archive-btn" style="margin-top:10px;width:100%">Открыть архив</button></section>`;
  $('#open-archive-btn').onclick = openArchive;
}

async function openArchive() {
  openModal(`<h2>Архив чтений</h2><div id="archive-body"><div class="skeleton line"></div><div class="skeleton line"></div></div>`);
  try {
    const res = state.archive || await api('/api/archive');
    state.archive = res;
    const items = res.items || [];
    const body = $('#archive-body');
    if (!items.length) {
      body.innerHTML = '<div class="empty">Прошлых дней пока нет — это только начало.</div>';
      return;
    }
    body.innerHTML = items.map(x => `
      <button class="archive-item" data-date="${esc(x.date)}">
        <div>
          <div class="archive-date">${esc(dateLabel(x.date))}</div>
          <div class="archive-title">${esc(x.title)}</div>
        </div>
        <span class="status ${x.viewed ? 'success' : ''}">${x.viewed ? '✓' : ''}</span>
      </button>`).join('');
    body.querySelectorAll('.archive-item').forEach(btn => btn.addEventListener('click', () => openArchiveDay(btn.dataset.date)));
  } catch (e) {
    $('#archive-body').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

async function openArchiveDay(dateIso) {
  try {
    const res = await api(`/api/reading?date=${encodeURIComponent(dateIso)}`);
    openReading(res.reading, dateIso, res.viewed);
  } catch (e) { toast(e.message); }
}

function renderHistory() {
  const rows = state.data.history || [];
  const body = rows.length ? rows.map(x=>`<div class="timeline-item"><div class="timeline-dot"></div><div><h3>${esc(x.title)}</h3><p>${esc(x.description)}</p>${x.reference ? `<div class="status" style="margin-top:9px">${esc(x.reference)}</div>` : ''}</div></div>`).join('') : '<div class="empty">История пока наполняется.</div>';
  content.innerHTML = `<div class="date">Люди, события и места</div><h1 class="section-title">Библия — это история</h1><section class="card">${body}</section>`;
}

function renderMore() {
  const notifications = state.data.notifications !== false;
  content.innerHTML = `<div class="date">Настройки</div><h1 class="section-title">Ещё</h1>
    <div class="grid">
      <section class="card">
        <h2>Уведомления</h2>
        <p>Бот присылает сообщение с сегодняшним чтением в ${esc(state.data.notify_time || '08:00')}.</p>
        <label class="switch-row">
          <span>Присылать уведомления</span>
          <span class="switch ${notifications ? 'on' : ''}" id="notify-switch" role="switch" aria-checked="${notifications}"></span>
        </label>
      </section>
      <section class="card"><h2>Анонимность</h2><p>В опубликованных размышлениях не показываются имя, username или Telegram ID автора.</p></section>
      <section class="card"><h2>Библия на каждый день</h2><p>Ежедневное чтение, вопрос дня и анонимные размышления — в одном спокойном пространстве.</p></section>
    </div>`;
  $('#notify-switch').onclick = toggleNotifications;
}

async function toggleNotifications() {
  const el = $('#notify-switch');
  const next = !el.classList.contains('on');
  el.classList.toggle('on', next);
  el.setAttribute('aria-checked', String(next));
  haptic('light');
  try {
    await api('/api/settings', { method: 'POST', body: { notifications: next } });
    state.data.notifications = next;
  } catch (e) {
    el.classList.toggle('on', !next);
    el.setAttribute('aria-checked', String(!next));
    toast(e.message);
  }
}

function openModal(html) { modalBody.innerHTML = html; modal.classList.remove('hidden'); }
function closeModal() { modal.classList.add('hidden'); modalBody.innerHTML=''; }
$('#close-modal').onclick = closeModal;
$('.modal-backdrop').onclick = closeModal;
$('#refresh').onclick = () => { haptic('light'); state.archive = null; load(); };

document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => { haptic('light'); state.page = btn.dataset.page; render(); }));

load();
