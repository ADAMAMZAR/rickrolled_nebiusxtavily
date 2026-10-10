import { useState } from "react";
import { safeHref, type Attention as Item, type PendingAction } from "../api";
import { Icon } from "../ui";
import { SnoozeMenu } from "./menus";

export const KIND = { task: "Task", waiting: "Waiting on someone", commitment: "Your promise" } as const;
const risky = (reason: string) => reason.endsWith("hold off paying");

interface Props {
  items: Item[] | null;
  error: string | null;
  actions: PendingAction[];
  actionsError: string | null;
  act: (path: string, method: string, body?: unknown) => Promise<void>;
  draft: (loopId: string, title: string) => void;
}

/** What needs the user now: Continuum's flags in the red margin, then proposals waiting for a yes. */
export function Attention({ items, error, actions, actionsError, act, draft }: Props) {
  return (
    <section className="section sheet attention" aria-labelledby="attention-h">
      <div className="head">
        <h2 id="attention-h">Needs attention</h2>
        {items && <span className="count">{items.length}</span>}
      </div>
      {error && <p className="error-line pad">Couldn't load what needs attention: {error}</p>}
      {items && items.length === 0 && <p className="empty quiet">Nothing needs you today.</p>}
      {items && items.length > 0 && (
        <ul className="rows margined">
          {items.map(({ loop: l, reason }) => (
            <li key={l.id}>
              <div className="entry">
                <span className={risky(reason) ? "flag" : "kind"} title={risky(reason) ? "Risk signals" : KIND[l.kind]}>
                  <Icon name={risky(reason) ? "risk" : l.kind} label={risky(reason) ? "Risk signals" : KIND[l.kind]} />
                </span>
                <span className="title">
                  <b>{l.title}</b>
                  <span className="meta">
                    {l.goal_title && <>{l.goal_title} · </>}
                    <span className={reason.startsWith("overdue") || risky(reason) ? "late" : undefined}>{reason}</span>
                  </span>
                </span>
                <span className="side">
                  {!risky(reason) && <button className="btn small" type="button" onClick={() => draft(l.id, l.title)}>Draft</button>}
                  <SnoozeMenu small onPick={(until) => act(`/api/loops/${l.id}/snooze`, "POST", { until })} />
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {(actions.length > 0 || actionsError) && (
        <>
          <h3 className="subhead">Waiting for your yes <span className="count num">{actions.length}</span></h3>
          {actionsError && <p className="error-line">Couldn't load pending actions: {actionsError}</p>}
          <ul className="rows">{actions.map((a) => <Proposal key={a.id} action={a} act={act} />)}</ul>
        </>
      )}
    </section>
  );
}

function Proposal({ action, act }: { action: PendingAction; act: Props["act"] }) {
  const [deciding, setDeciding] = useState(false); // one click only
  const decide = (verb: "approve" | "reject") => { setDeciding(true); act(`/api/actions/${action.id}/${verb}`, "POST"); };
  const href = safeHref(action.payload.source_url);
  return (
    <li>
      <div className="entry">
        <span className="kind"><Icon name="done" /></span>
        <span className="title">
          <b>{action.summary}</b>
          {href && <span className="meta"><a href={href} target="_blank" rel="noopener noreferrer">Open the page</a></span>}
        </span>
        <span className="side">
          <button className="btn small primary" type="button" disabled={deciding} onClick={() => decide("approve")}>Approve</button>
          <button className="btn small" type="button" disabled={deciding} onClick={() => decide("reject")}>Reject</button>
        </span>
      </div>
    </li>
  );
}
