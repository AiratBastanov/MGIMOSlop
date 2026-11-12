import {useEffect,useRef,useState} from 'react';
import {api,csrf,setDirty} from './lifecycle';
import {Person} from './Person';
import type {PersonValue} from './Person';
type Mode='text'|'link'|'email';
type Policy={version:number;modes:Mode[];text_limit:number;instructions:string;cloud_hosts:string[];attachments_allowed:boolean;attachment_types:string[];attachment_count:number;attachment_file_limit:number;attachment_total_limit:number};
type Attachment={id:string;name:string;kind:string;mime:string;size:number;url:string};
type Assignment={id:number;label:string;discipline:string;group:string;period:string;instructor_person:PersonValue;policy:Policy|null};
type Revision={id:number;number:number;title:string;mode:Mode;mode_label:string;content:string;submitted_at:string;
  task_conditions?:{late?:boolean;deadline_applies?:boolean};task_revision_id?:number;task_revision_number?:number;review:null|{status:string;status_label:string;feedback:string;result?:string;person:PersonValue};policy:Policy;attachments:Attachment[]};
type Material={task_id?:number;task_revision_id?:number;task_url?:string;task_title?:string;id:number;assignment_id:number;title:string;mode:Mode;content:string;version:number;has_draft:boolean;can_submit:boolean;url:string;revisions:Revision[];attachments:Attachment[]};
type Envelope={marker:string;state:string;assignments:Assignment[];material:Material|null;error?:{message:string;field:string};url?:string};
const labels:Record<Mode,string>={text:'Текст',link:'Ссылка на облако',email:'Контактная почта'};
const when=(value:string)=>new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'});
const sizeLabel=(size:number)=>size<1024*1024?`${(size/1024).toLocaleString('ru-RU',{maximumFractionDigits:1})} КиБ`:`${(size/1024/1024).toLocaleString('ru-RU',{maximumFractionDigits:1})} МиБ`;

