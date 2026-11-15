/* Личные данные и черновики — только в текущем документе, без browser storage. */
(() => {
  'use strict';
  const toggle = document.querySelector('#password-toggle');
  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener('click', () => {
      const field = document.querySelector('#id_password');
      const visible = field.type === 'password';
      field.type = visible ? 'text' : 'password';
      toggle.setAttribute('aria-pressed', String(visible));
      toggle.textContent = visible ? 'Скрыть' : 'Показать';
    });
  }
  let channel;
  try { channel = new BroadcastChannel('deanery-invalidation'); } catch (_) { /* focus/visibility/poll remain */ }
  if (!document.body.dataset.private) {
    addEventListener('pagehide', () => {
      for (const field of document.querySelectorAll('.login-form input:not([type="hidden"])')) field.value = '';
      const password = document.querySelector('#id_password');
      if (password) password.type = 'password';
      if (toggle) { toggle.textContent = 'Показать'; toggle.setAttribute('aria-pressed','false'); }
    });
    addEventListener('pageshow', event => { if (event.persisted) location.replace('/login/'); });
    return;
  }
  const content = document.querySelector('#private-content');
  const bottomNav = document.querySelector('.bottom-nav');
  if (bottomNav && typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => {
      const height = bottomNav.getBoundingClientRect().height;
      if (height > 0) document.documentElement.style.setProperty('--bottom-nav-height', `${Math.ceil(height)}px`);
    }).observe(bottomNav);
  }
  const mask = document.querySelector('#privacy-state');
  const message = document.querySelector('#privacy-message');
  const marker = document.body.dataset.context;
  const expiry = Date.parse(document.body.dataset.expires);
  const requestPage = Boolean(document.querySelector('[data-request-form], .request-list, .request-current'));
  const freshPath = requestPage ? (document.body.dataset.role === 'staff' ? '/staff/' : '/requests/') : '/';
  const freshLink = document.querySelector('#privacy-fresh');
  freshLink.href = freshPath;
  if (requestPage) freshLink.textContent = 'Проверить сохранённые заявки';
  let generation = 0, controller, expiryTimer, invalidated = false, pending = null, recoveryForm = null, recoveryReason = 'uncertain';
  let verified = false, concealed = true, checking = null, announced = false;
  let schedulePresented = false;
  const signal = name => document.dispatchEvent(new CustomEvent(`private:${name}`));
  // The single context owner exposes a bridge; components do not poll context.
  window.deanery = {marker, validate: (...args) => validate(...args), discard: text => discard(text),
    get active() { return verified && !invalidated && !concealed && Date.now() < expiry; },
    get invalidated() { return invalidated; }, busy: () => false};
  // The profile stays inside the private lifecycle subtree. Native top layer
  // escapes sidebar clipping without moving account data into a global portal.
  const profileTrigger = content.querySelector('.profile-trigger');
  const profilePanel = content.querySelector('#profile-panel');
  if (profileTrigger && profilePanel) {
    const native = typeof profilePanel.showPopover === 'function';
    if (!native) { profilePanel.hidden = true; content.append(profilePanel); }
    let frame = 0;
    const opened = () => native ? profilePanel.matches(':popover-open') : !profilePanel.hidden;
    function closeProfile(restore = false) {
      if (opened()) { if (native) profilePanel.hidePopover(); else profilePanel.hidden = true; }
      profileTrigger.setAttribute('aria-expanded', 'false');
      if (restore && window.deanery.active) profileTrigger.focus({preventScroll:true});
    }
    function placeProfile() {
      frame = 0;
      if (!opened() || !profileTrigger.isConnected) return;
      const r = profileTrigger.getBoundingClientRect(), viewport = window.visualViewport;
      const x = viewport?.offsetLeft || 0, y = viewport?.offsetTop || 0;
      const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
      const edge = 12, gap = 8, low = y + edge, high = y + height - edge;
      if (r.bottom <= low || r.top >= high) { closeProfile(); return; }
      profilePanel.style.maxWidth = `${Math.max(0, width - edge * 2)}px`;
      const above = Math.max(0, r.top - gap - low), below = Math.max(0, high - r.bottom - gap);
      const naturalHeight = profilePanel.scrollHeight + 2;
      const desktop = matchMedia('(min-width:761px)').matches;
      const up = desktop ? above >= naturalHeight || above >= below : !(below >= naturalHeight || below >= above);
      profilePanel.style.maxHeight = `${up ? above : below}px`;
      const box = profilePanel.getBoundingClientRect();
      profilePanel.style.left = `${Math.max(x + edge, Math.min(r.left, x + width - edge - box.width))}px`;
      profilePanel.style.top = `${Math.max(low, Math.min(up ? r.top - gap - box.height : r.bottom + gap, high - box.height))}px`;
    }
    function scheduleProfile() { if (opened() && !frame) frame = requestAnimationFrame(placeProfile); }
    profileTrigger.addEventListener('click', event => {
      event.preventDefault();
      if (!window.deanery.active) return;
      if (opened()) { closeProfile(); return; }
      if (native) profilePanel.showPopover({source:profileTrigger}); else profilePanel.hidden = false;
      profileTrigger.setAttribute('aria-expanded','true');
      placeProfile();
      if (event.detail === 0) profilePanel.querySelector('a')?.focus({preventScroll:true});
    });
    profilePanel.addEventListener('toggle', () => profileTrigger.setAttribute('aria-expanded', String(opened())));
    const outside = event => { if (opened() && !profilePanel.contains(event.target) && !profileTrigger.contains(event.target)) closeProfile(); };
    const escape = event => { if (opened() && event.key === 'Escape') { event.preventDefault(); closeProfile(true); } };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('focusin', outside);
    document.addEventListener('keydown', escape);
    addEventListener('scroll', scheduleProfile, true);
    addEventListener('resize', scheduleProfile);
    visualViewport?.addEventListener('resize', scheduleProfile);
    visualViewport?.addEventListener('scroll', scheduleProfile);
    document.addEventListener('presentation:change', scheduleProfile);
    document.addEventListener('private:conceal', () => closeProfile());
    const observer = new ResizeObserver(scheduleProfile);
    observer.observe(profileTrigger); observer.observe(profilePanel);
    document.addEventListener('private:discard', () => {
      closeProfile(); cancelAnimationFrame(frame); observer.disconnect();
      document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', outside);
      document.removeEventListener('keydown', escape);
      removeEventListener('scroll', scheduleProfile, true); removeEventListener('resize', scheduleProfile);
      visualViewport?.removeEventListener('resize', scheduleProfile); visualViewport?.removeEventListener('scroll', scheduleProfile);
      document.removeEventListener('presentation:change', scheduleProfile);
    }, {once:true});
  }
  const focusedErrors = new WeakSet();
  function closePrivateLayers() {
    for (const menu of content.querySelectorAll('.profile-menu[open]')) menu.open = false;
    for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
    for (const popover of document.querySelectorAll('[popover]')) {
      try { if (popover.matches(':popover-open')) popover.hidePopover(); } catch (_) { /* unsupported native popover */ }
    }
  }
  function clearDraftFields() {
    for (const field of content.querySelectorAll('[data-sensitive-draft], input[name="submission_token"]')) {
      if (field.matches('input[type="checkbox"], input[type="radio"]')) {
        field.checked = false; field.defaultChecked = false;
      } else if (field.matches('select')) {
        for (const option of field.options) { option.selected = false; option.defaultSelected = false; }
        field.selectedIndex = -1;
      } else { field.value = ''; field.defaultValue = ''; }
    }
  }
  function presentSchedule() {
    if (content.querySelector('[data-component="schedule"]')) return;
    if (schedulePresented) return;
    const strip = content.querySelector('[data-month-strip]');
    if (!strip) return;
    schedulePresented = true;
    const selected = strip.querySelector('[aria-current="date"]');
    if (selected) {
      const item = selected.getBoundingClientRect(), rail = strip.getBoundingClientRect();
      strip.scrollLeft += item.left - rail.left - (rail.width - item.width) / 2;
    }
    // Один акцент после подтверждения сеанса; повторные focus/poll не перезапускают его.
    if (new URL(location.href).searchParams.has('date')) content.querySelector('[data-selected-day]')?.classList.add('date-emphasis');
  }
  function enable(form) {
    if (!form?.isConnected) return;
    form.removeAttribute('aria-busy');
    for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = false;
  }
  function protection(state, text) {
    mask.dataset.state = state;
    document.querySelector('#privacy-title').textContent = state === 'pending' ? 'Проверяем сеанс' : state === 'retry' ? 'Доступ пока не подтверждён' : 'Сеанс изменился';
    document.querySelector('#privacy-retry').hidden = state !== 'retry';
    freshLink.hidden = state !== 'invalid';
    if (text) message.textContent = text;
    mask.hidden = false;
  }
  function conceal(text, state = 'pending') {
    generation += 1;
    controller?.abort();
    if (pending) { pending.controller.abort(); enable(pending.form); pending = null; }
    concealed = true;
    content.classList.add('private-concealed'); content.inert = true; content.setAttribute('aria-hidden', 'true');
    signal('conceal');
    for (const day of content.querySelectorAll('.date-emphasis')) day.classList.remove('date-emphasis');
    closePrivateLayers();
    protection(state, text || 'Подтверждаем доступ к этой странице.');
  }
  function discard(text) {
    invalidated = true;
    signal('discard');
    clearDraftFields();
    if (pending) for (const key of [...pending.payload.keys()]) pending.payload.delete(key);
    for (const image of content.querySelectorAll('img')) { image.removeAttribute('src'); image.removeAttribute('srcset'); }
    conceal(text, 'invalid');
    recoveryForm = null;
    // Удаляются также значения textarea, ошибки, раскрытая история, badges и имена.
    content.replaceChildren();
  }
  function expired() { discard('Срок доступа к этой странице завершён. Откройте кабинет заново; при необходимости войдите. Старый ввод очищен.'); }
  function focusErrors() {
    const error = content.querySelector('[data-focus-errors]');
    if (error && !focusedErrors.has(error)) { focusedErrors.add(error); error.focus(); }
  }
  function showRecovery() {
    if (!recoveryForm?.isConnected || concealed || pending) return;
    if (content.querySelector('.save-recovery')) return;
    const box = document.createElement('section');
    box.className = 'save-recovery'; box.tabIndex = -1; box.setAttribute('aria-labelledby', 'save-recovery-title');
    const heading = document.createElement('h2'); heading.id = 'save-recovery-title';
    heading.textContent = recoveryReason === 'unavailable' ? 'Сохранение временно недоступно' : 'Сохранение пока не подтверждено';
    const copy = document.createElement('p');
    copy.textContent = recoveryForm.dataset.requestForm === 'create'
      ? 'Ответ не получен, но заявка могла сохраниться. Проверьте список. Повтор этой отправки использует тот же ключ и не создаст дубликат.'
      : 'Ответ не получен, но решение могло сохраниться. Проверьте актуальную заявку. Повтор прежнего действия может сообщить о конфликте версий.';
    if (recoveryReason === 'unavailable') copy.textContent = 'Сервер не подтвердил результат сохранения. Проверьте сохранённые заявки перед повторной отправкой. Введённый текст оставлен в этой форме.';
    const actions = document.createElement('div'); actions.className = 'actions';
    const link = document.createElement('a'); link.className = 'text-link';
    link.href = recoveryForm.dataset.requestForm === 'create' ? '/requests/' : recoveryForm.getAttribute('action');
    link.textContent = recoveryForm.dataset.requestForm === 'create' ? 'Проверить мои заявки' : 'Проверить актуальную заявку';
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'button-quiet';
    retry.textContent = 'Повторить эту отправку';
    retry.addEventListener('click', () => recoveryForm?.requestSubmit());
    actions.append(link, retry); box.append(heading, copy, actions);
    recoveryForm.before(box); box.focus();
  }
  function validate({recover = true} = {}) {
    if (invalidated || document.hidden) return Promise.resolve(false);
    if (Date.now() >= expiry) { expired(); return Promise.resolve(false); }
    if (checking && !controller?.signal.aborted) return checking;
    if (!verified || concealed) protection('pending', 'Подтверждаем доступ к этой странице.');
    const requestGeneration = generation;
    controller = new AbortController();
    const checkController = controller;
    const timeout = setTimeout(() => checkController.abort(), 7000);
    const run = async () => {
    try {
      const response = await fetch('/context/', {cache:'no-store', credentials:'same-origin',
        headers:{'X-Page-Context':marker}, signal:checkController.signal});
      const data = await response.json();
      if (requestGeneration !== generation || document.visibilityState !== 'visible') return false;
      if (!response.ok || data.marker !== marker || Date.parse(data.expires_at) !== expiry || Date.now() >= expiry) {
        if (response.status === 503) conceal('Проверка временно недоступна. Попробуйте снова.', 'retry');
        else discard('Сеанс или доступ изменился. Старый ввод очищен. Откройте актуальную страницу и проверьте сохранённые заявки.');
        return false;
      }
      verified = true; concealed = false;
      mask.hidden = true; content.classList.remove('private-concealed'); content.inert = false; content.removeAttribute('aria-hidden');
      signal('ready');
      if (!announced) { announced = true; channel?.postMessage({type:'context', marker}); }
      presentSchedule();
      clearTimeout(expiryTimer); expiryTimer = setTimeout(expired, Math.max(0, expiry-Date.now()));
      if (recover) showRecovery();
      focusErrors();
      return true;
    } catch (_) {
      if (requestGeneration === generation) conceal('Не удалось проверить доступ. Проверьте соединение и повторите проверку.', 'retry');
      return false;
    } finally { clearTimeout(timeout); if (controller === checkController) checking = null; }
    };
    checking = run();
    return checking;
  }
  async function sendForm(form) {
    if (pending || invalidated) return;
    if (!await validate({recover:false}) || !form.isConnected) return;
    content.querySelector('.save-recovery')?.remove();
    const payload = new FormData(form);
    const requestGeneration = generation;
    const writeController = new AbortController();
    const operation = {controller: writeController, form, payload};
    pending = operation; recoveryForm = form; recoveryReason = 'uncertain';
    form.setAttribute('aria-busy', 'true');
    for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = true;
    const timeout = setTimeout(() => writeController.abort(), 15000);
    try {
      // Поле workflow с name=action затеняет DOM form.action.
      const response = await fetch(form.getAttribute('action'), {method:'POST', body:payload, credentials:'same-origin', cache:'no-store',
        headers:{'X-Request-Form':'1'}, signal:writeController.signal});
      const json = response.headers.get('Content-Type')?.includes('application/json');
      const result = json ? await response.json() : await response.text();
      if (requestGeneration !== generation || invalidated || document.hidden) return;
      pending = null; enable(form);
      if (json && ['stale_context','session_expired','login_required','invalid_scope','forbidden','not_found'].includes(result.state)) {
        if (response.status === 401) { expired(); return; }
        discard('Сеанс или доступ изменился. Старый ввод очищен. Проверьте сохранённые заявки на актуальной странице.'); return;
      }
      if (!await validate({recover:false})) return;
      if (json && response.ok && result.state === 'saved' && result.marker === marker) {
        recoveryForm = null; conceal('Сохранено. Открываем заявку.'); location.assign(result.redirect); return;
      }
      if (!json && [400,409].includes(response.status)) {
        const parsed = new DOMParser().parseFromString(result, 'text/html');
        const main = parsed.querySelector('#main');
        if (parsed.body.dataset.context !== marker || response.headers.get('X-Page-Context') !== marker || !main) {
          discard('Контекст формы изменился. Откройте актуальный список заявок.'); return;
        }
        recoveryForm = null;
        signal('replace');
        content.querySelector('#main').replaceWith(document.adoptNode(main)); signal('ready'); focusErrors(); return;
      }
      if (response.status === 403) { discard('Не удалось проверить форму. Откройте актуальный список заявок перед новой отправкой.'); return; }
      if (response.status === 503) recoveryReason = 'unavailable';
      showRecovery();
    } catch (_) {
      if (requestGeneration === generation && !invalidated) { pending = null; enable(form); await validate(); }
    } finally {
      clearTimeout(timeout);
      if (pending === operation) pending = null;
      enable(form);
    }
  }
  addEventListener('pagehide', () => {
    conceal();
  });
  addEventListener('pageshow', () => validate());
  addEventListener('focus', validate);
  document.addEventListener('visibilitychange', () => document.hidden ? conceal() : validate());
  channel?.addEventListener('message', event => {
    if (event.data?.type === 'logout') discard('В другой вкладке выполнен выход. Старый ввод очищен. Откройте кабинет заново.');
    else if (event.data?.type === 'context' && event.data.marker !== marker) {
      // A peer is a hint, not authority: an old tab cannot adopt a new context.
      conceal(); void validate();
    }
  });
  document.addEventListener('private:unread', event => {
    if (invalidated || concealed) return;
    for (const badge of content.querySelectorAll('.count, .mobile-count')) {
      badge.textContent = String(event.detail); badge.hidden = !event.detail;
      badge.setAttribute('aria-label', `непрочитанных: ${event.detail}`);
    }
  });
  document.querySelector('#privacy-retry').addEventListener('click', () => invalidated ? location.assign(freshPath) : validate());
  document.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    if (concealed || Date.now() >= expiry || invalidated) { event.preventDefault(); if (Date.now() >= expiry) expired(); return; }
    if (event.target.matches('[data-request-form]')) { event.preventDefault(); sendForm(event.target); return; }
    if (event.target.matches('[data-logout]')) {
      signal('discard');
      channel?.postMessage({type:'logout'});
      clearDraftFields();
    }
    conceal('Действие отправлено. Ожидаем ответ сервера.');
  });
  document.addEventListener('click', event => {
    if (event.defaultPrevented) return;
    const link = event.target.closest('a[href]');
    if (!link || link.target === '_blank' || link.hasAttribute('download') || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const target = new URL(link.href, location.href);
    // A native composer/dialer intent does not navigate or invalidate this document.
    if (target.protocol === 'mailto:' || target.protocol === 'tel:') return;
    if (target.origin === location.origin && target.pathname === location.pathname && target.search === location.search && target.hash) {
      if (link.closest('.error-summary')) { event.preventDefault(); document.getElementById(target.hash.slice(1))?.focus(); }
      return;
    }
    conceal();
  });
  // Единственный прежний owner проверки контекста. Автоповторов записи нет.
  setInterval(() => { if (Date.now() >= expiry) expired(); else if (!document.hidden) validate(); }, 30000);
  validate();
})();
