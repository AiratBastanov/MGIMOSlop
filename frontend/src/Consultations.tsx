import {useEffect,useRef,useState} from 'react';
import {api,body,contextFence,setDirty} from './lifecycle';

type RelatedRequest={id:number;reference:string;url:string};
type Conflict={blocking:boolean;academic:boolean;complete:boolean;message:string};
type Slot={id:number;version:number;topic:string;starts_at:string;ends_at:string;host:string;
  venue:string;meeting_url:string;audience:string;kind:string;state:string;state_label:string;
  available:boolean;conflict?:Conflict;can_close:boolean;can_cancel:boolean;can_venue:boolean};
type Booking={id:number;version:number;state:string;state_label:string;slot:Slot;question:string;
  request:RelatedRequest|null;history:{action:string;label:string;at:string}[];
  can_cancel:boolean;can_rebook:boolean;can_complete:boolean;student?:string;audience_message?:string};
type Audience={id:number;label:string;kind:string};
export type ConsultationData={marker:string;role:string;slots:Slot[];bookings:Booking[];audiences:Audience[];
  endpoint:string;state_url?:string;page:number;pages:number;count:number;booking_page?:number;
  booking_pages?:number;booking_count?:number;selected_booking?:number;selected_slot?:number;
  request_context?:RelatedRequest|null;preferred_assignment?:number;source_message?:string;error?:string;message?:string};
type Result={marker:string;state:string;message?:string;error?:string;field?:string;booking_id?:number;slot_id?:number;url?:string};
type Fields=Record<string,string|number>;
type Choice={action:'book'|'rebook'|'cancel'|'close'|'cancel_slot'|'venue'|'complete';slot:Slot;booking?:Booking};
const instant=(value:string)=>new Date(value).toLocaleString('ru-RU',{
  timeZone:'Europe/Moscow',day:'numeric',month:'long',hour:'2-digit',minute:'2-digit',year:'numeric'});
const time=(value:string)=>new Date(value).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'});
const day=(value:string)=>new Date(value).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow'});
const period=(slot:Slot)=>`${instant(slot.starts_at)} – ${day(slot.starts_at)===day(slot.ends_at)?time(slot.ends_at):instant(slot.ends_at)} МСК`;
const kindLabel=(kind:string)=>kind==='teacher'?'Учебная консультация':'Факультетская служба';

function SlotFacts({slot}:{slot:Slot}){
  return <dl className="consultation-facts">
    <dt>Когда</dt><dd className="consultation-time">{period(slot)}</dd>
    <dt>Ведущий</dt><dd>{slot.host}</dd>
    <dt>Место</dt><dd>{slot.venue||'Онлайн'}{slot.meeting_url&&<> · <a href={slot.meeting_url} target="_blank" rel="noopener noreferrer">Ссылка на встречу</a></>}</dd>
    <dt>Для кого</dt><dd>{slot.audience}</dd>
  </dl>;
}

function ConflictNote({conflict}:{conflict?:Conflict}){
  if(!conflict)return null;
  return <p className={(conflict.blocking||conflict.academic||!conflict.complete?'data-state':'support')+' consultation-conflict'}>{conflict.message}</p>;
}

function Pager({page,pages,label,disabled,onPage}:{page:number;pages:number;label:string;disabled:boolean;onPage:(page:number)=>void}){
  if(pages<=1)return null;
  return <nav className="consultation-pagination" aria-label={label}>
    <button type="button" className="button-secondary" disabled={disabled||page<=1} onClick={()=>onPage(page-1)}>Предыдущая</button>
    <span>Страница {page} из {pages}</span>
    <button type="button" disabled={disabled||page>=pages} onClick={()=>onPage(page+1)}>Следующая</button>
  </nav>;
}

