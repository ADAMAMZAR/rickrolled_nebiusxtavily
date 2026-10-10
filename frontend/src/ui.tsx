import type { ReactNode } from "react";

// Authored line icons: 24px grid, one 1.8 stroke, drawn in currentColor.
const PATHS: Record<string, ReactNode> = {
  ledger: (<><rect x="5" y="3" width="14" height="18" rx="1.5" /><path d="M8.5 3v18M11.5 8.5h4.5M11.5 12h4.5M11 15.6c1.6-.5 3.4.4 5.6-.3" /></>),
  task: (<><circle cx="6.5" cy="10.5" r="1.5" fill="currentColor" stroke="none" /><path d="M10 10.5h9.5" /><path d="M4 15.5h16" strokeOpacity={0.45} /></>),
  ask: (<><circle cx="12" cy="12" r="8.5" /><path d="M9.7 9.6a2.4 2.4 0 1 1 3.3 2.2c-.6.3-1 .8-1 1.4v.5" /><circle cx="12" cy="16.6" r=".9" fill="currentColor" stroke="none" /></>),
  waiting: (<><circle cx="12" cy="12" r="8" /><path d="M12 7.5V12l3 2" /></>),
  commitment: <path d="M7 4h10v16l-5-3.6L7 20z" />,
  done: (<><rect x="5" y="5" width="14" height="14" rx="2" /><path d="M8.6 12.3l2.3 2.3 4.5-4.9" /></>),
  risk: (<><circle cx="12" cy="12" r="8.5" /><path d="M12 7.6v5.2" /><circle cx="12" cy="16.3" r=".9" fill="currentColor" stroke="none" /></>),
  send: <path d="M5 12h13M13 6l6 6-6 6" />,
  search: (<><circle cx="11" cy="11" r="6" /><path d="M15.5 15.5L20 20" /></>),
  gear: (<><circle cx="12" cy="12" r="3" /><path d="M12 3v2.6M12 18.4V21M3 12h2.6M18.4 12H21M5.6 5.6l1.9 1.9M16.5 16.5l1.9 1.9M5.6 18.4l1.9-1.9M16.5 7.5l1.9-1.9" /></>),
  chevron: <path d="M9 6l6 6-6 6" />,
};

export type IconName = keyof typeof PATHS;

export function Icon({ name, label }: { name: IconName; label?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      {PATHS[name]}
    </svg>
  );
}

export function Wordmark() {
  return (
    <a className="wordmark" href="/" aria-label="Continuum, your loops">
      <Icon name="ledger" />Continuum
    </a>
  );
}

/** The red cover band at the top of every page. */
export function Band({ children }: { children: ReactNode }) {
  return (
    <header className="band">
      <div className="band-inner">{children}</div>
    </header>
  );
}

/** The signature move: a ballpoint stroke through a settled entry. `draw` animates it once. */
export function Strike({ draw = false }: { draw?: boolean }) {
  return (
    <svg className={`strike${draw ? " draw" : ""}`} viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden>
      <path pathLength={1} vectorEffect="non-scaling-stroke" d="M1 6.2C18 3.6 36 7.4 55 4.8S86 5.6 99 3.4" />
    </svg>
  );
}

const URL = /(https?:\/\/[^\s<>"]+)/g;

/** Untrusted text with its http(s) links made clickable. React escapes the rest. */
export function Linked({ text }: { text: string }) {
  return (
    <>
      {text.split(URL).map((part, i) => {
        if (i % 2 === 0) return part;
        const trail = part.match(/[.,;:!?)\]]+$/)?.[0] ?? "";
        const url = trail ? part.slice(0, -trail.length) : part;
        return (<span key={i}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a>{trail}</span>);
      })}
    </>
  );
}
