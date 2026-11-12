import {useEffect,useRef,useState} from 'react';
import {api,body} from './lifecycle';
import {useDismiss} from './useDismiss';
type InboxData={marker:string;kind:string;archive:boolean;mode:string;read_all_token:string;count:number;unread:number;html:string;clear_token:string;undo:string|null};
const kinds=[['all','Все категории'],['requests','Заявки'],['schedule','Расписание'],['academic','Учёба и сессия'],['announcements','Объявления'],['deadlines','Сроки объявлений']];
export function Inbox({initial}:{initial:InboxData}){
  const [data,setData]=useState(initial),[reading,setReading]=useState(false),[writing,setWriting]=useState(false),[message,setMessage]=useState(''),[uncertain,setUncertain]=useState(false);
  const busy=reading||writing,readPending=useRef(false),mutation=useRef(false);
  const serial=useRef(0),attempt=useRef<{url:string;fields:Record<string,string>}|null>(null);
  const displayedUrl=useRef(location.pathname+location.search);
  const filter=useRef<HTMLDivElement>(null),filterTrigger=useRef<HTMLButtonElement>(null);
  const [filterOpen,setFilterOpen]=useState(false);
  useDismiss(filterOpen,[filter],reason=>{setFilterOpen(false);if(reason==='escape')filterTrigger.current?.focus({preventScroll:true});});
  function url(kind=data.kind,mode=data.mode){return `/notifications/?kind=${kind}&mode=${mode}`;}
  async function load(target:string,push=true,afterMutation=false){
    if(mutation.current&&!afterMutation){history.replaceState(null,'',displayedUrl.current);return;}
    const id=++serial.current;readPending.current=true;setReading(true);
    try{const response=await api<InboxData>(target);if(id!==serial.current)return;if(!response.ok)throw Error();
      setData(response.data);if(push)history.pushState(null,'',target);displayedUrl.current=target;document.dispatchEvent(new CustomEvent('private:unread',{detail:response.data.unread}));
    }catch{if(id===serial.current){history.replaceState(null,'',displayedUrl.current);setMessage('Лента не обновилась. Повторите действие; прежние записи остаются на экране.');}}
    finally{if(id===serial.current){readPending.current=false;setReading(false);}}
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
  useEffect(()=>{const back=()=>{void load(location.pathname+location.search,false);};addEventListener('popstate',back);return()=>{removeEventListener('popstate',back);attempt.current=null;serial.current++;};},[]);
  return <div className="inbox-component content-container notifications-container" aria-busy={busy}>
    <header className="planner-heading"><div><p className="eyebrow">События вашего кабинета</p><h1>Уведомления</h1><p className="weekday">{data.mode==='history'?'История · ':''}Всего: {data.count}</p></div>
      <div className="view-switch" aria-label="Лента уведомлений">{[['inbox','Входящие'],['history','История']].map(([mode,label])=><button key={mode} type="button" disabled={writing} aria-pressed={data.mode===mode} onClick={()=>load(url(data.kind,mode))}>{label}</button>)}</div>
    </header>
    <div className="inbox-toolbar"><div ref={filter} className="filter-popover"><button ref={filterTrigger} type="button" className="filter-trigger" aria-expanded={filterOpen} aria-controls="notification-filter" onClick={()=>setFilterOpen(!filterOpen)}>Фильтр: {kinds.find(k=>k[0]===data.kind)?.[1]}</button><div id="notification-filter" className="filter-content" hidden={!filterOpen}><label htmlFor="notification-kind">Категория</label><select id="notification-kind" value={data.kind} disabled={busy} onChange={e=>{setMessage('');void load(url(e.target.value));}}>{kinds.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></div></div>
      {data.mode==='inbox'&&<button type="button" className="button-quiet clear-inbox" disabled={busy||uncertain||!data.unread} onClick={()=>action('/notifications/read-all/',{snapshot:data.read_all_token})}>Прочитать все</button>}
    </div>
    {message&&<div className={uncertain?'save-recovery':'inline-status'} role="status"><p>{message}</p>{uncertain&&<div className="actions"><button className="button-secondary" disabled={busy} onClick={()=>action('',{},true)}>Повторить действие</button><button className="button-quiet" disabled={busy} onClick={()=>load(location.pathname+location.search,false)}>Обновить ленту</button></div>}</div>}
    <details className="retention-note"><summary>Сроки хранения</summary><p className="support">Прочитанное переходит в историю через 24 часа, остальные входящие — через 7 суток после появления. История видна 30 дней. Это правила демонстрационного кабинета; история самого объекта сохраняется.</p></details>
    <fieldset className="inbox-entries" aria-label="Темы уведомлений" disabled={busy||uncertain} onClick={e=>{const link=(e.target as Element).closest<HTMLAnchorElement>('.pagination a');if(link){e.preventDefault();void load(link.pathname+link.search);}}}
      onSubmit={e=>{e.preventDefault();const form=e.target as HTMLFormElement;if(busy)return;const fields=Object.fromEntries(new FormData(form).entries()) as Record<string,string>;void action(form.getAttribute('action')!,fields);}}
      dangerouslySetInnerHTML={{__html:data.html}}/>
  </div>;
}