export function Consultations({initial}:{initial:ConsultationData}){
  const [data,setData]=useState(initial),[state,setState]=useState('ready'),[message,setMessage]=useState('');
  const [choice,setChoice]=useState<Choice|null>(null),[moving,setMoving]=useState<Booking|null>(null);
  const [question,setQuestion]=useState(''),[shareRequest,setShareRequest]=useState(false),[ack,setAck]=useState(false);
  const [venue,setVenue]=useState(''),[meetingUrl,setMeetingUrl]=useState('');
  const [showCreate,setShowCreate]=useState(false),[create,setCreate]=useState({topic:'',starts_at:'',ends_at:'',venue:'',meeting_url:'',audience:String(initial.audiences.find(a=>a.id===initial.preferred_assignment)?.id||initial.audiences[0]?.id||'')});
  const [fieldError,setFieldError]=useState('');
  const owner=useRef({}),attempt=useRef<{fields:Fields;success:string}|null>(null),sequence=useRef(0);
  const confirmation=useRef<HTMLHeadingElement>(null),messageRef=useRef<HTMLDivElement>(null),selectionButton=useRef<HTMLElement|null>(null);
  const pending=state==='saving'||state==='loading',uncertain=state==='uncertain',stale=state==='saved_stale';
  const locked=pending||uncertain||stale,student=data.role==='student';
  const dirty=Boolean(question||create.topic||create.starts_at||create.ends_at||create.venue||create.meeting_url||choice?.action==='venue'&&(venue!==choice.slot.venue||meetingUrl!==choice.slot.meeting_url));
  useEffect(()=>{setDirty(owner.current,dirty);},[dirty]);
  useEffect(()=>()=>{setDirty(owner.current,false);attempt.current=null;sequence.current++;},[]);
  useEffect(()=>{
    const conceal=()=>{
      sequence.current++;
      if(attempt.current){setState('uncertain');setMessage('Ответ прерван проверкой доступа. После подтверждения сеанса повторите то же действие.');}
      else setState(old=>old==='loading'?'ready':old);
    };
    document.addEventListener('private:conceal',conceal);
    return()=>document.removeEventListener('private:conceal',conceal);
  },[]);
  useEffect(()=>{
    if(choice&&window.deanery.active)confirmation.current?.focus();
  },[choice]);
  useEffect(()=>{
    const id=initial.selected_booking?`consultation-booking-${initial.selected_booking}`:initial.selected_slot?`consultation-slot-${initial.selected_slot}`:'';
    if(id){const frame=requestAnimationFrame(()=>{if(window.deanery.active)document.getElementById(id)?.scrollIntoView({block:'center'});});return()=>cancelAnimationFrame(frame);}
  },[]);

  function readUrl(changes:Fields={},targetPath?:string){
    const url=new URL(data.state_url||'/consultations/state/',location.origin);
    if(targetPath)url.pathname=targetPath;
    url.searchParams.set('page',String(data.page));
    url.searchParams.set('booking_page',String(data.booking_page||1));
    if(moving)url.searchParams.set('rebook',String(moving.id));else url.searchParams.delete('rebook');
    for(const [key,value] of Object.entries(changes))value===''?url.searchParams.delete(key):url.searchParams.set(key,String(value));
    return url.pathname+url.search;
  }
  async function refresh(changes:Fields={},afterSave=false,targetPath?:string){
    const serial=++sequence.current,live=contextFence();
    setState('loading');
    try{
      const response=await api<ConsultationData>(readUrl(changes,targetPath));
      if(serial!==sequence.current||!live())return false;
      if(!response.ok)throw Error(response.data.message||'Не удалось обновить список.');
      setData(response.data);setState('ready');return true;
    }catch{
      if(serial===sequence.current&&live()){
        setState(afterSave?'saved_stale':'error');
        setMessage(afterSave?'Действие сохранено. Актуальный список пока не получен; обновите его перед следующим действием.':'Список не обновлён. Показаны прежние данные; повторите обновление.');
      }
      return false;
    }
  }
  function choose(next:Choice){
    selectionButton.current=document.activeElement as HTMLElement;
    setChoice(next);setQuestion(next.booking?.question||'');setAck(false);setShareRequest(Boolean(next.booking?.request));
    setVenue(next.slot.venue);setMeetingUrl(next.slot.meeting_url);setFieldError('');setMessage('');
  }
  function dismissChoice(){
    setChoice(null);setQuestion('');setShareRequest(false);setAck(false);setFieldError('');
    const live=contextFence();requestAnimationFrame(()=>{if(live()&&selectionButton.current?.isConnected)selectionButton.current.focus();});
  }
  async function send(fields:Fields,success:string,retry=false){
    if(pending||!retry&&(uncertain||stale))return;
    const current=retry?attempt.current:{fields:{...fields,operation:crypto.randomUUID()},success};
    if(!current)return;
    attempt.current=current;setState('saving');setMessage('');setFieldError('');
    const live=contextFence();
    try{
      const response=await api<Result>(data.endpoint||'/consultations/actions/',{method:'POST',body:body(current.fields)});
      if(!live())return;
      if(!response.ok){
        attempt.current=null;setState('error');setFieldError(response.data.field||'');
        setMessage(response.data.message||response.data.error||'Действие не выполнено. Проверьте данные и актуальность времени.');
        requestAnimationFrame(()=>{if(live())messageRef.current?.focus();});return;
      }
      attempt.current=null;setDirty(owner.current,false);setQuestion('');setChoice(null);setMoving(null);setAck(false);setShareRequest(false);
      if(current.fields.action==='create')setCreate(old=>({...old,topic:'',starts_at:'',ends_at:'',venue:'',meeting_url:''}));
      setMessage(current.success);setState('saved');
      const updated=await refresh({rebook:'',...(response.data.slot_id?{slot:response.data.slot_id}:{})},true,response.data.booking_id?`/consultations/bookings/${response.data.booking_id}/state/`:undefined);
      if(updated&&live()){
        setMessage(current.success);setState('saved');
        requestAnimationFrame(()=>{if(live())messageRef.current?.focus();});
      }
    }catch{
      if(live()){
        setState('uncertain');setMessage('Ответ сервера не получен. Действие могло сохраниться. Повтор использует прежние данные и ключ, чтобы не создавать вторую запись.');
        requestAnimationFrame(()=>{if(live())messageRef.current?.focus();});
      }
    }
  }
  function confirmChoice(){
    if(!choice)return;
    const fields:Fields={action:choice.action,slot:choice.slot.id,version:choice.booking?.version||choice.slot.version};
    if(choice.booking)fields.booking=choice.booking.id;
    if(choice.action==='book'||choice.action==='rebook'){
      fields.question=question;fields.academic_ack=ack?'1':'0';
      const related=choice.booking?.request||data.request_context;
      if(shareRequest&&choice.slot.kind==='staff'&&related)fields.request=related.id;
      if(choice.action==='rebook')fields.slot_version=choice.slot.version;
    }
    if(choice.action==='venue'){fields.venue=venue;fields.meeting_url=meetingUrl;}
    const messages={book:'Запись сохранена. Время, ведущий и место указаны в «Моих записях».',rebook:'Перенос сохранён. Прежняя запись осталась в истории, новая подтверждена.',cancel:'Запись отменена. История консультации сохранена.',close:'Время закрыто для новых записей.',cancel_slot:'Консультация отменена. Участнику сохранено уведомление.',venue:'Место встречи обновлено. Участнику сохранено уведомление.',complete:'Ведущий отметил консультацию проведённой. Эта отметка сохранена в истории.'};
    void send(fields,messages[choice.action]);
  }
  async function beginRebook(booking:Booking){
    setMoving(booking);setChoice(null);setMessage('Выберите новое время. Прежняя запись сохранится, если новое время занять не получится.');
    const updated=await refresh({rebook:booking.id,page:1});
    if(updated&&window.deanery.active)document.getElementById('consultation-available')?.focus();
  }
  const related=choice?.booking?.request||data.request_context;
  const choiceTitle=choice?.action==='book'?'Подтвердить запись':choice?.action==='rebook'?'Подтвердить перенос':choice?.action==='venue'?'Уточнить место встречи':choice?.action==='complete'?'Отметить консультацию проведённой':choice?.action==='close'?'Закрыть время для записи':'Подтвердить отмену';

  return <div className="consultation-workspace" aria-busy={pending}>
    <header className="planner-heading"><div><p className="eyebrow">{student?'Поддержка в учёбе':data.role==='teacher'?'Преподаватель':'Служба факультета'}</p><h1>{student?'Консультации':'Приём и консультации'}</h1><p className="support">Время по Москве · одна запись на каждое время</p></div>{!student&&<button type="button" className="button-secondary" disabled={locked} aria-expanded={showCreate} onClick={()=>setShowCreate(!showCreate)}>{showCreate?'Скрыть форму':'Предложить время'}</button>}</header>
    <nav className="consultation-navigation" aria-label="Разделы консультаций"><a href="#consultation-mine">{student?'Мои записи':'Записавшиеся'}</a><a href="#consultation-available">{student?'Доступное время':'Моё время приёма'}</a>{student?<><a href="/journeys/">Решить вопрос</a><a href="/requests/">Мои заявки</a><a href="/schedule/">Расписание</a></>:<a href={data.role==='teacher'?'/teacher/courses/':'/staff/'}>{data.role==='teacher'?'Мои дисциплины':'Рабочее место службы'}</a>}</nav>
    <p className="support">Встречи проходят внутри конкурсного прототипа на вымышленных данных.</p>
    {data.request_context&&<p className="support">Вы пришли из <a href={data.request_context.url}>заявки {data.request_context.reference}</a>. Связь с заявкой можно выбрать перед подтверждением встречи со службой.</p>}
    {student&&data.source_message&&<p className="support">{data.source_message}</p>}

    {message&&<div ref={messageRef} tabIndex={-1} role={state==='error'||uncertain?'alert':'status'} className={(state==='saved'?'state-success':state==='error'||uncertain?'save-recovery':'data-state')+' consultation-message'}><p>{message}</p>{uncertain&&<button type="button" disabled={pending} onClick={()=>send({},'',true)}>Повторить то же действие</button>}{(stale||state==='error')&&<button type="button" className="button-secondary" disabled={pending} onClick={()=>refresh({},stale)}>Обновить сохранённый список</button>}</div>}

    {showCreate&&!student&&<section className="surface consultation-section" aria-labelledby="consultation-create-title"><h2 id="consultation-create-title">Предложить время консультации</h2>{!data.audiences.length?<p className="data-state">Нет действующей аудитории для создания времени приёма.</p>:<form className="consultation-form" onSubmit={e=>{e.preventDefault();void send({action:'create',...create},'Время консультации сохранено и доступно разрешённым студентам.');}}>
      <fieldset className="consultation-fieldset" disabled={locked}>
        <div className="field"><label htmlFor="consultation-audience">Для кого</label><select id="consultation-audience" name="audience" value={create.audience} required onChange={e=>setCreate({...create,audience:e.target.value})}>{data.audiences.map(a=><option key={`${a.kind}-${a.id}`} value={a.id}>{a.label}</option>)}</select></div>
        <div className="field"><label htmlFor="consultation-topic">Тема встречи</label><input id="consultation-topic" name="topic" required maxLength={160} autoComplete="off" value={create.topic} aria-invalid={fieldError==='topic'} onChange={e=>setCreate({...create,topic:e.target.value})}/></div>
        <div className="form-grid"><div className="field"><label htmlFor="consultation-start">Начало, МСК</label><input id="consultation-start" name="starts_at" type="datetime-local" required value={create.starts_at} aria-invalid={fieldError==='starts_at'} onChange={e=>setCreate({...create,starts_at:e.target.value})}/></div><div className="field"><label htmlFor="consultation-end">Окончание, МСК</label><input id="consultation-end" name="ends_at" type="datetime-local" required value={create.ends_at} aria-invalid={fieldError==='ends_at'} onChange={e=>setCreate({...create,ends_at:e.target.value})}/></div></div>
        <div className="field"><label htmlFor="consultation-venue">Место встречи</label><input id="consultation-venue" name="venue" maxLength={240} autoComplete="off" value={create.venue} aria-invalid={fieldError==='venue'} onChange={e=>setCreate({...create,venue:e.target.value})}/></div>
        <details><summary>Встреча онлайн</summary><div className="field"><label htmlFor="consultation-meeting-url">Подготовленная HTTPS-ссылка</label><input id="consultation-meeting-url" name="meeting_url" type="url" maxLength={1000} autoComplete="off" value={create.meeting_url} aria-invalid={fieldError==='meeting_url'} onChange={e=>setCreate({...create,meeting_url:e.target.value})}/><p className="support">Укажите место или подготовленную ссылку. Кабинет не создаёт видеовстречу автоматически.</p></div></details>
        <p className="support">От 5 минут до 4 часов, в пределах ближайших 180 дней и срока вашего назначения. После записи участника время, ведущий и аудитория сохраняются. Место можно уточнить с уведомлением участника.</p><div><button type="submit">Сохранить время</button></div>
      </fieldset>
    </form>}</section>}

    {choice&&<section className="consultation-confirmation" aria-labelledby="consultation-confirm-title"><h2 ref={confirmation} tabIndex={-1} id="consultation-confirm-title">{choiceTitle}</h2><h3>{choice.slot.topic}</h3><SlotFacts slot={choice.slot}/>
      {choice.action==='rebook'&&choice.booking&&<p className="data-state">Перенос с {period(choice.booking.slot)}. Прежняя запись отменится только после сохранения новой.</p>}
      <form className="consultation-form" onSubmit={e=>{e.preventDefault();confirmChoice();}}><fieldset className="consultation-fieldset" disabled={locked}>
        {(choice.action==='book'||choice.action==='rebook')&&<><ConflictNote conflict={choice.slot.conflict}/><div className="field"><label htmlFor="consultation-question">Вопрос ведущему, необязательно</label><textarea id="consultation-question" maxLength={1000} rows={4} value={question} aria-invalid={fieldError==='question'} onChange={e=>setQuestion(e.target.value)}/><p className="support">До 1000 символов. Вопрос будет виден ведущему этой встречи.</p></div>
          {related&&choice.slot.kind==='staff'&&<label className="check-option"><input type="checkbox" checked={shareRequest} onChange={e=>setShareRequest(e.target.checked)}/>Связать с заявкой {related.reference}. Ведущий увидит только заявку, доступную его факультетской службе.</label>}
          {choice.slot.conflict?.academic&&<label className="check-option"><input type="checkbox" required checked={ack} onChange={e=>setAck(e.target.checked)}/>Вижу пересечение с учебным событием. Осознанно выбираю это время; учебное событие остаётся в расписании.</label>}
          <p className="form-note">Подтверждение появится после сохранения записи. Запись на встречу не означает её посещение или завершение подготовки.</p></>}
        {choice.action==='venue'&&<><div className="field"><label htmlFor="consultation-venue-edit">Новое место встречи</label><input id="consultation-venue-edit" maxLength={240} value={venue} onChange={e=>setVenue(e.target.value)}/></div><div className="field"><label htmlFor="consultation-link-edit">Подготовленная HTTPS-ссылка, необязательно</label><input id="consultation-link-edit" type="url" maxLength={1000} value={meetingUrl} onChange={e=>setMeetingUrl(e.target.value)}/></div><p className="form-note">Время встречи сохранится. Участнику будет доступно уведомление об уточнении места.</p></>}
        {choice.action==='complete'&&<p className="form-note">Сохраните эту отметку только после проведённой консультации. Она отражает отметку ведущего; заявка и учебные результаты изменяются отдельно.</p>}
        {(choice.action==='cancel'||choice.action==='cancel_slot')&&<p className="form-note">Участнику и ведущему сохранится история отмены. Повторная запись будет новым действием.</p>}
        {choice.action==='close'&&<p className="form-note">Это время перестанет быть доступным для новых записей.</p>}
        <div className="actions"><button type="submit" disabled={choice.slot.conflict?.blocking&&(choice.action==='book'||choice.action==='rebook')||Boolean(choice.slot.conflict?.academic&&!ack)&&(choice.action==='book'||choice.action==='rebook')}>{choice.action==='book'?'Подтвердить запись':choice.action==='rebook'?'Сохранить перенос':choice.action==='venue'?'Сохранить место':choice.action==='complete'?'Отметить проведённой':choice.action==='close'?'Закрыть время':'Отменить запись'}</button><button type="button" className="button-secondary" onClick={dismissChoice}>Вернуться без изменений</button></div>
      </fieldset></form>
    </section>}

    <section className="consultation-section" aria-labelledby="consultation-mine"><div className="section-rule"><h2 id="consultation-mine" tabIndex={-1}>{student?'Мои записи':'Записавшиеся'}</h2><button type="button" className="button-quiet" disabled={locked} onClick={()=>refresh()}>Обновить</button></div>
      {data.bookings.length?<ul className="consultation-list">{data.bookings.map(booking=><li key={booking.id} id={`consultation-booking-${booking.id}`} className="consultation-row" data-selected={data.selected_booking===booking.id||moving?.id===booking.id}><div className="consultation-row-heading"><div><p className="eyebrow">{kindLabel(booking.slot.kind)}</p><h3>{booking.slot.topic}</h3></div><span className="tag">{booking.state_label}</span></div><SlotFacts slot={booking.slot}/>
        {booking.student&&<p><strong>Участник: </strong>{booking.student}</p>}{booking.question&&<p className="consultation-question"><strong>Вопрос: </strong>{booking.question}</p>}{booking.request&&<a className="text-link" href={booking.request.url}>Заявка {booking.request.reference}</a>}{booking.audience_message&&<p className="data-state">{booking.audience_message}</p>}
        {student&&<ConflictNote conflict={booking.slot.conflict}/>}
        <div className="actions">{booking.can_rebook&&<button type="button" className="button-secondary" disabled={locked} onClick={()=>beginRebook(booking)}>Перенести запись</button>}{booking.can_cancel&&<button type="button" className="button-quiet" disabled={locked} onClick={()=>choose({action:'cancel',booking,slot:booking.slot})}>Отменить запись</button>}{booking.can_complete&&<button type="button" className="button-secondary" disabled={locked} onClick={()=>choose({action:'complete',booking,slot:booking.slot})}>Отметить консультацию проведённой</button>}{!student&&booking.slot.can_venue&&<button type="button" className="button-secondary" disabled={locked} onClick={()=>choose({action:'venue',slot:booking.slot})}>Уточнить место</button>}</div>
        <details><summary>История записи</summary><ol className="consultation-history">{booking.history.map((event,i)=><li key={i}><p>{event.label}</p><p className="support">{instant(event.at)} МСК</p></li>)}</ol><p className="support">Просмотр и наступление времени встречи не подтверждают посещение.</p></details>
      </li>)}</ul>:<div className="consultation-empty"><p>{student?'У вас пока нет записей на консультацию.':'На ваше время пока никто не записался.'}</p><p className="support">{student?'Выберите подходящее доступное время ниже.':'После сохранённой записи здесь появятся участник и его вопрос.'}</p></div>}
      <Pager page={data.booking_page||1} pages={data.booking_pages||1} label="Страницы записей" disabled={locked} onPage={page=>refresh({booking_page:page})}/>
    </section>

    <section className="consultation-section" aria-labelledby="consultation-available"><h2 id="consultation-available" tabIndex={-1}>{student?'Доступное время':'Моё время приёма'}</h2>
      {moving&&<div className="data-state"><p>Перенос записи «{moving.slot.topic}» с {period(moving.slot)}. Выберите новое время.</p><button type="button" className="button-secondary" disabled={locked} onClick={()=>{setMoving(null);setChoice(null);setMessage('Перенос не выполнен. Прежняя запись сохранена.');void refresh({rebook:''});}}>Оставить прежнюю запись</button></div>}
      {data.slots.length?<ul className="consultation-list">{data.slots.map(slot=><li key={slot.id} id={`consultation-slot-${slot.id}`} className="consultation-row" data-selected={data.selected_slot===slot.id}><div className="consultation-row-heading"><div><p className="eyebrow">{kindLabel(slot.kind)}</p><h3>{slot.topic}</h3></div>{!student&&<span className="tag">{slot.state_label}</span>}</div><SlotFacts slot={slot}/>{student&&<ConflictNote conflict={slot.conflict}/>}
        <div className="actions">{student&&slot.available&&<button type="button" disabled={locked||slot.conflict?.blocking||moving?.slot.id===slot.id} onClick={()=>choose({action:moving?'rebook':'book',slot,booking:moving||undefined})}>{moving?'Перенести на это время':'Выбрать время'}</button>}{!student&&slot.can_close&&<button type="button" className="button-quiet" disabled={locked} onClick={()=>choose({action:'close',slot})}>Закрыть время</button>}{!student&&slot.can_cancel&&<button type="button" className="button-quiet" disabled={locked} onClick={()=>choose({action:'cancel_slot',slot})}>Отменить консультацию</button>}{!student&&slot.can_venue&&<button type="button" className="button-secondary" disabled={locked} onClick={()=>choose({action:'venue',slot})}>Уточнить место</button>}</div>
      </li>)}</ul>:<div className="consultation-empty"><p>{student?'Подходящего доступного времени сейчас нет.':'Вы ещё не предложили время консультации.'}</p><p className="support">{student?'Можно вернуться позднее или продолжить обращение через заявки. Это не означает, что вопрос рассмотрен.':'Нажмите «Предложить время», чтобы выбрать дату и аудиторию.'}</p></div>}
      <Pager page={data.page} pages={data.pages} label="Страницы времени консультаций" disabled={locked} onPage={page=>refresh({page})}/>
    </section>
  </div>;
}
