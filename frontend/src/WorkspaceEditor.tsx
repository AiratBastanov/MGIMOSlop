import {useEffect,useRef,useState} from 'react';
import {api,csrf,setDirty,body,contextFence} from './lifecycle';
type Field={name:string;label:string;value:string|number|string[];type:string;maxLength?:number;required?:boolean;locked?:boolean;options?:string[][];min?:number;max?:number;group?:string};
type FileValue={id:string;name:string;size:number;url:string};
type EditorData={marker:string;title:string;fields:Field[];actions:string[][];version:number;id?:number;endpoint:string;url:string;files:FileValue[];uploads:boolean;hint:string;task?:boolean;error?:string;state?:string;error_field?:string};
const values=(d:EditorData)=>Object.fromEntries(d.fields.map(f=>[f.name,f.value]));

export function WorkspaceEditor({initial}:{initial:EditorData}){
  const [data,setData]=useState(initial),[fields,setFields]=useState(values(initial)),[retained,setRetained]=useState(initial.files),[files,setFiles]=useState<File[]>([]);
  const [state,setState]=useState('ready'),[message,setMessage]=useState(''),[preview,setPreview]=useState(false),[conflict,setConflict]=useState<EditorData|null>(null);
  const owner=useRef({}),saved=useRef(JSON.stringify([values(initial),initial.files.map(f=>f.id)])),attempt=useRef<{url:string;payload:FormData}|null>(null);
  const changed=JSON.stringify([fields,retained.map(f=>f.id)])!==saved.current||files.length>0;
  useEffect(()=>{setDirty(owner.current,changed);},[changed]);
  useEffect(()=>()=>{setDirty(owner.current,false);attempt.current=null;},[]);
  const locked=state==='saving'||state==='uncertain';
  function choose(newFiles:FileList|null){
    const add=Array.from(newFiles||[]),all=[...retained,...files,...add];
    if(all.length>5||all.some(f=>f.size===0||f.size>10*1048576)||all.reduce((n,f)=>n+f.size,0)>25*1048576){setMessage('Допустимо до 5 непустых файлов, 10 МиБ каждый и 25 МиБ вместе.');return;}
    setFiles([...files,...add]);setMessage('');
  }
  async function send(action:string,retry=false){
    if(state==='saving')return;
    let current=attempt.current;
    if(!retry){
      const payload=new FormData();for(const [k,v] of Object.entries(fields)){if(Array.isArray(v))v.forEach(x=>payload.append(k,x));else payload.append(k,String(v));}
      payload.append('expected_version',String(data.version));payload.append('operation',crypto.randomUUID());payload.append('action',action);
      payload.append('attachment_ids',JSON.stringify(retained.map(f=>f.id)));payload.append('context',window.deanery.marker);payload.append('csrfmiddlewaretoken',csrf());files.forEach(f=>payload.append('files',f,f.name));
      current={url:data.endpoint,payload};attempt.current=current;
    }
    if(!current)return;setState('saving');setMessage('');
    try{
      const r=await api<EditorData>(current.url,{method:'POST',body:current.payload,headers:{'X-CSRFToken':csrf()}},files.length?60000:15000);
      if(!r.ok){setState('error');setMessage(r.data.error||'Сохранение не выполнено. Проверьте данные.');if(r.status===409)setConflict(r.data);attempt.current=null;return;}
      setData(r.data);setFields(values(r.data));setRetained(r.data.files);setFiles([]);saved.current=JSON.stringify([values(r.data),r.data.files.map(f=>f.id)]);setDirty(owner.current,false);attempt.current=null;setConflict(null);
      setState('saved');setMessage('Изменения сохранены. '+(action==='publish'?'Текущая версия опубликована для выбранной аудитории.':action==='save'&&r.data.uploads?'Черновик доступен только вам.':''));
      if(r.data.url&&location.pathname.includes('/new/'))history.replaceState(null,'',r.data.url);
    }catch{setState('uncertain');setMessage('Сервер не подтвердил результат. Действие могло сохраниться; повтор использует тот же ключ и данные.');}
  }
  function control(f:Field){
    if(f.type==='hidden')return null;
    const id='workspace-'+f.name,val=fields[f.name]??'';
    return <div className="field" key={f.name}><label htmlFor={id}>{f.label}</label>{f.type==='multi'?<div className="choice-row" id={id}>{f.options?.map(([v,label])=><label className="check-label" key={v}><input type="checkbox" checked={Array.isArray(val)&&val.includes(v)} onChange={e=>setFields({...fields,[f.name]:e.target.checked?[...(Array.isArray(val)?val:[]),v]:(Array.isArray(val)?val:[]).filter(x=>x!==v)})}/>{label}</label>)}</div>:f.type==='select'?<select id={id} value={String(val)} disabled={f.locked} onChange={e=>setFields({...fields,[f.name]:e.target.value})}>{!f.options?.some(([v])=>v===String(val))&&<option value="">Выберите</option>}{f.options?.map(([v,label])=><option key={v} value={v}>{label}</option>)}</select>:f.type==='textarea'?<textarea id={id} value={String(val)} rows={f.name==='body'?9:3} maxLength={f.maxLength} onChange={e=>setFields({...fields,[f.name]:e.target.value})}/>:<input id={id} type={f.type} value={String(val)} maxLength={f.maxLength} min={f.min} max={f.max} step={f.type==='number'?1:undefined} autoComplete="off" onChange={e=>setFields({...fields,[f.name]:e.target.value})}/>}</div>;
  }
  const groups=[...new Set(data.fields.map(f=>f.group).filter(Boolean))];
  return <section className="surface publication-editor"><div className="section-rule"><h2>{data.title}</h2><span className="tag" role="status">{state==='saving'?'Сохраняем…':state==='uncertain'?'Результат неизвестен':changed?'Есть изменения':data.id?'Сохранено':'Новая запись'}</span></div>
    <form noValidate onSubmit={e=>{e.preventDefault();void send('save');}}><fieldset disabled={locked} className="editor-fields">{data.fields.filter(f=>!f.group).map(control)}
      {data.task&&<p className="data-state">Без явного окончания приёма работа принимается и после срока, с пометкой о поздней отправке. «Срок не указан» означает отсутствие дедлайна. Результат работы не меняет официальный итог дисциплины.</p>}
      {groups.map(group=><details className="editor-section" key={group} open={group==='Условия задания'}><summary>{group}</summary><div className="editor-grid">{data.fields.filter(f=>f.group===group).map(control)}</div></details>)}
      {data.uploads&&<section className="attachment-editor"><h3>Файлы публикации</h3><p className="support">PDF, DOCX, TXT UTF-8, JPEG, PNG, статичный WebP. До 5 файлов, 10 МиБ каждый, 25 МиБ вместе.</p><label htmlFor="publication-files">Добавить файлы</label><input id="publication-files" type="file" multiple accept=".pdf,.docx,.txt,.jpg,.jpeg,.png,.webp" onChange={e=>{choose(e.target.files);e.target.value='';}}/>
        <ul className="attachment-list">{retained.map(f=><li key={f.id}><a href={f.url} download>{f.name}</a><button type="button" className="button-quiet" onClick={()=>setRetained(retained.filter(x=>x.id!==f.id))}>Убрать {f.name}</button></li>)}{files.map((f,i)=><li key={i}><span>{f.name} · выбран</span><button type="button" className="button-quiet" onClick={()=>setFiles(files.filter((_,n)=>n!==i))}>Убрать {f.name}</button></li>)}</ul></section>}
    </fieldset>
    {message&&<p className={state==='error'||state==='uncertain'?'field-error':'state-success'} role={state==='error'?'alert':'status'}>{message}</p>}
    {conflict&&<div className="request-conflict"><p>Ввод остался в этом окне. Сравните с текущей записью перед повтором.</p><a href={data.url||location.pathname} target="_blank" rel="noopener">Открыть сохранённую версию</a><button type="button" className="button-secondary" onClick={()=>{setData(conflict);setConflict(null);setMessage('Версия обновлена для следующей попытки. Текст остался вашим; проверьте различия.');}}>Я сравнил версии, продолжить с моим текстом</button></div>}
    <div className="editor-actions">{state==='uncertain'?<button type="button" onClick={()=>send('',true)}>Повторить то же действие</button>:data.actions.map(([action,label])=><button key={action} type="button" disabled={locked||Boolean(conflict)} className={action==='publish'?'':'button-secondary'} onClick={()=>send(action)}>{label}</button>)}<button type="button" className="button-quiet" onClick={()=>setPreview(!preview)} aria-expanded={preview}>Предпросмотр</button></div>
    <p className="support">{data.hint}</p>{state==='saved'&&data.url&&<a className="text-link" href={data.url}>Открыть сохранённую запись и историю</a>}</form>
    {preview&&<article className="publication-preview"><p className="eyebrow">Предпросмотр текущего ввода · ещё не публикация</p><h2>{String(fields.title||'Без названия')}</h2><p>{String(fields.topic||'')}</p><div className="material-content">{String(fields.body||fields.description||'')}</div><p className="support">{String(fields.links||fields.link||fields.url||'')}</p><ul>{[...retained,...files].map((f,i)=><li key={i}>{f.name}</li>)}</ul></article>}
  </section>;
}

