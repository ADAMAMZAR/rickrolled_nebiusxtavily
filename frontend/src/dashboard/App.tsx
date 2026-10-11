import { useCallback, useEffect, useRef, useState } from "react";
import { api, send, type Attention as Item, type Loop, type PendingAction } from "../api";
import { Band, Icon, Wordmark } from "../ui";
import { Attention } from "./Attention";
import { Chat, useChat } from "./Chat";
import { Ledger } from "./Ledger";
import { Settings } from "./menus";

const failed = (e: unknown) => (e as Error).message;

export function App() {
  const [loops, setLoops] = useState<Loop[] | null>(null);
  const [loopsError, setLoopsError] = useState<string | null>(null);
  const [attention, setAttention] = useState<Item[] | null>(null);
  const [attentionError, setAttentionError] = useState<string | null>(null);
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [actionsError, setActionsError] = useState<string | null>(null);
  const [showSnoozed, setShowSnoozed] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  const [webEnabled, setWebEnabled] = useState(false);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [version, setVersion] = useState(0); // open loop details reload when it changes
  const seen = useRef(new Map<string, string>()); // loop id -> updated_at, to mark what a chat changed
  const markNext = useRef(false);

  const refresh = useCallback(async () => {
    const status = showResolved ? "all" : "open";
    await Promise.all([
      api<Loop[]>(`/api/loops?status=${status}&include_snoozed=${showSnoozed}`).then((found) => {
        if (markNext.current) setFresh(new Set(found.filter((l) => seen.current.get(l.id) !== l.updated_at).map((l) => l.id)));
        markNext.current = false;
        seen.current = new Map(found.map((l) => [l.id, l.updated_at]));
        setLoops(found); setLoopsError(null);
      }, (e) => setLoopsError(failed(e))),
      api<Item[]>("/api/attention").then((a) => { setAttention(a); setAttentionError(null); }, (e) => setAttentionError(failed(e))),
      api<PendingAction[]>("/api/actions").then((a) => { setActions(a); setActionsError(null); }, (e) => setActionsError(failed(e))),
    ]);
    setVersion((v) => v + 1);
  }, [showResolved, showSnoozed]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    api<{ enabled: boolean }>("/api/web/status").then((s) => setWebEnabled(s.enabled), (e) => console.warn("web status:", failed(e)));
    // Close an open popover when clicking elsewhere.
    const close = (e: MouseEvent) => {
      for (const menu of document.querySelectorAll<HTMLDetailsElement>("details.menu[open]")) if (!menu.contains(e.target as Node)) menu.open = false;
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  const chat = useChat(() => { markNext.current = true; refresh(); });

  const [notice, setNotice] = useState<string | null>(null); // a failed action, shown at the top of the page

  async function act(path: string, method: string, body?: unknown) {
    try { await send(path, method, body); setNotice(null); } catch (e) { setNotice(failed(e)); }
    await refresh();
  }

  const due = (attention?.length ?? 0) + actions.length;

  return (
    <>
      <Band>
        <Wordmark />
        {attention && (
          <div className="tally" aria-live="polite">
            <span className="tally-n">{due}</span>
            <span className="tally-label">{due === 1 ? "needs you today" : "need you today"}</span>
          </div>
        )}
        <div className="band-actions">
          <a className="btn on-cover solid" href="/investigate.html"><Icon name="search" />Check a message</a>
          <Settings onChange={refresh} />
        </div>
      </Band>
      <main className="page">
        {notice && (
          <p className="notice" role="alert">
            <span>That didn't work: {notice}</span>
            <button className="link-btn" type="button" onClick={() => setNotice(null)}>Dismiss</button>
          </p>
        )}
        <div className="dash">
          <Attention items={attention} error={attentionError} actions={actions} actionsError={actionsError} act={act}
            draft={(id, title) => chat.say(`Draft a follow-up for loop ${id}`, `Draft a follow-up: ${title}`, true)} />
          <Chat chat={chat} />
          <Ledger loops={loops} error={loopsError} fresh={fresh} version={version} webEnabled={webEnabled}
            showSnoozed={showSnoozed} showResolved={showResolved} setShowSnoozed={setShowSnoozed} setShowResolved={setShowResolved}
            act={act} refresh={refresh} useExample={(text) => { chat.setInput(text); document.getElementById("say")?.focus(); }} />
        </div>
      </main>
    </>
  );
}
