import {useEffect, useRef, useState} from 'react';
import type {ReactNode, RefObject} from 'react';
import {api, body} from './lifecycle';
import {useDismiss} from './useDismiss';

type Section={id:string;label:string};
type Help={id:string;question:string;answer:string;url:string;label:string;category:string};
type ProviderStatus={state:'disabled'|'checking'|'unavailable'|'model_missing'|'available'|'inference_failed';reason:string};
type SupportData={marker:string;version:string;topics:Help[];mode:string;provider:ProviderStatus;answer?:Help;clarify?:string};

function providerMessage(status:ProviderStatus|undefined,checking:boolean){
  if(checking||!status||status.state==='checking')return 'Проверяем подключение ИИ. Готовые вопросы уже доступны.';
  if(status.state==='available')return 'Локальная модель доступна для вопроса. Ответы — из проверенной справки.';
  const reasons:Record<string,string>={unconfigured:'Локальный ИИ отключён в настройках.',invalid_configuration:'Проверьте локальную настройку сервиса.',service_unavailable:'Локальный сервис недоступен.',model_missing:'Настроенная модель не найдена.',timeout:'Время ожидания локального сервиса истекло.',provider_error:'Локальный сервис временно не отвечает на запрос.',invalid_response:'Локальный сервис вернул неподходящий ответ.',invalid_intent:'Модель не смогла выбрать тему справки.'};
  return `ИИ не подключён. Доступны готовые вопросы и справка по кабинету. ${reasons[status.reason]||'Повторите проверку подключения.'}`;
}

type PanelProps={title:string;onClose:(restore?:boolean)=>void;children:ReactNode;launcher:RefObject<HTMLButtonElement|null>};
function Panel({title,onClose,children,launcher}:PanelProps){
  const ref=useRef<HTMLDivElement>(null);
  useDismiss(true,[ref,launcher],reason=>onClose(reason==='escape'));
  useEffect(()=>{
    const place=()=>{const panel=ref.current,button=launcher.current;if(!panel||!button)return;
      const vv=visualViewport,left=vv?.offsetLeft||0,top=vv?.offsetTop||0,width=vv?.width||innerWidth,height=vv?.height||innerHeight;
      const nav=document.querySelector('.bottom-nav')?.getBoundingClientRect();
      const reserve=nav&&nav.height&&nav.top<top+height&&nav.bottom>top?Math.min(nav.height,top+height-nav.top)+12:12;
      panel.style.maxHeight=`${Math.max(120,height-reserve-24)}px`;
      panel.style.width=`${Math.min(440,width-24)}px`;
      const b=button.getBoundingClientRect(),p=panel.getBoundingClientRect();
      panel.style.left=`${Math.max(left+12,Math.min(b.left-p.width-12,left+width-p.width-12))}px`;
      panel.style.top=`${Math.max(top+12,Math.min(b.bottom-p.height,top+height-reserve-p.height))}px`;
    };
    place();const observer=new ResizeObserver(place);if(ref.current)observer.observe(ref.current);
    addEventListener('resize',place);visualViewport?.addEventListener('resize',place);visualViewport?.addEventListener('scroll',place);
    return()=>{observer.disconnect();removeEventListener('resize',place);visualViewport?.removeEventListener('resize',place);visualViewport?.removeEventListener('scroll',place);};
  },[launcher]);
  return <div ref={ref} id="page-tool-panel" role="dialog" aria-modal="false" className="page-tool-panel" aria-label={title}><header><h2>{title}</h2><button type="button" className="button-quiet" onClick={()=>onClose(true)}>Закрыть</button></header><div className="page-tool-scroll">{children}</div></div>;
}

export function PageNavigator({sections,onClose,launcher}:{sections:Section[];onClose:(restore?:boolean)=>void;launcher:RefObject<HTMLButtonElement|null>}){
  function move(section:Section){onClose(false);requestAnimationFrame(()=>{if(!window.deanery.active)return;const element=document.getElementById(section.id);if(!element)return;const disclosure=element.closest('details');if(disclosure)disclosure.open=true;element.setAttribute('tabindex','-1');element.focus({preventScroll:true});element.scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});});}
  return <Panel title="Разделы страницы" onClose={onClose} launcher={launcher}><nav className="section-jump" aria-label="На этой странице">{sections.map(s=><button key={s.id} type="button" className="button-secondary" onClick={()=>move(s)}>{s.label}</button>)}</nav></Panel>;
}

