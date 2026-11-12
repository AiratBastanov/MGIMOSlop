import {useRef,useState} from 'react';
import {contextFence} from './lifecycle';

/** Values and protocol targets are the server's current authorized projection. */
export function Contact({value,href,label}:{value:string;href:string;label:string}) {
  const [feedback,setFeedback]=useState('');
  const serial=useRef(0);
  async function copy() {
    if(!window.deanery.active)return;
    const current=contextFence(), id=++serial.current;
    setFeedback('');
    try {
      if(!navigator.clipboard?.writeText)throw new Error('unavailable');
      await navigator.clipboard.writeText(value);
      if(current()&&id===serial.current)setFeedback('Скопировано');
    } catch {
      if(current()&&id===serial.current)setFeedback('Не удалось скопировать. Выделите контакт и скопируйте вручную.');
    }
  }
  return <span className="contact-value"><span className="contact-actions"><a href={href} aria-label={`${label}: ${value}`}>{value}</a><button type="button" className="contact-copy button-quiet" onClick={()=>void copy()} aria-label={`Копировать: ${value}`}>Копировать</button></span><span className="contact-feedback" role="status" aria-live="polite">{feedback}</span></span>;
}
