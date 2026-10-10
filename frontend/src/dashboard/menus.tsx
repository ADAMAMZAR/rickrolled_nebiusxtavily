import { useState } from "react";
import { api, type GoogleStatus } from "../api";
import { fromToday, isoDay, localDate, nextMonday, shortDate } from "../dates";
import { Icon } from "../ui";

/** Snooze picks, plus any date. Fri: "In 3 days" is Monday, so duplicates are dropped. */
export function SnoozeMenu({ onPick, small = false }: { onPick: (until: string) => void; small?: boolean }) {
  const picks = ([["Tomorrow", fromToday(1)], ["In 3 days", fromToday(3)], ["Next Monday", nextMonday()]] as const)
    .filter(([, d], i, all) => all.findIndex(([, e]) => isoDay(e) === isoDay(d)) === i);
  return (
    <details className="menu">
      <summary className={`btn${small ? " small" : ""}`}>Snooze</summary>
      <div className="menu-pop">
        {picks.map(([label, d]) => (
          <button key={label} type="button" onClick={() => onPick(isoDay(d))}>{label} · {shortDate(d)}</button>
        ))}
        <label>Pick a date
          <input type="date" min={isoDay(fromToday(1))} onChange={(e) => e.target.value && onPick(isoDay(localDate(e.target.value)))} />
        </label>
      </div>
    </details>
  );
}

/** Google connection and stored email text. */
export function Settings({ onChange }: { onChange: () => void }) {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null); // the last action's outcome

  async function load() {
    try { setStatus(await api<GoogleStatus>("/api/google/status")); setError(null); }
    catch (e) { setError((e as Error).message); }
  }

  async function google(path: string, waiting: string) {
    setBusy(waiting);
    setResult(null);
    try { await api(path, { method: "POST" }); } catch (e) { setResult({ ok: false, text: (e as Error).message }); }
    setBusy(null);
    load();
  }

  async function forget() {
    if (!confirm('Forget all email text Continuum stored? Loops stay, marked "source deleted".')) return;
    try {
      const { forgotten } = await api<{ forgotten: number }>("/api/google/forget", { method: "POST" });
      setResult({ ok: true, text: `Forgot ${forgotten} email${forgotten === 1 ? "" : "s"}.` });
    } catch (e) { setResult({ ok: false, text: (e as Error).message }); }
    onChange();
  }

  const last = status?.last_sync
    ? new Date(status.last_sync).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : "not yet";

  return (
    <details className="menu settings" onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && load()}>
      <summary className="btn on-cover"><Icon name="gear" />Settings</summary>
      <div className="menu-pop settings-pop">
        <h3>Google</h3>
        {error && <p className="error-line">Couldn't load settings: {error}</p>}
        {status?.connected && (<>
          <p><span className="ok">Connected</span> · Last email check: {last}</p>
          <p className="muted">Reads email only from people linked to open loops. Drafts and calendar events need your OK. Nothing is ever sent.</p>
          <div><button className="btn small" type="button" disabled={!!busy} onClick={() => google("/api/google/disconnect", "Disconnecting…")}>{busy ?? "Disconnect"}</button></div>
        </>)}
        {status && !status.connected && status.set_up && (<>
          <p>Not connected. Connecting opens Google's sign-in in your browser.</p>
          <div><button className="btn small primary" type="button" disabled={!!busy} onClick={() => google("/api/google/connect", "Waiting for the browser…")}>{busy ?? "Connect Google"}</button></div>
        </>)}
        {status && !status.connected && !status.set_up && <p>Not set up: add your OAuth client file first (README: Google setup).</p>}
        <h3>Email data</h3>
        <p className="muted">Deletes the text of every email Continuum read. Loops made from them stay.</p>
        <div><button className="btn small danger" type="button" onClick={forget}>Forget email data</button></div>
        {result && <p className={result.ok ? "ok" : "error-line"} role={result.ok ? "status" : "alert"}>{result.text}</p>}
      </div>
    </details>
  );
}