function Assistant({onClose,launcher}:{onClose:(restore?:boolean)=>void;launcher:RefObject<HTMLButtonElement|null>}){
  const [data,setData]=useState<SupportData|null>(null),[question,setQuestion]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[last,setLast]=useState(''),[checking,setChecking]=useState(false),[statusError,setStatusError]=useState('');
  const serial=useRef(0),statusSerial=useRef(0),pending=useRef(false);
  const answer=useRef<HTMLElement>(null);
  useEffect(()=>{const node=answer.current,scroll=node?.closest('.page-tool-scroll');if(node&&scroll)scroll.scrollTop+=node.getBoundingClientRect().top-scroll.getBoundingClientRect().top;},[data?.answer]);
  async function check(refresh=false){const n=++statusSerial.current;setChecking(true);setStatusError('');try{const result=await api<SupportData>(`/support/?check=1${refresh?'&refresh=1':''}`);if(n===statusSerial.current&&result.ok)setData(old=>old?{...old,provider:result.data.provider}:result.data);}catch{if(n===statusSerial.current)setStatusError('Проверка подключения не завершена. Готовые вопросы доступны.');}finally{if(n===statusSerial.current)setChecking(false);}}
  async function load(){const n=++serial.current;setError('');try{const result=await api<SupportData>('/support/');if(n===serial.current&&result.ok){setData(result.data);if(result.data.provider.state==='checking')void check();}}catch{if(n===serial.current)setError('Не удалось открыть справку. Попробуйте ещё раз.');}}
  useEffect(()=>{void load();return()=>{serial.current++;statusSerial.current++;};},[]);
  async function ask(value:string,topic=''){
    if((pending.current&&!topic)||!value.trim())return;pending.current=true;const n=++serial.current;setBusy(true);setError('');setLast(value);setQuestion(value);
    try{const result=await api<SupportData>('/support/',{method:'POST',body:body({question:value,topic})});if(n!==serial.current)return;if(!result.ok)throw Error();
      if(!topic&&result.data.provider.state!=='checking'){statusSerial.current++;setChecking(false);setStatusError('');}
      setData(old=>({...result.data,provider:(topic||result.data.provider.state==='checking')&&old?old.provider:result.data.provider}));}
    catch{if(n===serial.current)setError('Ответ не получен. Повторите вопрос или откройте нужный раздел через меню.');}
    finally{if(n===serial.current){pending.current=false;setBusy(false);}}
  }
  return <Panel title="Помощник по кабинету" onClose={onClose} launcher={launcher}>
    <p className="support assistant-intro">Подскажу, где найти нужное и как пользоваться кабинетом. Действия выполняете вы.</p>
    <div className="assistant-mode"><p role="status">{statusError||providerMessage(data?.provider,checking)}</p>{data&&<button type="button" className="button-quiet" disabled={checking} onClick={()=>check(true)}>{checking?'Проверяем…':'Проверить подключение'}</button>}</div>
    <div className="help-suggestions" aria-label="Популярные вопросы">{data?.topics.slice(0,4).map(t=><button type="button" className="button-secondary" key={t.id} onClick={()=>ask(t.question,t.id)}>{t.question}</button>)}</div>
    {data&&data.topics.length>4&&<details><summary>Ещё вопросы</summary><div className="help-suggestions">{data.topics.slice(4).map(t=><button type="button" className="button-secondary" key={t.id} onClick={()=>ask(t.question,t.id)}>{t.question}</button>)}</div></details>}
    {data?.answer&&<section ref={answer} className="assistant-answer" aria-label="Ответ помощника" aria-live="polite"><p className="eyebrow">{data.answer.category}</p><p>{data.answer.answer}</p>{data.answer.url&&<a className="button-secondary" href={data.answer.url}>{data.answer.label}</a>}<p className="support">{data.mode==='model'?'Вопрос разобран локальной моделью · ответ из проверенной справки':'Ответ из готовой справки'}</p></section>}
    {data?.clarify&&<p role="status" className="assistant-answer">{data.clarify}</p>}
    {error&&<div className="field-error" role="alert"><p>{error}</p><button type="button" className="button-secondary" onClick={()=>last?ask(last):load()}>Повторить</button></div>}
    <form className="assistant-form" onSubmit={e=>{e.preventDefault();void ask(question);}}><label htmlFor="support-question">Ваш вопрос</label><textarea id="support-question" value={question} onChange={e=>setQuestion(e.target.value)} maxLength={800} rows={2} placeholder="Например: где мои зачёты?"/><p className="support">Не вводите пароли и личные данные.</p><button type="submit" disabled={busy||!question.trim()}>{busy?'Ищем ответ…':'Спросить'}</button></form>
  </Panel>;
}

