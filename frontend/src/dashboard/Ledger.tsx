import { useEffect, useState, type FormEvent } from "react";
import { api, startCheck, type Activity, type ActivityBy, type Loop, type LoopDetail } from "../api";
import { dueInfo, isSnoozed, localDate, shortDate, stamp } from "../dates";
import { Icon, Strike } from "../ui";
import { KIND } from "./Attention";
import { SnoozeMenu } from "./menus";

const BY: Record<ActivityBy, string> = { user: "you", chat: "chat", email: "email", web: "the web", check: "a message check" };
// Loops about paying someone get a one-click check of the message they came from.
// ponytail: a keyword test; widen it if real payment loops slip past.
const MONEY = /https?:\/\/|www\.|\bRM ?\d|\$ ?\d|\b(pay|paid|payment|transfer|deposit|bank|account|invest\w*|fee|OTP|TAC)\b/i;
// Same wording as engine.risk_reason, which Needs attention shows.
const riskReason = (level: string) =>
  level === "INSUFFICIENT_EVIDENCE" ? "sender not verified, hold off paying" : `${level.toLowerCase()} risk, hold off paying`;
const EXAMPLE = "I'm applying for an NVIDIA internship. I submitted my application yesterday. Sarah said she'll get back to me next Friday. I also need to finish my portfolio before the interview.";

export type Act = (path: string, method: string, body?: unknown) => Promise<void>;

interface Props {
  loops: Loop[] | null;
  error: string | null;
  fresh: Set<string>;
  version: number;
  webEnabled: boolean;
  showSnoozed: boolean;
  showResolved: boolean;
  setShowSnoozed: (v: boolean) => void;
  setShowResolved: (v: boolean) => void;
  act: Act;
  refresh: () => Promise<void>;
  useExample: (text: string) => void;
}

