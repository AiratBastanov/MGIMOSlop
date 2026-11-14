/* Only an explicit browser-level visual preference. Runs before styles/paint.
   No account state, network, private DOM copies or React lifecycle ownership. */
(() => {
  'use strict';
  const key = 'deanery.presentation.v1';
  const defaults = () => ({v:1,enabled:false,size:125,palette:'light',spacing:'normal'});
  const valid = value => value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).sort().join(',') === 'enabled,palette,size,spacing,v' &&
    value.v === 1 && typeof value.enabled === 'boolean' && [100,125,150,200].includes(value.size) &&
    ['light','dark'].includes(value.palette) && ['normal','increased'].includes(value.spacing);
  let preference = defaults(), persistent = true;
  try {
    const raw = localStorage.getItem(key);
    if (raw && raw.length < 180) { const parsed = JSON.parse(raw); if (valid(parsed)) preference = parsed; }
  } catch (_) { persistent = false; }
  const root = document.documentElement;
  function apply() {
    if (preference.enabled) {
      root.dataset.reading = preference.palette;
      root.dataset.readingSize = String(preference.size);
      root.dataset.readingSpacing = preference.spacing;
    } else {
      delete root.dataset.reading; delete root.dataset.readingSize; delete root.dataset.readingSpacing;
    }
  }
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    const trigger = document.getElementById('reading-toggle'), panel = document.getElementById('reading-settings');
    if (!trigger || !panel) return;
    const size = document.getElementById('reading-size'), palette = document.getElementById('reading-palette'), spacing = document.getElementById('reading-spacing');
    const native = typeof panel.showPopover === 'function';
    if (!native) panel.hidden = true;
    const opened = () => native ? panel.matches(':popover-open') : !panel.hidden;
    function sync() {
      size.value=String(preference.size); palette.value=preference.palette; spacing.value=preference.spacing;
      trigger.textContent=preference.enabled?'Настройки вида':'Для слабовидящих';
      trigger.setAttribute('aria-expanded',String(opened()));
      document.getElementById('reading-storage').textContent=persistent?
        'Настройка для этого браузера. Личные данные не сохраняются.':
        'Настройка действует в этом окне. Браузер не разрешил её сохранить.';
    }
    function close(restore=false) {
      if (opened()) { if (native) panel.hidePopover(); else panel.hidden=true; }
      trigger.setAttribute('aria-expanded','false');
      if (restore) trigger.focus({preventScroll:true});
    }
    function change(next) {
      // Preserve the actual visible content anchor through reflow, never a clone.
      const focused=document.activeElement;
      const anchor=focused instanceof HTMLElement && focused.closest('#main') ? focused :
        [...document.querySelectorAll('#main h1,#main h2,#main input,#main textarea,#main article,#main section')]
          .find(node => {const r=node.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight/2;});
      const before=anchor?.getBoundingClientRect().top;
      preference=next; apply();
      try { if (preference.enabled) localStorage.setItem(key,JSON.stringify(preference)); else localStorage.removeItem(key); persistent=true; }
      catch (_) { persistent=false; }
      sync();
      requestAnimationFrame(()=>{
        if(anchor?.isConnected&&before!==undefined&&scrollY>0){
          const delta=anchor.getBoundingClientRect().top-before;
          if(Math.abs(delta)>1)scrollBy({top:delta,behavior:'instant'});
        }
        document.dispatchEvent(new Event('presentation:change'));
      });
    }
    trigger.addEventListener('click',()=>{
      if(!preference.enabled)change({...defaults(),enabled:true});
      if(opened())close();else {if(native)panel.showPopover();else panel.hidden=false;sync();}
    });
    panel.addEventListener('toggle',sync);
    document.getElementById('reading-close').addEventListener('click',()=>close(true));
    for(const button of document.querySelectorAll('[data-reading-reset]'))button.addEventListener('click',()=>{change(defaults());close(true);});
    size.addEventListener('change',()=>change({...preference,size:Number(size.value)}));
    palette.addEventListener('change',()=>change({...preference,palette:palette.value}));
    spacing.addEventListener('change',()=>change({...preference,spacing:spacing.value}));
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&opened()){event.preventDefault();close(true);}});
    document.addEventListener('pointerdown',event=>{if(opened()&&!panel.contains(event.target)&&!trigger.contains(event.target))close(panel.contains(document.activeElement));});
    // A non-modal panel may not conceal a newly focused page control.
    document.addEventListener('focusin',event=>{if(opened()&&!panel.contains(event.target)&&event.target!==trigger)close();});
    const header=document.querySelector('.site-header');
    if(header&&typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{
      root.style.setProperty('--shell-header-height',`${Math.ceil(header.getBoundingClientRect().height)}px`);
    }).observe(header);
    sync();
  });
})();
