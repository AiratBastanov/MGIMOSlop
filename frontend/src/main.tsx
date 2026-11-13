import 'vite/modulepreload-polyfill';
import '../../static/app.css';
import {Component} from 'react';
import type {ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import type {Root} from 'react-dom/client';
import {installLifecycle} from './lifecycle';
import {Schedule} from './Schedule';
import {Inbox} from './Inbox';
import {MaterialEditor} from './MaterialEditor';
import {RequestForm} from './RequestForm';
import {PageTools, RequestView} from './PageTools';
import {Contact} from './Contact';
import {WorkspaceEditor,ClarificationBox,ViewedEvents} from './WorkspaceEditor';
import {Search} from './Search';
import {Journey} from './Journey';
import {Consultations} from './Consultations';
import '../../static/consultations.css';
import '../../static/journeys.css';

class Boundary extends Component<{children:ReactNode},{failed:boolean}> {
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  render(){return this.state.failed?<section className="save-recovery" role="alert"><h2>Не удалось показать рабочую область</h2><p>Проверьте сохранённые действия после обновления. Несохранённый ввод будет очищен.</p><button onClick={()=>location.reload()}>Обновить страницу</button></section>:this.props.children;}
}
const roots=new Map<HTMLElement,Root>();
function unmount(){for(const [element,root] of roots){root.unmount();element.replaceChildren();}roots.clear();}
function mount(){if(!window.deanery.active)return;
  for(const element of document.querySelectorAll<HTMLElement>('[data-component]')){
    if(roots.has(element))continue;
    const props=element.dataset.props?document.getElementById(element.dataset.props):null;
    const data=props?JSON.parse(props.textContent||'{}'):null;
    if(data&&data.marker!==window.deanery.marker){window.deanery.discard('Контекст страницы изменился. Обновите кабинет.');return;}
    const root=createRoot(element);roots.set(element,root);
    root.render(<Boundary>{element.dataset.component==='search'?<Search initial={data}/>:element.dataset.component==='journey'?<Journey initial={data}/>:element.dataset.component==='consultations'?<Consultations initial={data}/>:element.dataset.component==='workspace-editor'?<WorkspaceEditor initial={data}/>:element.dataset.component==='clarification'?<ClarificationBox initial={data}/>:element.dataset.component==='viewed-events'?<ViewedEvents initial={data}/>:element.dataset.component==='contact'?<Contact value={element.dataset.value!} href={element.dataset.href!} label={element.dataset.label!}/>:element.dataset.component==='page-tools'?<PageTools/>:element.dataset.component==='request-view'?<RequestView/>:element.dataset.component==='schedule'?<Schedule initial={data}/>:element.dataset.component==='inbox'?<Inbox initial={data}/>:element.dataset.component==='request'?<RequestForm initial={data}/>:<MaterialEditor/>}</Boundary>);
  }
}
if(document.body.dataset.private&&window.deanery)installLifecycle(unmount,mount);