/** The ledger's two sides: what others owe you, and what you owe. */
export function Ledger(p: Props) {
  const owed = p.loops?.filter((l) => l.kind === "waiting") ?? [];
  const owe = p.loops?.filter((l) => l.kind !== "waiting") ?? [];
  const goalOpen = (goalId: string) => p.loops?.filter((l) => l.goal_id === goalId && l.status === "open").length ?? 0;
  return (
    <section className="section sheet ledger" aria-labelledby="ledger-h">
      <div className="head">
        <h2 id="ledger-h">Open loops</h2>
        <span className="tools">
          <label className="check"><input type="checkbox" checked={p.showSnoozed} onChange={(e) => p.setShowSnoozed(e.target.checked)} /> Show snoozed</label>
          <label className="check"><input type="checkbox" checked={p.showResolved} onChange={(e) => p.setShowResolved(e.target.checked)} /> Show resolved</label>
        </span>
      </div>
      {p.error && <div className="empty"><h3>Couldn't load your loops</h3><p>{p.error}</p></div>}
      {p.loops && p.loops.length === 0 && (
        <div className="empty">
          <h3>Nothing open yet</h3>
          <p>Tell Continuum about a goal, a deadline, or something someone owes you. It turns that into loops it keeps track of.</p>
          <blockquote className="ruled">{EXAMPLE}</blockquote>
          <button className="btn" type="button" onClick={() => p.useExample(EXAMPLE)}>Use this example</button>
        </div>
      )}
      {p.loops === null && !p.error && <p className="empty" aria-busy="true">Loading your loops…</p>}
      {p.loops && p.loops.length > 0 && (
        <div className="sides">
          <Side {...p} title="Owed to you" empty="Nobody owes you anything right now." loops={owed} goalOpen={goalOpen} />
          <Side {...p} title="You owe" empty="Nothing on your side right now." loops={owe} goalOpen={goalOpen} />
        </div>
      )}
    </section>
  );
}

function Side({ title, empty, loops, goalOpen, ...p }: Props & { title: string; empty: string; loops: Loop[]; goalOpen: (id: string) => number }) {
  const sorted = [...loops].sort((a, b) => Number(a.status === "resolved") - Number(b.status === "resolved"));
  return (
    <div className="side-col">
      <h3 className="subhead">{title} <span className="count num">{loops.filter((l) => l.status === "open").length}</span></h3>
      {sorted.length === 0 ? <p className="empty quiet">{empty}</p> : (
        <ul className="rows margined">
          {sorted.map((l) => <LoopRow key={l.id} loop={l} fresh={p.fresh.has(l.id)} version={p.version} webEnabled={p.webEnabled} act={p.act} refresh={p.refresh} goalOpen={goalOpen} />)}
        </ul>
      )}
    </div>
  );
}

interface RowProps {
  loop: Loop; fresh: boolean; version: number; webEnabled: boolean; act: Act; refresh: () => Promise<void>; goalOpen: (id: string) => number;
}

function LoopRow({ loop: l, fresh, version, webEnabled, act, refresh, goalOpen }: RowProps) {
  const [open, setOpen] = useState(false);
  const [settling, setSettling] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const resolved = l.status === "resolved";
  const due = l.due && !resolved ? dueInfo(l.due) : null;
  // Continuum's flag in the margin: a check found risk, or the loop asks for money and nobody checked yet.
  const risky = !resolved && !!l.risk_level && l.risk_level !== "LOW";
  const unchecked = !resolved && !l.risk_level && MONEY.test(`${l.title} ${l.summary}`);
  const warning = risky ? riskReason(l.risk_level!) : unchecked ? "asks for money, not checked yet" : null;
  const meta = [
    l.goal_title,
    l.waiting_on && `from ${l.waiting_on}`,
    resolved && (l.resolved_by === "email" ? "closed by email" : "resolved"),
    isSnoozed(l) && l.snoozed_until && `snoozed until ${shortDate(localDate(l.snoozed_until))}`,
    !resolved && l.watch_query && "watching the web",
  ].filter(Boolean).join(" · ");

  // Settle: the strike draws first, then the list refreshes (the loop leaves unless resolved ones are shown).
  async function resolve() {
    try { await api(`/api/loops/${l.id}/resolve`, { method: "POST" }); }
    catch (e) { setResolveError(`Couldn't resolve it: ${(e as Error).message}`); return; }
    setResolveError(null);
    setOpen(false);
    setSettling(true);
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    setTimeout(refresh, reduced ? 0 : 650);
  }

  return (
    <li className={`${resolved || settling ? "settled" : ""}${fresh ? " fresh" : ""}`}>
      <button className="entry loop-entry row-btn" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {warning
          ? <span className="flag" title={warning}><Icon name={risky ? "risk" : "ask"} label={risky ? "Risk signals" : "Not checked yet"} /></span>
          : <span className="kind" title={resolved ? "Resolved" : KIND[l.kind]}><Icon name={resolved ? "done" : l.kind} label={resolved ? "Resolved" : KIND[l.kind]} /></span>}
        <span className="title">
          <b>{l.title}</b>
          {(resolved || settling) && <Strike draw={settling} />}
          {(warning || meta) && (
            <span className="meta">{warning && <span className="warn-text">{warning}</span>}{warning && meta && " · "}{meta}</span>
          )}
        </span>
        {due && <span className={`due${due.late ? " late" : due.soon ? " soon" : ""}`}>{due.text}</span>}
        <span className={`chev${open ? " open" : ""}`}><Icon name="chevron" /></span>
      </button>
      {open && <Detail id={l.id} version={version} webEnabled={webEnabled} act={act} resolve={resolve} resolveError={resolveError} goalOpen={goalOpen} />}
    </li>
  );
}

interface DetailProps {
  id: string; version: number; webEnabled: boolean; act: Act; resolve: () => void; resolveError: string | null; goalOpen: (id: string) => number;
}

function Detail({ id, version, webEnabled, act, resolve, resolveError, goalOpen }: DetailProps) {
  const [d, setD] = useState<LoopDetail | null>(null);
  const [history, setHistory] = useState<Activity[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api<LoopDetail>(`/api/loops/${id}`), api<Activity[]>(`/api/activity?loop_id=${id}`)])
      .then(([detail, acts]) => { setD(detail); setHistory(acts); setError(null); })
      .catch((e) => setError(e.message));
  }, [id, version]);

  if (error) return <div className="detail"><p className="error-line">{error}</p></div>;
  if (!d) return <div className="detail" aria-busy="true"><p className="muted">Loading…</p></div>;

  const resolved = d.status === "resolved";
  const said = new Date(d.source.created_at);
  const fields: [string, string | null | false][] = [
    ["Kind", KIND[d.kind]],
    ["Waiting on", d.waiting_on],
    ["Due", d.due && shortDate(localDate(d.due))],
    ["Next action", d.next_action],
    ["Summary", d.summary],
    ["Snoozed until", isSnoozed(d) && d.snoozed_until && shortDate(localDate(d.snoozed_until))],
    ["Resolved", d.resolved_at && new Date(d.resolved_at).toLocaleString()],
  ];

  async function check() { // the loop's own words go to a message check that stays linked to it
    setChecking(true);
    const form = new FormData();
    form.append("text", d!.source.text);
    form.append("loop_id", d!.id);
    try { location.href = `/investigate.html#${await startCheck(form)}`; }
    catch (e) { setCheckError(`Couldn't start the check: ${(e as Error).message}`); setChecking(false); }
  }

  function completeGoal() {
    const open = goalOpen(d!.goal_id!);
    const loops = open === 1 ? "its open loop" : `its ${open} open loops`;
    const note = open ? ` This resolves ${loops}. Reopen any of them to undo.` : "";
    if (confirm(`Mark "${d!.goal_title}" done?${note}`)) act(`/api/goals/${d!.goal_id}/complete`, "POST");
  }

  return (
    <div className="detail">
      <dl>
        {fields.filter(([, v]) => v).map(([k, v]) => (<div key={k}><dt>{k}</dt><dd>{v}</dd></div>))}
        {d.goal_title && (
          <div><dt>Goal</dt><dd>{d.goal_title}{d.goal_status === "active" && <> · <button className="link-btn" type="button" onClick={completeGoal}>Mark goal done</button></>}{d.goal_status === "done" && " · done"}</dd></div>
        )}
      </dl>
      <figure className="why">
        <h4>Why Continuum knows this</h4>
        {d.source.text ? <blockquote className="ruled">{d.source.text}</blockquote> : <p className="muted">Source deleted.</p>}
        <figcaption>
          {d.source.kind === "email" ? (
            <>Email{d.source.sender ? ` from ${d.source.sender}` : ""} · {shortDate(said)}
              {d.source.url && <> · <a href={d.source.url} target="_blank" rel="noopener noreferrer">Open in Gmail</a></>}</>
          ) : d.source.kind === "investigation" ? (
            <>A message you checked · {shortDate(said)}{d.source.url && <> · <a href={d.source.url}>See the check</a></>}</>
          ) : <>You said this {stamp(d.source.created_at)}</>}
        </figcaption>
      </figure>
      {history.length > 0 && (
        <div className="history">
          <h4>History</h4>
          <ul>
            {history.map((a) => (
              <li key={a.id}>
                <span>{stamp(a.created_at)} · {a.action.replace("_", " ")} by {BY[a.by]}
                  {a.detail && " · "}{/^https?:\/\//.test(a.detail) ? <a href={a.detail} target="_blank" rel="noopener noreferrer">source</a> : a.detail}</span>
                {a.can_undo && <button className="btn small" type="button" onClick={() => act(`/api/activity/${a.id}/undo`, "POST")}>Undo</button>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {d.person_id && <ContactForm d={d} act={act} />}
      {webEnabled && !resolved && <WatchForm d={d} act={act} />}
      <div className="actions">
        {resolved
          ? <button className="btn primary" type="button" onClick={() => act(`/api/loops/${d.id}/reopen`, "POST")}>Reopen</button>
          : <button className="btn primary" type="button" onClick={resolve}>Resolve</button>}
        {!resolved && (isSnoozed(d)
          ? <button className="btn" type="button" onClick={() => act(`/api/loops/${d.id}/unsnooze`, "POST")}>Unsnooze</button>
          : <SnoozeMenu onPick={(until) => act(`/api/loops/${d.id}/snooze`, "POST", { until })} />)}
        {!resolved && d.source.kind !== "investigation" && MONEY.test(d.source.text || "") && (
          <button className="btn" type="button" disabled={checking} onClick={check}><Icon name="search" />{checking ? "Starting the check…" : "Check who's asking"}</button>
        )}
        <button className="btn danger apart" type="button"
          onClick={() => confirm(`Delete "${d.title}"? This can't be undone.`) && act(`/api/loops/${d.id}`, "DELETE")}>Delete</button>
        {(resolveError || checkError) && <p className="error-line" role="alert">{resolveError || checkError}</p>}
      </div>
    </div>
  );
}

function ContactForm({ d, act }: { d: LoopDetail; act: Act }) {
  const [email, setEmail] = useState(d.person_email || "");
  const save = (e: FormEvent) => { e.preventDefault(); act(`/api/people/${d.person_id}`, "PATCH", { email: email.trim() || null }); };
  return (
    <form className="field" onSubmit={save}>
      <h4>Contact email</h4>
      <p className="muted">Lets Continuum notice when {d.person_name} replies. It never sends email.</p>
      <div className="row">
        <input type="email" value={email} placeholder={`${d.person_name}'s email`} aria-label={`${d.person_name}'s email`} onChange={(e) => setEmail(e.target.value)} />
        <button className="btn small" type="submit">Save</button>
      </div>
    </form>
  );
}

function WatchForm({ d, act }: { d: LoopDetail; act: Act }) {
  const [query, setQuery] = useState(d.watch_query || [d.goal_title, d.title].filter(Boolean).join(" "));
  const save = (e: FormEvent) => { e.preventDefault(); if (query.trim()) act(`/api/loops/${d.id}/watch`, "PUT", { query: query.trim() }); };
  const checked = d.watch_checked_on ? ` Last checked ${shortDate(localDate(d.watch_checked_on))}.` : "";
  return (
    <form className="field" onSubmit={save}>
      <h4>Watch the web</h4>
      <p className="muted">{d.watch_query
        ? `Searched once a day. Anything found waits for your OK.${checked}`
        : "Search the web once a day for news that closes this loop or moves its date. Anything found waits for your OK."}</p>
      <div className="row">
        <input type="text" maxLength={400} value={query} aria-label="Web search" onChange={(e) => setQuery(e.target.value)} />
        <button className="btn small" type="submit">{d.watch_query ? "Save" : "Watch"}</button>
        {d.watch_query && <button className="btn small" type="button" onClick={() => act(`/api/loops/${d.id}/watch`, "DELETE")}>Stop</button>}
      </div>
    </form>
  );
}
