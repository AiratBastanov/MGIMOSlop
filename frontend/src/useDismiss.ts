import {useEffect} from 'react';
import type {RefObject} from 'react';

export type DismissReason = 'outside' | 'escape' | 'privacy';
/** Light dismiss never cancels the outside action or steals its focus. */
export function useDismiss(open:boolean, refs:RefObject<HTMLElement|null>[], dismiss:(reason:DismissReason)=>void) {
  useEffect(()=>{
    if(!open)return;
    const outside=(event:PointerEvent)=>{if(!refs.some(ref=>ref.current?.contains(event.target as Node)))dismiss('outside');};
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!event.defaultPrevented){event.preventDefault();dismiss('escape');}};
    const privacy=()=>dismiss('privacy');
    document.addEventListener('pointerdown',outside,true);
    document.addEventListener('keydown',escape);
    document.addEventListener('private:conceal',privacy);
    return()=>{document.removeEventListener('pointerdown',outside,true);document.removeEventListener('keydown',escape);document.removeEventListener('private:conceal',privacy);};
  },[open, ...refs, dismiss]);
}