export function PageTools(){
  const [sections,setSections]=useState<Section[]>([]),[visible,setVisible]=useState(false),[panel,setPanel]=useState<'navigation'|'assistant'|null>(null);
  const navButton=useRef<HTMLButtonElement>(null),helpButton=useRef<HTMLButtonElement>(null);
  useEffect(()=>{const main=document.getElementById('main');if(!main)return;let frame=0;
    const update=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{const rows=Array.from(main.querySelectorAll<HTMLElement>('[data-page-section][id]')).filter(e=>e.getClientRects().length).map(e=>({id:e.id,label:e.dataset.pageSection!}));setSections(old=>JSON.stringify(old)===JSON.stringify(rows)?old:rows);setVisible(scrollY>180&&document.documentElement.scrollHeight>innerHeight+300);});};
    const observer=new MutationObserver(update);observer.observe(main,{childList:true,subtree:true});const resize=new ResizeObserver(update);resize.observe(main);addEventListener('scroll',update,{passive:true});addEventListener('resize',update);update();
    return()=>{observer.disconnect();resize.disconnect();cancelAnimationFrame(frame);removeEventListener('scroll',update);removeEventListener('resize',update);};
  },[]);
  function close(restore=false){setPanel(null);if(restore)requestAnimationFrame(()=>{if(window.deanery.active)(panel==='navigation'?navButton:helpButton).current?.focus({preventScroll:true});});}
  return <><div className="page-tools-dock">
    {(visible||panel==='navigation')&&sections.length>1&&<button ref={navButton} type="button" className="floating-control" onClick={()=>setPanel(panel==='navigation'?null:'navigation')} aria-label="Разделы страницы" title="Разделы страницы" aria-expanded={panel==='navigation'} aria-controls={panel==='navigation'?'page-tool-panel':undefined}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1"/></svg><span>Разделы страницы</span></button>}
    <button ref={helpButton} type="button" className="floating-control assistant-control" onClick={()=>setPanel(panel==='assistant'?null:'assistant')} aria-label="Помощник" title="Помощник" aria-expanded={panel==='assistant'} aria-controls={panel==='assistant'?'page-tool-panel':undefined}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v12H9l-5 4V4Z"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 1-3 3m0 2v1"/></svg><span>Помощник</span></button>
  </div>{panel==='navigation'&&<PageNavigator sections={sections} onClose={close} launcher={navButton}/>} {panel==='assistant'&&<Assistant onClose={close} launcher={helpButton}/>}</>;
}

