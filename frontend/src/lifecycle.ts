/** One bridge to app.js. No second context timer and no browser storage. */
declare global {
  interface Window {
    deanery: { marker: string; active: boolean; invalidated: boolean; busy: () => boolean;
      validate: (options?: {recover: boolean}) => Promise<boolean>; discard: (message: string) => void };
  }
}
let epoch = 0;
const pending = new Set<AbortController>();
const dirtyOwners = new Set<object>();
/** Fence non-fetch async work with the same owner epoch, including clipboard feedback. */
export function contextFence() {
  const generation=epoch, marker=window.deanery.marker;
  return () => generation===epoch && window.deanery.active && marker===window.deanery.marker;
}
export function setDirty(owner: object, dirty: boolean) { dirty ? dirtyOwners.add(owner) : dirtyOwners.delete(owner); }
export function csrf() { return document.querySelector<HTMLInputElement>('[name="csrfmiddlewaretoken"]')?.value || ''; }
export function body(fields: Record<string, string | number>) {
  return new URLSearchParams({...Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,String(v)])),
    context:window.deanery.marker, csrfmiddlewaretoken:csrf()});
}
export class RequestFailure extends Error { constructor(public status: number, message: string) { super(message); } }
export async function api<T>(url: string, options: RequestInit = {}, timeoutMs=15000): Promise<{data:T; status:number; ok:boolean}> {
  if (!window.deanery.active) throw new RequestFailure(409,'Доступ проверяется. Повторите после проверки сеанса.');
  const generation = epoch;
  const controller = new AbortController(); pending.add(controller);
  const timeout = setTimeout(()=>controller.abort(),timeoutMs);
  try {
    const response = await fetch(url, {...options, signal:controller.signal, credentials:'same-origin', cache:'no-store',
      headers:{'X-Component':'1','X-Page-Context':window.deanery.marker,...options.headers}});
    if (generation !== epoch || !window.deanery.active) throw new RequestFailure(409,'Ответ устарел.');
    if (!response.headers.get('Content-Type')?.includes('application/json')) {
      if (response.status === 403 || response.redirected) window.deanery.discard('Сеанс изменился. Откройте кабинет заново.');
      throw new RequestFailure(response.status,'Ответ сервера не подтверждён.');
    }
    const data = await response.json();
    if (generation !== epoch || !window.deanery.active) throw new RequestFailure(409,'Ответ устарел.');
    if (['stale_context','session_expired','login_required','invalid_scope','forbidden','not_found'].includes(data.state) || data.marker && data.marker !== window.deanery.marker) {
      window.deanery.discard('Сеанс или права изменились. Старый ввод очищен. Откройте актуальную страницу.');
      throw new RequestFailure(response.status,'Контекст изменился.');
    }
    if (response.status >= 500 || !data.marker) throw new RequestFailure(response.status,'Сервер не подтвердил результат.');
    return {data,status:response.status,ok:response.ok};
  } finally { pending.delete(controller); clearTimeout(timeout); }
}
export function installLifecycle(unmount:()=>void, mount:()=>void) {
  const abort = () => { epoch++; pending.forEach(c=>c.abort()); pending.clear(); };
  const discard = () => { abort(); dirtyOwners.clear(); unmount(); };
  document.addEventListener('private:conceal', abort);
  document.addEventListener('private:discard', discard);
  document.addEventListener('private:replace', discard);
  document.addEventListener('private:ready', mount);
  window.deanery.busy = () => pending.size > 0;
  addEventListener('beforeunload', event => {
    if (dirtyOwners.size && !window.deanery.invalidated) { event.preventDefault(); event.returnValue = ''; }
  });
  // Capture before app.js can conceal; logout/invalidation always bypass draft prompts.
  document.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const link = (event.target as Element).closest<HTMLAnchorElement>('a[href]');
    if(link && ['mailto:','tel:'].includes(link.protocol))return;
    if (!link || !dirtyOwners.size || link.hasAttribute('download') || link.target === '_blank' || link.hash && link.pathname === location.pathname) return;
    if (!confirm('Есть несохранённые изменения. Покинуть страницу и потерять этот ввод?')) {
      event.preventDefault(); event.stopPropagation();
    } else dirtyOwners.clear();
  },true);
  document.addEventListener('submit', event => {
    if ((event.target as Element).matches('[data-logout]')) dirtyOwners.clear();
  },true);
  if (window.deanery.active) mount();
}