type ExchangeData={marker:string;request_id:number;request_version:number;role:string;terminal:boolean;items:{id:number;version:number;state:string;label:string;messages:{action:string;body:string;at:string;actor:string}[]}[]};
export function ClarificationBox({initial:entry}:{initial:ExchangeData}){
  const [initial,setExchange]=useState(entry);
  useEffect(()=>{
    const receive=(event:Event)=>{
      const {exchange,viewed}=(event as CustomEvent<{exchange:ExchangeData;viewed?:{token:string}}>).detail;
      if(!exchange||exchange.marker!==window.deanery.marker||exchange.request_id!==entry.request_id)return;
      setExchange(old=>exchange.request_version>=old.request_version&&exchange.items.reduce((n,i)=>n+i.messages.length,0)>=old.items.reduce((n,i)=>n+i.messages.length,0)?exchange:old);
      if(viewed?.token)void api<{unread:number}>('/notifications/viewed-object/',{method:'POST',body:body({snapshot:viewed.token})}).then(r=>{if(r.ok)document.dispatchEvent(new CustomEvent('private:unread',{detail:r.data.unread}));}).catch(()=>{});
    };
    document.addEventListener('private:clarification',receive);
    return()=>document.removeEventListener('private:clarification',receive);
  },[entry.request_id]);
  const [text,setText]=useState(''),[state,setState]=useState('ready'),[message,setMessage]=useState('');
  const owner=useRef({}),attempt=useRef<URLSearchParams|null>(null),current=initial.items.find(i=>i.state!=='resolved');
  useEffect(()=>{setDirty(owner.current,Boolean(text));},[text]);useEffect(()=>()=>{setDirty(owner.current,false);attempt.current=null;},[]);
  async function send(action:string,retry=false){
    const payload=retry?attempt.current:body({action,body:text,request_version:initial.request_version,clarification:current?.id||'',expected_version:current?.version||1,operation:crypto.randomUUID()});
    if(!payload)return;attempt.current=payload;setState('saving');
    try{const r=await api<{state:string;error?:string;exchange:ExchangeData}>('/requests/'+initial.request_id+'/clarification/',{method:'POST',body:payload});if(!r.ok){if(r.data.exchange)setExchange(r.data.exchange);setState('error');setMessage(r.data.error||'Проверьте данные.');attempt.current=null;return;}setDirty(owner.current,false);setText('');setExchange(r.data.exchange);setState('saved');setMessage('Уточнение сохранено.');attempt.current=null;}
    catch{setState('uncertain');setMessage('Ответ сервера не получен. Ввод сохранён; повторите то же действие.');}
  }
  const canAsk=initial.role==='staff'&&!current&&!initial.terminal,canReply=initial.role==='student'&&current?.state==='open'&&!initial.terminal;
  return <section id="clarification" className="surface clarification-box"><h2>Уточнение по заявке</h2>{initial.items.map(item=><article key={item.id}><p className="request-status">{item.label}</p>{item.messages.map((m,i)=><div className="clarification-message" key={i}><p className="support">{m.actor} · {new Date(m.at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})} МСК</p><div className="material-content">{m.body}</div></div>)}</article>)}{!initial.items.length&&<p className="support">Уточнений пока нет. Исходные поля заявки остаются неизменными.</p>}
    {(canAsk||canReply)&&<form onSubmit={e=>{e.preventDefault();send(canAsk?'ask':'reply');}}><label htmlFor="clarification-body">{canAsk?'Что нужно уточнить':'Ваш ответ'}</label><textarea id="clarification-body" maxLength={2000} rows={4} value={text} disabled={state==='saving'||state==='uncertain'} onChange={e=>setText(e.target.value)}/><button disabled={state==='saving'||state==='uncertain'}>{canAsk?'Запросить уточнение':'Отправить ответ'}</button></form>}
    {initial.role==='staff'&&current?.state==='answered'&&!initial.terminal&&<button disabled={state==='saving'||state==='uncertain'} onClick={()=>send('resolve')}>Уточнение рассмотрено</button>}
    {message&&<p role="status" className="data-state">{message}</p>}{state==='uncertain'&&<button onClick={()=>send('',true)}>Повторить то же действие</button>}
    {text&&!canAsk&&!canReply&&<><label htmlFor="clarification-preserved">Ваш несохранённый текст</label><textarea id="clarification-preserved" readOnly rows={4} value={text}/></>}
    {initial.terminal&&<p className="support">Заявка завершена. История уточнений сохранена.</p>}</section>;
}

export function ViewedEvents({initial}:{initial:{token:string}}){
  useEffect(()=>{const live=contextFence();void api<{unread:number}>('/notifications/viewed-object/',{method:'POST',body:body({snapshot:initial.token})}).then(r=>{if(live()&&r.ok)document.dispatchEvent(new CustomEvent('private:unread',{detail:r.data.unread}));}).catch(()=>{});},[]);
  return <span hidden aria-hidden="true" data-view-receipt="true">Проверка отметок прочтения</span>;
}