type ViewedRequest={marker:string;token:string;version:number;id:number};
export function RequestView(){
  const initial=useRef<ViewedRequest>(JSON.parse(document.getElementById('viewed-request')?.textContent||'{}'));
  if(!initial.current.id){const el=document.querySelector<HTMLElement>('[data-component="request-view"]');initial.current={marker:window.deanery.marker,token:'',id:Number(el?.dataset.requestId),version:Number(el?.dataset.requestVersion)};}
  const [error,setError]=useState(''),[status,setStatus]=useState(''),[busy,setBusy]=useState(false);
  const alive=useRef(true),away=useRef(false),pending=useRef(false),refreshing=useRef(false),acknowledged=useRef(false);
  async function acknowledge(){if(!initial.current.token||pending.current||acknowledged.current||!window.deanery.active||document.hidden)return;pending.current=true;setError('');try{const result=await api<{unread:number}>('/notifications/viewed-request/',{method:'POST',body:body({snapshot:initial.current.token})});if(!result.ok)throw Error();if(alive.current){acknowledged.current=true;document.dispatchEvent(new CustomEvent('private:unread',{detail:result.data.unread}));}}catch{if(alive.current&&window.deanery.active)setError('Прочтение уведомления не подтверждено.');}finally{pending.current=false;}}
  async function refresh(){if(refreshing.current)return;refreshing.current=true;setBusy(true);try{
    const result=await api<{version:number;html:string;viewed:ViewedRequest|null;clarification:unknown;clarification_viewed:unknown}>(`/requests/${initial.current.id}/state/`);
    if(!result.ok)throw Error();if(!alive.current)return;
    document.dispatchEvent(new CustomEvent('private:clarification',{detail:{exchange:result.data.clarification,viewed:result.data.clarification_viewed}}));
    if(result.data.version!==initial.current.version){
      const parsed=new DOMParser().parseFromString(result.data.html,'text/html');
      if(parsed.body.dataset.context!==window.deanery.marker)throw Error();
      const dirty=Array.from(document.querySelectorAll<HTMLTextAreaElement>('.decision-form textarea')).some(el=>Boolean(el.value.trim())||el.value!==el.defaultValue);
      // Preserve the visible reading anchor only when real content above it changes.
      const anchor=Array.from(document.querySelectorAll<HTMLElement>('#request-payload p,#request-history li,.decision-form textarea')).find(el=>{const r=el.getBoundingClientRect();return r.top>=60&&r.top<innerHeight-60;});
      const before=anchor?.getBoundingClientRect().top;
      const anchorId=anchor?.id;
      for(const selector of ['#request-current','#request-history']){
        const old=document.querySelector(selector),next=parsed.querySelector(selector);
        if(old&&next)old.innerHTML=next.innerHTML;
      }
      const decisions=document.querySelector('.request-decisions'),next=parsed.querySelector('.request-decisions');
      if(dirty&&decisions){
        for(const button of decisions.querySelectorAll<HTMLButtonElement>('button[type="submit"]'))button.disabled=true;
        setStatus('Статус изменился. Ваше несохранённое решение оставлено с прежней версией. Проверьте новый статус перед повторным открытием действий.');
      }else{if(decisions)next?decisions.replaceWith(document.adoptNode(next)):decisions.remove();else if(next)document.querySelector('#request-history')?.before(document.adoptNode(next));setStatus('Статус и история обновлены.');}
      const restoredAnchor=anchor?.isConnected?anchor:anchorId?document.getElementById(anchorId):null;
      if(restoredAnchor&&before!==undefined){const delta=restoredAnchor.getBoundingClientRect().top-before;if(Math.abs(delta)>1)scrollBy(0,delta);}
      initial.current={...initial.current,...(result.data.viewed||{}),version:result.data.version};acknowledged.current=false;void acknowledge();
    }else setStatus('Статус актуален.');
  }catch{setStatus('Не удалось проверить статус. Повторите обновление.');}finally{refreshing.current=false;if(alive.current)setBusy(false);}}
  useEffect(()=>{alive.current=true;const target=document.getElementById('request-current');const visible=()=>{const r=target?.getBoundingClientRect();return Boolean(r&&r.top<innerHeight-80&&r.bottom>80);};const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)&&visible())void acknowledge();},{threshold:.2});if(target)observer.observe(target);const leave=()=>{away.current=true;};const visibility=()=>{if(document.hidden)leave();};const ready=()=>{if(visible())void acknowledge();if(away.current&&!document.hidden&&document.hasFocus()){away.current=false;void refresh();}};addEventListener('blur',leave);document.addEventListener('visibilitychange',visibility);document.addEventListener('private:ready',ready);return()=>{alive.current=false;observer.disconnect();removeEventListener('blur',leave);document.removeEventListener('visibilitychange',visibility);document.removeEventListener('private:ready',ready);};},[]);
  return <div className="request-view-tools"><button type="button" className="button-secondary" disabled={busy} onClick={()=>refresh()}>{busy?'Проверяем…':'Обновить статус'}</button>{status&&<p role="status">{status}</p>}{error&&<div role="status"><span>{error}</span> <button type="button" className="button-quiet" onClick={()=>acknowledge()}>Повторить отметку</button></div>}</div>;
}
