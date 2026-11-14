import {useEffect,useRef,useState} from 'react';
import {api,body,contextFence} from './lifecycle';
import {useDismiss} from './useDismiss';
type InboxData={marker:string;kind:string;archive:boolean;mode:string;read_all_token:string;count:number;unread:number;html:string;clear_token:string;undo:string|null};
const kinds=[['all','Все категории'],['requests','Заявки'],['schedule','Расписание'],['academic','Учёба и сессия'],['announcements','Объявления'],['deadlines','Сроки объявлений']];
function viewKey(target:string){
  const parsed=new URL(target,location.origin);
  return `${parsed.pathname}?kind=${parsed.searchParams.get('kind')||'all'}&mode=${parsed.searchParams.get('mode')||(parsed.searchParams.get('archive')==='1'?'history':'inbox')}&page=${parsed.searchParams.get('page')||'1'}`;
}
export function Inbox({initial}:{initial:InboxData}){
  const [data,setData]=useState(initial),[reading,setReading]=useState(false),[writing,setWriting]=useState(false),[message,setMessage]=useState(''),[uncertain,setUncertain]=useState(false);
  const [requested,setRequested]=useState<string|null>(null),[failed,setFailed]=useState<string|null>(null);
  const busy=reading||writing,readPending=useRef(false),mutation=useRef(false);
  const serial=useRef(0),attempt=useRef<{url:string;fields:Record<string,string>}|null>(null);
  const activeRead=useRef<{target:string;controller:AbortController}|null>(null);
  const displayedUrl=useRef(location.pathname+location.search);
  const filter=useRef<HTMLDivElement>(null),filterTrigger=useRef<HTMLButtonElement>(null);
  const [filterOpen,setFilterOpen]=useState(false);
  useDismiss(filterOpen,[filter],reason=>{setFilterOpen(false);if(reason==='escape')filterTrigger.current?.focus({preventScroll:true});});
  function url(kind=data.kind,mode=data.mode){return `/notifications/?kind=${kind}&mode=${mode}`;}
  async function load(target:string,push=true,afterMutation=false,force=false){
    if(mutation.current&&!afterMutation){history.replaceState(null,'',displayedUrl.current);return;}
    if(activeRead.current&&viewKey(activeRead.current.target)===viewKey(target))return;
    const id=++serial.current;activeRead.current?.controller.abort();activeRead.current=null;
    setFailed(null);if(!afterMutation)setMessage('');setFilterOpen(false);
    if(!force&&!afterMutation&&viewKey(target)===viewKey(displayedUrl.current)){
      readPending.current=false;setReading(false);setRequested(null);return;
    }
    const controller=new AbortController(),valid=contextFence();
    activeRead.current={target,controller};readPending.current=true;setReading(true);setRequested(target);
    try{const response=await api<InboxData>(target,{signal:controller.signal});if(id!==serial.current||!valid())return;if(!response.ok)throw Error();
      setData(response.data);
      if(push&&viewKey(target)!==viewKey(displayedUrl.current))history.pushState(null,'',target);
      else history.replaceState(null,'',target);
      displayedUrl.current=target;document.dispatchEvent(new CustomEvent('private:unread',{detail:response.data.unread}));
    }catch{if(id===serial.current&&valid()&&!controller.signal.aborted){history.replaceState(null,'',displayedUrl.current);setFailed(target);setMessage('Не удалось загрузить ленту. На экране — прежние записи.');}}
    finally{if(id===serial.current){activeRead.current=null;readPending.current=false;setReading(false);setRequested(null);}}
  }
  async function action(target:string,fields:Record<string,string>,retry=false){
    if(mutation.current||readPending.current||!retry&&attempt.current)return;
    const op=retry?attempt.current:{url:target,fields};if(!op)return;
    attempt.current=op;mutation.current=true;setWriting(true);setMessage('');setUncertain(false);
    try{const result=await api<{marker:string;undo?:string;error?:string}>(op.url,{method:'POST',body:body(op.fields)});
      if(!result.ok){attempt.current=null;setMessage(result.data.error||'Действие недоступно. Обновите ленту.');setUncertain(false);return;}attempt.current=null;
      setMessage(op.url.includes('/read-all/')?'Уведомления из показанного снимка прочитаны. Новые события останутся непрочитанными.':'Отметка прочтения сохранена.');
      const refresh=new URL(displayedUrl.current,location.origin);
      if(result.data.undo){refresh.searchParams.set('undo',result.data.undo);refresh.searchParams.delete('page');}
      await load(refresh.pathname+refresh.search,true,true);
    }catch{setUncertain(true);setMessage('Ответ не подтверждён. Действие могло сохраниться. Проверьте ленту или повторите это действие.');}
    finally{mutation.current=false;setWriting(false);}
  }
  useEffect(()=>{
    const back=()=>{void load(location.pathname+location.search,false);};
    const cancel=()=>{serial.current++;activeRead.current?.controller.abort();activeRead.current=null;readPending.current=false;setReading(false);setRequested(null);};
    addEventListener('popstate',back);document.addEventListener('private:conceal',cancel);
    return()=>{removeEventListener('popstate',back);document.removeEventListener('private:conceal',cancel);activeRead.current?.controller.abort();attempt.current=null;serial.current++;};
  },[]);
  const requestedMode=requested?new URL(requested,location.origin).searchParams.get('mode'):null;
  const loading=reading?(requestedMode==='history'?'Загружаем историю…':'Загружаем входящие…'):'';
  return <div className="inbox-component content-container notifications-container">
    <header className="planner-heading"><div><p className="eyebrow">События вашего кабинета</p><h1>Уведомления</h1><p className="weekday">{data.mode==='history'?'История · ':''}Всего: {data.count}</p></div>
      <div className="view-switch" aria-label="Лента уведомлений">{[['inbox','Входящие'],['history','История']].map(([mode,label])=><button key={mode} type="button" disabled={writing} aria-pressed={data.mode===mode} data-requested={requestedMode===mode||undefined} onClick={()=>load(url(data.kind,mode))}>{label}</button>)}</div>
    </header>
    <div className="inbox-feedback"><p className={reading?'inbox-loading':'inline-status'} role="status" aria-live="polite" aria-atomic="true">{reading&&<span className="loading-indicator" aria-hidden="true"/>}{loading||message}</p>
      {failed&&<button type="button" className="button-secondary" disabled={busy} onClick={()=>load(failed,true,false,true)}>Повторить загрузку</button>}
      <p className="support">На экране: {data.mode==='history'?'история':'входящие'}.</p>
    </div>
    <div className="inbox-toolbar"><div ref={filter} className="filter-popover"><button ref={filterTrigger} type="button" className="filter-trigger" aria-expanded={filterOpen} aria-controls="notification-filter" onClick={()=>setFilterOpen(!filterOpen)}>Фильтр: {kinds.find(k=>k[0]===data.kind)?.[1]}</button><div id="notification-filter" className="filter-content" hidden={!filterOpen}><label htmlFor="notification-kind">Категория</label><select id="notification-kind" value={data.kind} disabled={busy} onChange={e=>{setMessage('');void load(url(e.target.value));}}>{kinds.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></div></div>
      {data.mode==='inbox'&&<button type="button" className="button-quiet clear-inbox" disabled={busy||uncertain||!data.unread} onClick={()=>action('/notifications/read-all/',{snapshot:data.read_all_token})}>Прочитать все</button>}
    </div>
    {uncertain&&<div className="save-recovery actions"><button className="button-secondary" disabled={busy} onClick={()=>action('',{},true)}>Повторить действие</button><button className="button-quiet" disabled={busy} onClick={()=>load(displayedUrl.current,false,false,true)}>Обновить ленту</button></div>}
    <details className="retention-note"><summary>Сроки хранения</summary><p className="support">Прочитанное переходит в историю через 24 часа, остальные входящие — через 7 суток после появления. История видна 30 дней. Это правила демонстрационного кабинета; история самого объекта сохраняется.</p></details>
    <fieldset className="inbox-entries" aria-label="Темы уведомлений" aria-busy={busy} disabled={busy||uncertain} onClick={e=>{const link=(e.target as Element).closest<HTMLAnchorElement>('a');if(readPending.current&&link){e.preventDefault();return;}if(link?.closest('.pagination')){e.preventDefault();void load(link.pathname+link.search);}}}
      onSubmit={e=>{e.preventDefault();const form=e.target as HTMLFormElement;if(busy)return;const fields=Object.fromEntries(new FormData(form).entries()) as Record<string,string>;void action(form.getAttribute('action')!,fields);}}
      dangerouslySetInnerHTML={{__html:data.html}}/>
  </div>;
}
