import {useEffect, useRef, useState} from 'react';
import type {KeyboardEvent} from 'react';
import {api} from './lifecycle';

type ScheduleData = {marker:string; selected:string; today:string; view:'day'|'week'; group:string; period:string;
  base?:string; heading?:string; previous:string; next:string; days:{date:string;state:string}[]; html:string; source:string; error:string};
const date = (value:string) => new Date(value+'T12:00:00');
const iso = (value:Date) => `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;
const label = (value:string, options:Intl.DateTimeFormatOptions) => date(value).toLocaleDateString('ru-RU',options);
const add = (value:string, count:number) => { const d=date(value); d.setDate(d.getDate()+count); return iso(d); };
function monthMove(value:string, offset:number) {const d=date(value);d.setDate(1);d.setMonth(d.getMonth()+offset);return iso(d);}

function Calendar({selected,onSelect,onClose}: {selected:string;onSelect:(v:string)=>void;onClose:()=>void}) {
  const [cursor,setCursor] = useState(selected);
  const [yearInput,setYearInput]=useState(String(date(selected).getFullYear()));
  const modal = useRef<HTMLDialogElement>(null);
  const focusDate = useRef(true);
  const month = date(cursor), start = new Date(month.getFullYear(),month.getMonth(),1,12);
  const offset=(start.getDay()+6)%7, total=new Date(month.getFullYear(),month.getMonth()+1,0).getDate();
  useEffect(()=>{modal.current?.showModal(); const hide=()=>{modal.current?.close();onClose();}; document.addEventListener('private:conceal',hide);return()=>{document.removeEventListener('private:conceal',hide);modal.current?.close();};},[]);
  useEffect(()=>{if(focusDate.current)modal.current?.querySelector<HTMLButtonElement>('[tabindex="0"]')?.focus();},[cursor]);
  useEffect(()=>{setYearInput(String(date(cursor).getFullYear()));},[cursor]);
  function chooseMonth(monthNumber:number,year=month.getFullYear()) {focusDate.current=false;setCursor(iso(new Date(year,monthNumber,1,12)));}
  function commitYear(){const year=Number(yearInput);if(/^\d{4}$/.test(yearInput)&&year>=1901&&year<=2099)chooseMonth(month.getMonth(),year);}
  function keys(e:KeyboardEvent, value:string) {
    let target='';
    const day=(date(value).getDay()+6)%7;
    if(e.key==='ArrowLeft')target=add(value,-1);
    if(e.key==='ArrowRight')target=add(value,1);
    if(e.key==='ArrowUp')target=add(value,-7);
    if(e.key==='ArrowDown')target=add(value,7);
    if(e.key==='Home')target=add(value,-day);
    if(e.key==='End')target=add(value,6-day);
    if(e.key==='PageUp')target=monthMove(value,e.shiftKey?-12:-1);
    if(e.key==='PageDown')target=monthMove(value,e.shiftKey?12:1);
    if(target){e.preventDefault();if(date(target).getFullYear()>=1901 && date(target).getFullYear()<=2099){focusDate.current=true;setCursor(target);}}
  }
  return <dialog ref={modal} className="calendar-dialog" aria-labelledby="calendar-title" onCancel={onClose}>
    <div className="section-rule"><h2 id="calendar-title">Выбрать дату</h2><button type="button" className="button-quiet" onClick={onClose}>Закрыть</button></div>
    <div className="calendar-selectors"><div><label htmlFor="calendar-month">Месяц</label><select id="calendar-month" value={month.getMonth()} onChange={e=>chooseMonth(Number(e.target.value))}>{Array.from({length:12},(_,i)=><option key={i} value={i}>{new Date(2024,i,1).toLocaleDateString('ru-RU',{month:'long'})}</option>)}</select></div><div><label htmlFor="calendar-year">Год</label><input id="calendar-year" type="text" inputMode="numeric" value={yearInput} maxLength={4} onChange={e=>setYearInput(e.target.value)} onBlur={commitYear} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();commitYear();}}} aria-describedby="calendar-bounds"/></div></div>
    {(!/^\d{4}$/.test(yearInput)||Number(yearInput)<1901||Number(yearInput)>2099)&&<p className="field-error" role="status">Введите год от 1901 до 2099.</p>}
    <div className="calendar-month-controls"><button type="button" className="button-secondary" disabled={cursor.startsWith('1901-01')} onClick={()=>{focusDate.current=false;setCursor(monthMove(cursor,-1));}}>Пред. месяц</button><button type="button" className="button-secondary" disabled={cursor.startsWith('2099-12')} onClick={()=>{focusDate.current=false;setCursor(monthMove(cursor,1));}}>След. месяц</button></div>
    <h3 className="calendar-month" aria-live="polite">{label(cursor,{month:'long',year:'numeric'})}</h3>
    <table className="calendar-grid" role="grid" aria-label="Календарь"><thead><tr>{['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].map(d=><th scope="col" key={d}>{d}</th>)}</tr></thead><tbody>
      {Array.from({length:Math.ceil((total+offset)/7)},(_,week)=><tr key={week}>{Array.from({length:7},(_,n)=>{
        const number=week*7+n-offset+1;
        if(number<1||number>total)return <td key={n}/>;
        const value=iso(new Date(month.getFullYear(),month.getMonth(),number,12));
        return <td key={n} aria-selected={value===selected}><button type="button" tabIndex={value===cursor?0:-1} aria-label={label(value,{day:'numeric',month:'long',year:'numeric'})} aria-pressed={value===selected} onKeyDown={e=>keys(e,value)} onFocus={()=>{focusDate.current=false;setCursor(value);}} onClick={()=>onSelect(value)}>{number}</button></td>;
      })}</tr>)}
    </tbody></table><p className="calendar-selection-note">Выбрано: {label(selected,{day:'numeric',month:'long',year:'numeric'})}. Просмотр месяца не меняет дату — нажмите нужный день.</p><p id="calendar-bounds" className="support">1901–2099. Доступность занятий проверяется после выбора дня.</p><p className="support">Стрелки — дни; Page Up / Page Down — месяцы, с Shift — годы. Enter — выбрать.</p>
  </dialog>;
}

export function Schedule({initial}: {initial:ScheduleData}) {
  const [data,setData] = useState(initial), [open,setOpen]=useState(false), [busy,setBusy]=useState(false), [error,setError]=useState(initial.error);
  const calendarButton=useRef<HTMLButtonElement>(null), strip=useRef<HTMLDivElement>(null), sequence=useRef(0);
  const displayedUrl=useRef(location.pathname+location.search);
  const url=(day:string,view=data.view)=>(data.base||'/schedule/')+'?date='+day+'&view='+view;
  async function navigate(target:string, push=true) {
    const serial=++sequence.current;
    setBusy(true);setError('');
    try {const result=await api<ScheduleData>(target);if(serial!==sequence.current)return;
      if(!result.ok)throw Error(result.data.error||'Проверьте дату.');
      setData(result.data);if(push)history.pushState(null,'',target);displayedUrl.current=target;
    } catch(e){if(serial===sequence.current){history.replaceState(null,'',displayedUrl.current);setError('Не удалось открыть дату. Сохранён прежний день; попробуйте снова.');}}
    finally{if(serial===sequence.current)setBusy(false);}
  }
  useEffect(()=>{const back=()=>{void navigate(location.pathname+location.search,false);};addEventListener('popstate',back);return()=>{removeEventListener('popstate',back);sequence.current++;};},[]);
  useEffect(()=>{
    const rail=strip.current, selected=rail?.querySelector<HTMLElement>('[aria-current="date"]');
    if(rail&&selected)rail.scrollLeft=selected.offsetLeft-rail.offsetLeft-(rail.clientWidth-selected.clientWidth)/2;
  },[data.selected]);
  function close(returnFocus=true){setOpen(false);if(returnFocus)requestAnimationFrame(()=>{if(window.deanery.active)calendarButton.current?.focus();});}
  return <div className="schedule-component" aria-busy={busy}>
    <header className="planner-heading"><div><p className="eyebrow">Учебный план · {data.group}</p><h1>{data.heading||'Расписание'}</h1></div>
      <div className="view-switch" aria-label="Вид расписания">{(['day','week'] as const).map(v=><button key={v} type="button" aria-pressed={data.view===v} onClick={()=>navigate(url(data.selected,v))}>{v==='day'?'День':'Неделя'}</button>)}</div>
    </header>
    <section className="date-workbench" aria-label="Выбор даты">
      <div className="date-title-row"><p className="date-period">{data.view==='week'?'Неделя':'День'} · {data.period}</p><button ref={calendarButton} className="button-secondary calendar-open" type="button" onClick={()=>setOpen(true)}><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="16" rx="2"/><path d="M8 3v4m8-4v4M4 10h16m-12 4h3m2 0h3"/></svg>Календарь</button></div>
      <div className="date-step"><button type="button" className="button-quiet" onClick={()=>navigate(data.previous)} aria-label={data.view==='week'?'Предыдущая неделя':'Предыдущий день'}><span>Предыдущ{data.view==='week'?'ая':'ий'}<span className="period-unit">{data.view==='week'?'неделя':'день'}</span></span></button><button type="button" className="button-quiet" onClick={()=>navigate(url(data.today))}>Сегодня</button><button type="button" className="button-quiet" onClick={()=>navigate(data.next)} aria-label={data.view==='week'?'Следующая неделя':'Следующий день'}><span>Следующ{data.view==='week'?'ая':'ий'}<span className="period-unit">{data.view==='week'?'неделя':'день'}</span></span></button></div>
      <div className="month-caption"><strong>{label(data.selected,{month:'long',year:'numeric'})}</strong><details className="date-legend"><summary>Обозначения</summary><p className="month-legend"><span><i className="day-indicator day-classes"/>Есть занятия</span><span><i className="day-indicator day-empty"/>Пар нет</span><span><i className="day-indicator day-unknown"/>Не подтверждено</span></p></details></div>
      <div className="month-strip" ref={strip} data-month-strip aria-label="Все даты месяца">{data.days.map(d=>{
        const state=['complete','finished'].includes(d.state)?'classes':d.state==='complete_empty'?'empty':'unknown';
        return <button key={d.date} type="button" aria-current={d.date===data.selected?'date':undefined} aria-label={`${label(d.date,{day:'numeric',month:'long',weekday:'long'})}, ${state==='classes'?'есть занятия':state==='empty'?'пар нет':'полнота не подтверждена'}`} onClick={()=>navigate(url(d.date))}><span>{label(d.date,{weekday:'short'})}</span><strong>{date(d.date).getDate()}</strong><span className={'day-indicator day-'+state} aria-hidden="true"/></button>;
      })}</div>
    </section>
    {error&&<p className="field-error" role="alert">{error}</p>}
    <p className="sr-only" role="status">{busy?'Открываем день…':`Выбрано ${label(data.selected,{day:'numeric',month:'long',weekday:'long'})}`}</p>
    <div className="agenda-fragment" key={data.selected+data.view} dangerouslySetInnerHTML={{__html:data.html}}/>
    {data.view==='week'&&<div className="date-step schedule-bottom-navigation"><button className="button-secondary" onClick={()=>navigate(data.previous)}>Предыдущая неделя</button><button className="button-secondary" onClick={()=>navigate(data.next)}>Следующая неделя</button></div>}
    <div dangerouslySetInnerHTML={{__html:data.source}}/>
    {open&&<Calendar selected={data.selected} onClose={()=>close()} onSelect={d=>{close();void navigate(url(d));}}/>}
  </div>;
}
