import {useEffect,useRef,useState} from 'react';
import {api,body,setDirty} from './lifecycle';
import {Person} from './Person';
import type {PersonValue} from './Person';
type Field={name:string;label:string;type:string;value:string|number;required:boolean;max_length?:number;min_value?:number;max_value?:number;choices?:[string,string][];errors:string[]};
type RequestData={marker:string;token:string;selected:string;types:{value:string;label:string;fields:Field[]}[];person:PersonValue;group:string;faculty:string;programme:string;year:number;conflict:string;errors:string[]};
export function RequestForm({initial}:{initial:RequestData}){
  const [data,setData]=useState(initial),[kind,setKind]=useState(initial.selected),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[uncertain,setUncertain]=useState(false);
  const [values,setValues]=useState<Record<string,Record<string,string>>>(()=>Object.fromEntries(initial.types.map(t=>[t.value,Object.fromEntries(t.fields.map(f=>[f.name,String(f.value)]))])));
  const owner=useRef({}),attempt=useRef<Record<string,string>|null>(null);
  const [quantityError,setQuantityError]=useState('');
  const selected=data.types.find(t=>t.value===kind)!;
  function change(name:string,value:string){setValues(v=>({...v,[kind]:{...v[kind],[name]:value}}));setDirty(owner.current,true);}
  function choose(value:string){setKind(value);setMessage('');history.pushState(null,'',`/requests/new/?type=${value}`);}
  useEffect(()=>{const back=()=>{const value=new URL(location.href).searchParams.get('type')||initial.selected;if(initial.types.some(t=>t.value===value))setKind(value);};addEventListener('popstate',back);return()=>{removeEventListener('popstate',back);setDirty(owner.current,false);attempt.current=null;};},[]);
  async function submit(retry=false){
    if(busy)return;const fields=retry?attempt.current:{...values[kind],request_type:kind,submission_token:data.token};if(!fields)return;
    if('copies' in fields&&!/^(?:[1-9]|10)$/.test(fields.copies)){setQuantityError('Введите целое число от 1 до 10.');document.getElementById('request-copies')?.focus();return;}
    attempt.current=fields;setBusy(true);setMessage('');setUncertain(false);
    try{const result=await api<RequestData&{state:string;redirect:string}>('/requests/new/',{method:'POST',body:body(fields),headers:{'X-Request-Form':'1'}});
      if(result.ok&&result.data.state==='saved'){setDirty(owner.current,false);attempt.current=null;location.assign(result.data.redirect);return;}
      if(result.status===400||result.status===409){setData(result.data);setMessage(result.data.conflict||'Проверьте отмеченные поля. Введённые данные сохранены в форме.');attempt.current=null;}
      else throw Error();
    }catch{setUncertain(true);setMessage('Ответ не получен. Заявка могла сохраниться. Повтор этой отправки использует прежний ключ и не создаст дубликат.');}
    finally{setBusy(false);}
  }
  function field(f:Field){const id='request-'+f.name;const attrs={id,name:f.name,value:values[kind][f.name],required:f.required,maxLength:f.max_length,min:f.min_value,max:f.max_value,'aria-invalid':Boolean(f.errors.length),'aria-describedby':f.errors.length?id+'-error':undefined,onChange:(e:{target:{value:string}})=>change(f.name,e.target.value)};
    const raw=values[kind][f.name], integer=/^(?:[1-9]|10)$/.test(raw);
    const editQuantity=(value:string)=>{if(value===''||/^(?:[1-9]|10)$/.test(value)){change(f.name,value);setQuantityError('');}else setQuantityError('Допустимы только целые числа от 1 до 10. Изменение отклонено.');};
    const step=(n:number)=>{setValues(v=>{const old=v[kind][f.name],next=Number(old)+n;if(!/^(?:[1-9]|10)$/.test(old)||next<1||next>10)return v;return {...v,[kind]:{...v[kind],[f.name]:String(next)}};});setDirty(owner.current,true);setQuantityError('');};
    return <div className="field" key={f.name}><label htmlFor={id}>{f.label}</label>{f.name==='copies'?<><div className="quantity-control"><button type="button" className="button-secondary" aria-label="Уменьшить количество" disabled={!integer||Number(raw)<=1} onClick={()=>step(-1)}>−</button><input {...attrs} type="text" inputMode="numeric" autoComplete="off" onChange={e=>editQuantity(e.target.value)} aria-invalid={Boolean(quantityError||f.errors.length)} aria-describedby={id+'-hint '+id+'-edit-error'+(f.errors.length?' '+id+'-error':'')} onKeyDown={e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();step(e.key==='ArrowDown'?-1:1);}}}/><button type="button" className="button-secondary" aria-label="Увеличить количество" disabled={!integer||Number(raw)>=10} onClick={()=>step(1)}>+</button></div><p id={id+'-hint'} className="support">От 1 до 10 экземпляров. Можно ввести число или изменить его на один.</p><p id={id+'-edit-error'} className="field-error" role="status">{quantityError}</p></>:f.type==='select'?<select {...attrs}>{f.choices?.map(([v,l])=><option key={v} value={v}>{l}</option>)}</select>:f.type==='textarea'?<textarea {...attrs} rows={f.name==='comment'?3:4}/>:<input {...attrs} type={f.type} autoComplete="off"/>}{f.name==='recipient'&&<p className="support">Укажите, куда предъявите справку.</p>}{f.errors.length>0&&<div id={id+'-error'} className="field-error">{f.errors.map(e=><p key={e}>{e}</p>)}</div>}</div>;
  }
  return <div className="request-composer surface"><form onSubmit={e=>{e.preventDefault();void submit();}} noValidate>
    <fieldset className="editor-fields" disabled={busy||uncertain}><div className="field"><label htmlFor="request-kind">Что вам нужно</label><select id="request-kind" value={kind} onChange={e=>choose(e.target.value)}>{data.types.map(t=><option key={t.value} value={t.value}>{t.label}</option>)}</select></div>
      <details className="applicant-summary"><summary>Заявитель · {data.person.name}</summary><Person person={data.person}/><p className="support">{data.group} · {data.year} курс<br/>{data.faculty}<br/>{data.programme}</p></details>
      {selected.fields.filter(f=>f.name!=='comment').map(field)}
      <div className="request-comment">{selected.fields.filter(f=>f.name==='comment').map(field)}<p className="support">Необязательно · до 500 символов.</p></div>
    </fieldset>
    {message&&<div role="alert" className="field-error">{message}</div>}{data.errors.map(e=><p className="field-error" key={e}>{e}</p>)}
    <p className="form-note">{kind==='academic_correction'?'Сотрудник проверит обращение и сохранит ответ. Учебные сведения изменяются только проверенным импортом.':'Справка демонстрационная, на вымышленных данных.'}</p>
    {!data.conflict&&!uncertain&&<button type="submit" disabled={busy}>{busy?'Отправляем…':'Отправить заявку'}</button>}
    {uncertain&&<div className="actions"><button type="button" disabled={busy} onClick={()=>submit(true)}>Повторить эту отправку</button><a href="/requests/">Проверить мои заявки</a></div>}
  </form></div>;
}
