export type PersonValue = {name:string; avatar_url:string};
export function Person({person}: {person:PersonValue}) {
  return <span className="person">{person.avatar_url ? <img className="person-avatar" src={person.avatar_url} alt=""/> :
    <span className="person-fallback" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/></svg></span>}<span>{person.name}</span></span>;
}