export function MaterialEditor(){
  const [data,setData]=useState<Envelope|null>(null),[assignment,setAssignment]=useState(0);
  const [title,setTitle]=useState(''),[mode,setMode]=useState<Mode>('text'),[content,setContent]=useState('');
  const [state,setState]=useState('loading'),[message,setMessage]=useState(''),[errorField,setErrorField]=useState('');
  const [conflict,setConflict]=useState<Envelope|null>(null);
  const [savedFiles,setSavedFiles]=useState<Attachment[]>([]),[selectedFiles,setSelectedFiles]=useState<{id:string;file:File}[]>([]),[fileError,setFileError]=useState('');
  const picker=useRef<HTMLInputElement>(null),initialFiles=useRef('[]');
  const owner=useRef({}), attempt=useRef<{url:string;fields:Record<string,string|number>;files:File[];carry?:{title:string;mode:Mode;content:string;files:{id:string;file:File}[]}}|null>(null), initial=useRef('');
  const selected=data?.assignments.find(a=>a.id===assignment),policy=selected?.policy,material=data?.material;
  const endpoint=()=>material?`/study/materials/${material.id}/editor/`:'/study/materials/editor/';
  function accept(value:Envelope){
    setData(value);setAssignment(value.material?.assignment_id||value.assignments[0]?.id||0);
    const m=value.material;setTitle(m?.title||'');setMode(m?.mode||value.assignments[0]?.policy?.modes[0]||'text');setContent(m?.content||'');
    initial.current=JSON.stringify([m?.title||'',m?.mode||value.assignments[0]?.policy?.modes[0]||'text',m?.content||'']);
    setSavedFiles(m?.attachments||[]);setSelectedFiles([]);setFileError('');initialFiles.current=JSON.stringify((m?.attachments||[]).map(a=>a.id));
    setDirty(owner.current,false);
  }
  async function load(){
    setState('loading');setMessage('');
    try{const id=location.pathname.match(/materials\/(\d+)\//)?.[1];const {data:value,ok}=await api<Envelope>(id?`/study/materials/${id}/editor/`:'/study/materials/editor/');
      if(!ok)throw Error();accept(value);setState('ready');
    }catch{setState('error');setMessage('Не удалось открыть материал. Проверьте соединение и повторите.');}
  }
  useEffect(()=>{void load();return()=>{setDirty(owner.current,false);attempt.current=null;};},[]);
  const changed=JSON.stringify([title,mode,content])!==initial.current||selectedFiles.length>0||JSON.stringify(savedFiles.map(a=>a.id))!==initialFiles.current;
  useEffect(()=>{if(data)setDirty(owner.current,Boolean((!material||material.has_draft)&&changed));},[changed,data,material]);
  function selectFiles(files:FileList|null){
    if(!files?.length)return;const added=Array.from(files);
    if(!policy?.attachments_allowed){setFileError('Текущие условия не разрешают вложения.');return;}
    const kind=(f:File)=>{const ext=f.name.split('.').pop()?.toLowerCase();return ext==='jpg'?'jpeg':ext;};
    if(savedFiles.length+selectedFiles.length+added.length>policy.attachment_count){setFileError(`Можно выбрать до ${policy.attachment_count} файлов. Удалите лишние и повторите выбор.`);return;}
    if(added.some(f=>!policy.attachment_types.includes(kind(f)||'')||f.size>policy.attachment_file_limit||!f.size)){setFileError('Выберите непустые файлы разрешённых типов и размера.');return;}
    if([...savedFiles,...selectedFiles.map(f=>f.file),...added].reduce((sum,f)=>sum+f.size,0)>policy.attachment_total_limit){setFileError(`Общий размер не должен превышать ${sizeLabel(policy.attachment_total_limit)}.`);return;}
    setSelectedFiles(old=>[...old,...added.map(file=>({id:crypto.randomUUID(),file}))]);setFileError('');
  }
  async function send(action:string,retry=false,conflictCorrection=false){
    if(state==='saving')return;
    const current=conflictCorrection?conflict?.material:material;
    const operation=retry?attempt.current:{url:current?`/study/materials/${current.id}/editor/`:endpoint(),fields:{action,title,mode,content,assignment,
      operation:crypto.randomUUID(),attachment_ids:JSON.stringify(action==='correct'?[]:savedFiles.map(a=>a.id)),
      expected_version:current?.version||0,expected_policy:policy?.version||0,expected_task_revision:current?.task_revision_id||'',task:current?.task_id||''},files:action==='correct'?[]:selectedFiles.map(f=>f.file),carry:conflictCorrection?{title,mode,content,files:selectedFiles}:undefined};
    if(!operation)return;
    attempt.current=operation;setState('saving');setMessage('');setErrorField('');
    try{const payload=new FormData();Object.entries(operation.fields).forEach(([key,value])=>payload.append(key,String(value)));payload.append('context',window.deanery.marker);payload.append('csrfmiddlewaretoken',csrf());operation.files.forEach(file=>payload.append('files',file,file.name));
      const response=await api<Envelope>(operation.url,{method:'POST',body:payload,headers:{'X-CSRFToken':csrf()}},operation.files.length?60000:15000);
      if(!response.ok){setState('error');setMessage(response.data.error?.message||'Проверьте введённые данные.');setErrorField(response.data.error?.field||'');
        if(response.status===409)setConflict(response.data);attempt.current=null;return;}
      accept(response.data);setConflict(null);attempt.current=null;
      if(operation.carry){setTitle(operation.carry.title);setMode(operation.carry.mode);setContent(operation.carry.content);setSelectedFiles(operation.carry.files);setDirty(owner.current,true);}
      if(response.data.material&&location.pathname.endsWith('/new/'))history.replaceState(null,'',response.data.material.url);
      setState(response.data.state);setMessage(response.data.state==='submitted'?'Версия отправлена преподавателю.':response.data.state==='corrected'?'Открыт черновик исправления. Прежние версии и отзывы сохранены.':'Черновик сохранён.');
    }catch{setState('uncertain');setMessage('Ответ не получен. Действие могло сохраниться. Введённое содержимое осталось здесь.');}
  }
  function changeMode(value:Mode){setMode(value);setMessage(content?'Содержимое осталось в редакторе. Проверьте, что оно соответствует выбранному формату.':'');}
  if(!data)return <section className="surface empty-state"><p role="status">{message||'Открываем редактор…'}</p>{state==='error'&&<button onClick={load}>Повторить</button>}</section>;
  const locked=state==='saving'||state==='uncertain';
  return <div className={'material-workspace'+(!material?.revisions.length?' material-new':'')+(material&&!material.has_draft?' material-submitted':'')}>
    <section className="material-compose surface" aria-label="Редактор материала">{material?.task_title&&<p className="task-context">Ответ на задание: {material.task_url?<a href={material.task_url}>{material.task_title}</a>:material.task_title}</p>}
      {!material||material.has_draft?<>
        <div className="section-rule"><h2>{material?'Черновик':'Новая работа'}</h2><span className="tag" role="status">{state==='saving'?'Сохраняем…':state==='uncertain'?'Результат не подтверждён':changed?'Есть изменения':material?'Сохранён':'Не сохранён'}</span></div>
        <form noValidate onSubmit={e=>{e.preventDefault();void send(material?'submit':'save');}}>
          <fieldset disabled={locked} className="editor-fields">
            {!material&&<div className="field"><label htmlFor="assignment">Дисциплина и преподаватель</label><select id="assignment" value={assignment} onChange={e=>{const id=Number(e.target.value);setAssignment(id);const p=data.assignments.find(a=>a.id===id)?.policy;if(p&&!p.modes.includes(mode))changeMode(p.modes[0]);}} required>{!data.assignments.length&&<option value="">Нет доступных назначений</option>}{data.assignments.map(a=><option key={a.id} value={a.id}>{a.label}</option>)}</select></div>}
            {selected&&<div className="assignment-context"><p className="eyebrow">{selected.discipline} · {selected.period}</p><Person person={selected.instructor_person}/><div className="acceptance-rules"><h3>Условия приёма</h3>{policy?<><p>{policy.instructions||'Выберите один из доступных форматов.'}</p><p className="support">{policy.modes.map(m=>labels[m]).join(' · ')}{policy.modes.includes('text')?` · до ${policy.text_limit} символов`:''}</p></>:<p>Преподаватель ещё не открыл приём. Можно сохранить черновик.</p>}</div></div>}
            <div className="field"><label htmlFor="material-title">Название работы</label><input id="material-title" value={title} onChange={e=>setTitle(e.target.value)} maxLength={160} required aria-invalid={errorField==='title'} aria-describedby={errorField==='title'?'material-title-error':undefined} autoComplete="off"/>{errorField==='title'&&<p className="field-error" id="material-title-error">{message}</p>}</div>
            <div className="field"><label htmlFor="material-mode">Формат</label><select id="material-mode" value={mode} onChange={e=>changeMode(e.target.value as Mode)}>{(policy?.modes||(['text'] as Mode[])).map(m=><option key={m} value={m}>{labels[m]}</option>)}{policy&&!policy.modes.includes(mode)&&<option value={mode} disabled>{labels[mode]} — больше не принимается</option>}</select></div>
            <div className="field"><label htmlFor="material-content">{mode==='text'?'Содержание работы':mode==='link'?'HTTPS-ссылка на работу':'Контактный email'}</label>
              {mode==='text'?<textarea id="material-content" rows={9} value={content} onChange={e=>setContent(e.target.value)} maxLength={policy?.text_limit||20000} required={Boolean(material)} aria-describedby="material-help" aria-invalid={errorField==='content'}/>:
                <input id="material-content" type={mode==='link'?'url':'email'} value={content} onChange={e=>setContent(e.target.value)} maxLength={mode==='link'?1500:254} required={Boolean(material)} aria-describedby="material-help" aria-invalid={errorField==='content'} autoComplete="off"/>}
              <p className="support" id="material-help">{mode==='text'?`${content.length} / ${policy?.text_limit||20000} символов`:mode==='link'?`Допустимые облака: ${policy?.cloud_hosts.join(', ')||'см. условия'}. Содержимое ссылки не загружается в кабинет.`:'Адрес для связи с преподавателем; это не отправка письма из кабинета.'}</p>
            </div>
            <section className="attachment-editor" aria-labelledby="attachment-title"><h3 id="attachment-title">Вложения</h3>
              {policy?.attachments_allowed?<><p className="support">{policy.attachment_types.map(t=>t.toUpperCase()).join(', ')} · до {policy.attachment_count} файлов · {sizeLabel(policy.attachment_file_limit)} каждый · {sizeLabel(policy.attachment_total_limit)} вместе</p><input ref={picker} id="material-files" hidden type="file" multiple accept={policy.attachment_types.flatMap(t=>t==='jpeg'?['.jpg','.jpeg']:['.'+t]).join(',')} onChange={e=>{selectFiles(e.target.files);e.target.value='';}} tabIndex={-1}/><button type="button" className="button-secondary" onClick={()=>picker.current?.click()}>Выбрать файлы</button></>:<p className="support">Вложения не разрешены текущими условиями преподавателя.</p>}
              <ul className="attachment-list">{savedFiles.map(file=><li key={file.id}><div><a href={file.url} download>{file.name}</a><p className="support">{file.kind.toUpperCase()} · {sizeLabel(file.size)} · Сохранён в черновике</p></div><button type="button" className="button-quiet" onClick={()=>setSavedFiles(files=>files.filter(f=>f.id!==file.id))} aria-label={`Убрать ${file.name}`}>Убрать</button></li>)}{selectedFiles.map(({id,file})=><li key={id}><div><span>{file.name}</span><p className="support">{file.name.split('.').pop()?.toUpperCase()} · {sizeLabel(file.size)} · {state==='saving'?'Загружается…':state==='error'?'Не сохранён — проверьте ошибку':'Выбран, ещё не сохранён'}</p></div><button type="button" className="button-quiet" onClick={()=>setSelectedFiles(files=>files.filter(f=>f.id!==id))} aria-label={`Убрать ${file.name}`}>Убрать</button></li>)}</ul>
              {fileError&&<p className="field-error" role="alert">{fileError}</p>}{errorField==='attachments'&&<p className="field-error" role="alert">{message}</p>}
              <p className="support">Файлы сохраняются вместе с черновиком. Преподаватель получит только отправленную версию.</p>
            </section>
          </fieldset>
          {message&&<p className={['error','uncertain'].includes(state)?'field-error':'status-message'} role={state==='error'?'alert':'status'}>{message}</p>}
          {conflict&&<section className="request-conflict"><h3>Проверьте актуальную версию</h3><p>Ваш ввод сохранён в редакторе. Условия или черновик на сервере изменились.</p><details><summary>Сохранённые данные и новые условия</summary><p>{conflict.material?.title}</p><p className="material-content">{conflict.material?.content}</p><p>{conflict.assignments.find(a=>a.id===assignment)?.policy?.instructions}</p></details><button type="button" className="button-secondary" disabled={Boolean(conflict.material&&!conflict.material.has_draft)} onClick={()=>{setData(conflict);setSavedFiles(files=>files.filter(f=>conflict.material?.attachments.some(a=>a.id===f.id)));setConflict(null);setMessage('Актуальные условия приняты. Ваш текст сохранён; проверьте формат перед отправкой.');}}>Продолжить с моим текстом</button>{conflict.material&&!conflict.material.has_draft&&<p>Другая вкладка уже отправила работу. Ваш текст остаётся здесь.<button type="button" className="button-secondary" onClick={()=>send('correct',false,true)}>Открыть исправление с моим текстом</button></p>}</section>}
          {state==='uncertain'?<div className="actions"><button type="button" onClick={()=>send('',true)}>Повторить ту же отправку</button><a href="/study/materials/">Проверить мои материалы</a></div>:
            <div className="editor-actions"><button type="submit" disabled={locked||!assignment||Boolean(conflict)||Boolean(material&&!material.can_submit)}>{material?'Отправить преподавателю':'Создать черновик'}</button>{material&&<button type="button" className="button-secondary" disabled={locked||Boolean(conflict)} onClick={()=>send('save')}>Сохранить черновик</button>}</div>}
          <p className="form-note">{material?'Отправится именно содержимое редактора. Несохранённый ввод хранится только до выхода из этой страницы.':'Создание сохраняет черновик. После этого вы сможете отправить работу преподавателю.'}</p>
        </form>
      </>:<><p className="eyebrow">Работа отправлена</p><h2>{material.title}</h2><p className="status-message" role="status">{message||'Отправленная версия сохранена. Ожидайте отзыв преподавателя.'}</p><button type="button" disabled={locked} onClick={()=>send('correct')}>Подготовить исправление</button>{state==='uncertain'&&<button className="button-secondary" onClick={()=>send('',true)}>Повторить действие</button>}</>}
    </section>
    <aside className="material-history"><h2>Версии и отзывы</h2><p className="support">Отзыв на работу не заменяет официальный учебный итог.</p>{material?.revisions.length?material.revisions.map((r,index)=><article key={r.id} id={"revision-"+r.id} className="material-revision surface"><div className="section-rule"><h3>Версия {r.number}</h3><span className="tag">{r.review?.status_label||'Ожидает отзыва'}</span></div><p className="support">{when(r.submitted_at)} МСК · {r.mode_label}</p><h4>{r.title}</h4>{r.task_revision_id&&<p className="support">Условия задания зафиксированы в версии {r.task_revision_number}{r.task_conditions?.late?' · Отправлено после срока':''}{r.task_conditions?.late&&!r.task_conditions.deadline_applies?' · Задание назначено после срока, нарушение не устанавливается':''}</p>}{r.attachments?.length>0&&<ul className="attachment-list">{r.attachments.map(file=><li key={file.id}><div><a href={file.url} download>{file.name}</a><p className="support">{file.kind.toUpperCase()} · {sizeLabel(file.size)} · Отправлен в этой версии</p></div></li>)}</ul>}{r.review&&<div className={'material-feedback review-'+r.review.status.replaceAll('_','-')}><Person person={r.review.person}/><p>{r.review.feedback}</p>{r.review.result&&<p>Результат работы: {r.review.result==='pass'?'Зачтено':r.review.result==='fail'?'Не зачтено':r.review.result}</p>}</div>}<details open={index===0&&!material.has_draft}><summary>Отправленное содержание и условия</summary><div className="material-content">{r.content}</div><p className="support">Условия версии {r.policy.version}: {r.policy.instructions}</p></details></article>):<p className="quiet-empty">Отправленных версий пока нет. Черновик доступен только вам.</p>}</aside>
  </div>;
}
