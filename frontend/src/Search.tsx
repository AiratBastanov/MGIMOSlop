import {useEffect, useRef, useState} from 'react';
import type {KeyboardEvent} from 'react';
import {api, body, contextFence} from './lifecycle';

type Result = {key:string;kind:'action'|'object';category:string;title:string;context:string;url:string};
type Source = {key:string;label:string;state:'ready'|'partial'|'unavailable';message:string;limited?:boolean};
type SearchData = {marker:string;state:string;items:Result[];count:number;page:number;pages:number;page_size:number;partial:boolean;limited?:boolean;sources:Source[];suggestions:Result[];error?:string};
type Initial = {marker:string;endpoint:string;suggestions:Result[]};

export function Search({initial}:{initial:Initial}) {
  const [query,setQuery] = useState('');
  const [data,setData] = useState<SearchData|null>(null);
  const [phase,setPhase] = useState<'empty'|'typing'|'loading'|'ready'|'error'>('empty');
  const [error,setError] = useState('');
  const serial = useRef(0), pending = useRef<AbortController|null>(null), timer = useRef<ReturnType<typeof setTimeout>|null>(null);
  const input = useRef<HTMLInputElement>(null), list = useRef<HTMLUListElement>(null), heading = useRef<HTMLHeadingElement>(null);

  function cancel() {
    serial.current++;
    pending.current?.abort(); pending.current=null;
    if(timer.current)clearTimeout(timer.current);
    timer.current=null;
  }
  useEffect(()=>{
    const conceal=()=>{cancel();setData(null);setPhase('empty');setError('');};
    document.addEventListener('private:conceal',conceal);
    return()=>{cancel();document.removeEventListener('private:conceal',conceal);};
  },[]);

  async function find(value:string,page=1,focus=false) {
    cancel();
    if(!value.trim()){setData(null);setPhase('empty');setError('');return;}
    const number=serial.current, valid=contextFence(), controller=new AbortController();
    pending.current=controller;setPhase('loading');setError('');
    try {
      const response=await api<SearchData>(initial.endpoint,{method:'POST',body:body({q:value,page}),signal:controller.signal});
      if(number!==serial.current||controller.signal.aborted||!valid())return;
      if(!response.ok){setData(null);setPhase('error');setError(response.data.error||'Поиск временно недоступен. Повторите запрос.');return;}
      setData(response.data);setPhase('ready');
      if(focus)requestAnimationFrame(()=>{if(number===serial.current&&valid())heading.current?.focus({preventScroll:true});});
    } catch {
      if(number!==serial.current||controller.signal.aborted||!valid())return;
      setData(null);setPhase('error');setError('Поиск временно недоступен. Это не означает, что нужной записи нет.');
    } finally {if(number===serial.current)pending.current=null;}
  }
  function change(value:string) {
    cancel();setQuery(value);setData(null);setError('');
    setPhase(value.trim()?'typing':'empty');
    if(value.trim())timer.current=setTimeout(()=>{void find(value);},350);
  }
  function select(event:KeyboardEvent<HTMLElement>) {
    if(event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return;
    const links=Array.from(list.current?.querySelectorAll<HTMLAnchorElement>('a')||[]);
    if(!links.length)return;
    const current=links.indexOf(document.activeElement as HTMLAnchorElement);
    if(event.key==='ArrowDown'){event.preventDefault();links[Math.min(links.length-1,current+1)].focus();}
    else if(event.key==='ArrowUp'&&current>=0){event.preventDefault();if(current===0)input.current?.focus();else links[current-1].focus();}
    else if(event.key==='Escape'&&current>=0){event.preventDefault();input.current?.focus();}
  }
  const items=data?.items||[], loading=phase==='loading'||phase==='typing';
  const notices=data?.sources.filter(source=>source.state!=='ready')||[];
  return <section className="search-panel" aria-label="Поиск действий и записей">
    <form className="search-form" role="search" onSubmit={event=>{event.preventDefault();void find(query,1,true);}}>
      <label htmlFor="cabinet-search">Что найти?</label>
      <div className="search-controls"><input ref={input} id="cabinet-search" type="search" value={query} onChange={event=>change(event.target.value)} onKeyDown={select} maxLength={100} autoComplete="off" spellCheck={false} placeholder="Например, обмен или номер заявки" aria-controls="search-results"/><button type="submit">Найти</button></div>
    </form>
    <div role="status" aria-live="polite" className="search-status">{loading?'Ищем доступные сведения…':phase==='ready'?`${data?.limited?'Совпадений в ограниченном результате':data?.partial?'Совпадений в доступных сведениях':'Найдено'}: ${data?.count}.`:''}</div>
    {error&&<div className="save-recovery" role="alert"><p>{error}</p><button type="button" className="button-secondary" onClick={()=>find(query)}>Повторить поиск</button></div>}
    {notices.length>0&&<div className="data-state"><p>{notices.some(source=>!source.limited)?'Часть сведений неполна или недоступна.':'Результат ограничен числом совпадений.'}</p><ul>{notices.map(source=><li key={source.key}><strong>{source.label}.</strong> {source.message}</li>)}</ul></div>}
    <div id="search-results" aria-busy={loading}>
      {phase==='ready'&&<h2 ref={heading} tabIndex={-1}>Результаты поиска</h2>}
      {phase==='ready'&&items.length===0&&<div className="empty-state"><h3>{data?.partial?'В доступной выборке совпадений нет':'Совпадений нет'}</h3><p>Попробуйте часть названия, номер заявки или действие: «справка», «обмен», «консультация».</p></div>}
      {items.length>0&&<ul className="search-results" ref={list} onKeyDown={select}>{items.map(item=><li key={item.key}><p className="eyebrow">{item.kind==='action'?'Действие':item.category}</p><h3><a href={item.url}>{item.title}</a></h3><p className="support">{item.context}</p></li>)}</ul>}
      {data&&data.pages>1&&<nav className="pagination" aria-label="Страницы поиска"><button type="button" className="button-secondary" disabled={loading||data.page<=1} onClick={()=>find(query,data.page-1,true)}>Предыдущая</button><span>{data.page} из {data.pages}</span><button type="button" disabled={loading||data.page>=data.pages} onClick={()=>find(query,data.page+1,true)}>Следующая</button></nav>}
    </div>
    {phase==='empty'&&<section className="search-actions"><h2>Действия и разделы</h2><ul>{initial.suggestions.map(item=><li key={item.key}><a href={item.url}>{item.title}</a><p className="support">{item.context}</p></li>)}</ul></section>}
  </section>;
}
