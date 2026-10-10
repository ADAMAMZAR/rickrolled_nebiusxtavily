import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, safeHref, startCheck, type Evidence, type Investigation, type RiskLevel } from "../api";
import { Band, Icon, Wordmark } from "../ui";
import { Graph } from "./Graph";

const LEVEL: Record<RiskLevel, string> = {
  LOW: "Low risk", GUARDED: "Guarded", ELEVATED: "Elevated risk", HIGH: "High risk", CRITICAL: "Critical risk",
  INSUFFICIENT_EVIDENCE: "Not enough evidence",
};
const INK: Record<RiskLevel, string> = {
  LOW: "settled", GUARDED: "warn", ELEVATED: "warn", HIGH: "warn", CRITICAL: "warn", INSUFFICIENT_EVIDENCE: "plain",
};
const IDENTITY = { verified: "verified", mismatch: "doesn't match", unverified: "not verified" };
const VERDICT: Record<string, string> = {
  supported: "Confirmed", contradicted: "Contradicted", suspicious: "Suspicious", unverified: "Unconfirmed", insufficient_evidence: "No evidence",
};
const DIRECTION = { supports: "supports", contradicts: "contradicts", warns: "warning", neutral: "mentions" };
const TIER = { A: "Regulator or government", B: "Official site", C: "News", D: "Other site", E: "Social media" };

const hashId = () => location.hash.slice(1);

export function App() {
  const [id, setId] = useState(hashId);
  useEffect(() => {
    const route = () => setId(hashId());
    addEventListener("hashchange", route);
    return () => removeEventListener("hashchange", route);
  }, []);

  return (
    <>
      <Band>
        <Wordmark />
        <h1 className="band-title">Check a message</h1>
        <nav className="band-actions" aria-label="Pages">
          {id && <a className="btn on-cover" href="#">New check</a>}
          <a className="btn on-cover solid" href="/">Your loops</a>
        </nav>
      </Band>
      <main className="page check">{id ? <Result key={id} id={id} /> : <Home />}</main>
    </>
  );
}

function Home() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { document.title = "Check a message · Continuum"; }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try { location.hash = await startCheck(new FormData(e.currentTarget)); }
    catch (err) {
      const message = (err as Error).message;
      setError(message === "Failed to fetch" ? "Couldn't reach Continuum. Is it running?" : message);
    } finally { setBusy(false); }
  }

  return (
    <div className="home">
      <section className="section sheet" aria-labelledby="form-h">
        <div className="head"><h2 id="form-h">Paste what they sent you</h2></div>
        <form className="check-form" onSubmit={submit}>
          <label><span>Message</span><textarea name="text" maxLength={8000} rows={8} placeholder="The message that asks you to pay or share details…" /></label>
          <label><span>Link <span className="muted">(optional)</span></span><input type="url" name="url" maxLength={2000} placeholder="https://…" /></label>
          <label><span>Screenshot <span className="muted">(optional, PNG, JPEG or WebP, up to 5 MB)</span></span>
            <input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp" /></label>
          <div className="submit-row">
            <button className="btn primary big" type="submit" disabled={busy}><Icon name="search" />{busy ? "Starting…" : "Check"}</button>
            {error && <p className="error-line" role="alert">{error}</p>}
          </div>
        </form>
      </section>
      <aside className="how" aria-label="How the check works">
        <p><b>Who's asking?</b> Continuum looks the sender up on Bank Negara's and the Securities Commission's alert lists, and finds the company's real website.</p>
        <p><b>Fixed rules set the risk level</b>, not the AI. Every finding links to its source or to the words in the message.</p>
        <p><b>Takes 1–2 minutes.</b> The message, screenshot and page text go to NVIDIA Nemotron on Nebius; names and links go to Tavily search. Nothing is sent to anyone else.</p>
      </aside>
    </div>
  );
}

function Result({ id }: { id: string }) {
  const [d, setD] = useState<Investigation | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    let timer: number | undefined;
    let live = true;
    async function load() {
      try {
        const res = await fetch(`/api/investigations/${encodeURIComponent(id)}`);
        const body = await res.json();
        if (!live) return;
        if (!res.ok) { setStatus(body.message || "Couldn't load that check."); return; }
        setD(body); setStatus(null);
        if (body.status === "running") timer = window.setTimeout(load, 1000);
      } catch {
        if (!live) return;
        setStatus("Couldn't reach Continuum. Retrying…");
        timer = window.setTimeout(load, 3000);
      }
    }
    load();
    return () => { live = false; clearTimeout(timer); };
  }, [id]);

  useEffect(() => {
    if (d?.status === "done") document.title = `${LEVEL[d.risk_level ?? "INSUFFICIENT_EVIDENCE"]} · Continuum`;
  }, [d?.status, d?.risk_level]);

  const focus = useCallback((selector: string) => {
    const target = document.querySelector(selector);
    if (!target) return;
    target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
    target.classList.add("flash");
    setTimeout(() => target.classList.remove("flash"), 1600);
  }, []);

  if (!d) return <p className="status-line" role="status">{status ?? "Loading the check…"}</p>;
  return (
    <>
      {status && <p className="status-line" role="status">{status}</p>}
      {d.status === "done" ? <Verdict d={d} /> : <Live d={d} />}
      {d.status === "done" && (
        <>
          <section className="section sheet" aria-labelledby="graph-h">
            <div className="head"><h2 id="graph-h">Evidence graph</h2><span className="tools muted">Tap a line or a point to see its evidence below.</span></div>
            <Graph d={d} onFocus={focus} />
            <div className="legend">
              <span style={{ ["--c" as string]: "var(--red)" }}>warns or contradicts</span>
              <span style={{ ["--c" as string]: "var(--blue)" }}>supports or official</span>
              <span style={{ ["--c" as string]: "var(--rule-strong)" }}>in the message</span>
            </div>
          </section>
          <Explorer d={d} />
        </>
      )}
      {d.status === "done" && <Steps d={d} />}
    </>
  );
}

function Live({ d }: { d: Investigation }) {
  return (
    <section className="section sheet live" aria-live="polite">
      <div className="head">
        <h2>{d.status === "failed" ? "The check didn't finish" : "Checking…"}</h2>
        <Counts d={d} />
      </div>
      {d.status === "failed" && <p className="error-line pad">{d.error} Try again, or paste the message again in a minute.</p>}
      <Steps d={d} open={d.status === "failed"} />
    </section>
  );
}

function Counts({ d }: { d: Investigation }) {
  const n = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  const hosts = new Set(d.evidence.map((e) => e.host)).size;
  return (
    <span className="tools muted num">
      {n(d.entities.length, "name or contact", "names and contacts")} · {n(d.claims.length, "claim", "claims")} · {n(hosts, "source", "sources")} with evidence
    </span>
  );
}

/** The step log: only the latest step while running, all of them on request. */
function Steps({ d, open = false }: { d: Investigation; open?: boolean }) {
  const [all, setAll] = useState(open);
  const running = d.status === "running";
  const done = d.status === "done";
  const shown = all ? d.steps : d.steps.slice(-1);
  return (
    <div className={`steps${done ? " sheet" : ""}`}>
      {done && <h3 className="subhead">How the check went</h3>}
      {done && !all ? <p>Done in {d.steps.length} steps.</p> : (
        <ol start={all ? 1 : d.steps.length}>
          {shown.map((s, i) => <li key={s.at + i} className={running && s === d.steps.at(-1) ? "now" : undefined}>{s.text}</li>)}
        </ol>
      )}
      {d.steps.length > 1 && (
        <button className="link-btn" type="button" onClick={() => setAll(!all)}>{all ? "Show only the latest" : `Show all ${d.steps.length} steps`}</button>
      )}
    </div>
  );
}

function Verdict({ d }: { d: Investigation }) {
  const level = d.risk_level ?? "INSUFFICIENT_EVIDENCE";
  const evidence = Object.fromEntries(d.evidence.map((e) => [e.id, e]));
  const signals = Object.fromEntries(d.signals.map((s) => [s.id, s]));
  const strong = new Set(d.evidence.filter((e) => e.tier === "A" || e.tier === "B").map((e) => e.url)).size;
  return (
    <section className="verdict sheet" aria-labelledby="level-h">
      <div className="verdict-main">
        <h2 id="level-h" className={`level ${INK[level]}`}>{LEVEL[level]}</h2>
        <dl className="facts">
          <div><dt>Confidence</dt><dd>{(d.confidence ?? "LOW").toLowerCase()}</dd></div>
          <div><dt>Identity</dt><dd>{IDENTITY[d.identity ?? "unverified"]}</dd></div>
          <div><dt>Score</dt><dd className="num">{d.score ?? 0}</dd></div>
          <div><dt>Regulator or official sources</dt><dd className="num">{strong}</dd></div>
        </dl>
        <p className="rules-note muted">Set by fixed rules from the evidence below, not by the AI.</p>
        <ul className="rows findings">
          {d.findings.map((f, i) => {
            const seen = new Set<string>();
            const cites: Evidence[] = [];
            const quotes: string[] = [];
            for (const id of f.evidence_ids) if (evidence[id] && !seen.has(evidence[id].url)) { seen.add(evidence[id].url); cites.push(evidence[id]); }
            for (const id of f.signal_ids) {
              const s = signals[id];
              const e = s?.evidence_id ? evidence[s.evidence_id] : undefined;
              if (e && !seen.has(e.url)) { seen.add(e.url); cites.push(e); }
              else if (s?.input_quote) quotes.push(s.input_quote);
            }
            return (
              <li key={i}>
                <p>{f.text}</p>
                <p className="cites">
                  {cites.map((e) => <CardLink key={e.id} e={e} />)}
                  {quotes.map((q) => <span key={q}>In the message: “{q}”</span>)}
                </p>
              </li>
            );
          })}
        </ul>
        <div className="todo">
          <h3>What to do</h3>
          <ul>{d.next_steps.map((t) => <li key={t}>{t}</li>)}</ul>
          {d.loop_title && <p className="tracked">On your list: <b>{d.loop_title}</b> · <a href="/">Open your loops</a></p>}
        </div>
      </div>
      <figure className="checked">
        <h3>The message that was checked</h3>
        <Flagged text={[d.input_text, d.input_url].filter(Boolean).join("\n\n")} quotes={d.signals.map((s) => s.input_quote).filter((q): q is string => !!q)} />
        <figcaption className="muted">Underlined: the words behind a risk signal.</figcaption>
      </figure>
    </section>
  );
}

/** The checked message with each quoted pressure tactic underlined in red. */
function Flagged({ text, quotes }: { text: string; quotes: string[] }) {
  const marks: [number, number][] = [];
  const lower = text.toLowerCase();
  for (const q of quotes) {
    const at = lower.indexOf(q.toLowerCase());
    if (at >= 0) marks.push([at, at + q.length]);
  }
  marks.sort((a, b) => a[0] - b[0]);
  const parts = [];
  let i = 0;
  for (const [start, end] of marks) {
    if (start < i) continue; // overlapping quotes: keep the first
    parts.push(text.slice(i, start), <mark key={start} className="flagged">{text.slice(start, end)}</mark>);
    i = end;
  }
  parts.push(text.slice(i));
  return <p className="message-text">{parts}</p>;
}

const CardLink = ({ e }: { e: Evidence }) => {
  const href = safeHref(e.url);
  const label = `${e.host} (${TIER[e.tier]})`;
  return href ? <a href={href} target="_blank" rel="noopener noreferrer">{label}</a> : <span>{label}</span>;
};

function EvidenceCard({ e }: { e: Evidence }) {
  const href = safeHref(e.url);
  return (
    <article className="ev" id={`ev-${e.id}`}>
      <header>
        <span className={`tier tier-${e.tier}`} title={TIER[e.tier]}>{e.tier}</span>
        {href ? <a href={href} target="_blank" rel="noopener noreferrer">{e.host}</a> : <span>{e.host}</span>}
        <span className={`dir dir-${e.direction}`}>{DIRECTION[e.direction]}</span>
        {e.official_type && <span className="muted">states the official {e.official_type}: {e.official_value}</span>}
      </header>
      <blockquote>{e.quote}</blockquote>
    </article>
  );
}

function Explorer({ d }: { d: Investigation }) {
  const about = (id: string) => d.evidence.filter((e) => (e.claim_id || e.entity_id) === id);
  const flags = Object.fromEntries(d.graph.nodes.map((n) => [n.id, n.flag]));
  const tactics = d.signals.filter((s) => s.input_quote);
  const entities = d.entities.filter((ent) => about(ent.id).length || flags[ent.id]);
  return (
    <section className="section sheet" aria-labelledby="claims-h">
      <div className="head"><h2 id="claims-h">Claims and evidence</h2></div>
      {d.claims.length + entities.length + tactics.length === 0 && <p className="empty quiet">No claims or evidence were found.</p>}
      <ul className="rows explorer">
        {d.claims.map((c) => (
          <li key={c.id} id={`n-${c.id}`} className="item">
            <header><h3>{c.text}</h3><span className={`chip v-${c.verdict}`}>{VERDICT[c.verdict] ?? c.verdict}</span></header>
            <p className="muted">{c.reason}</p>
            {about(c.id).map((e) => <EvidenceCard key={e.id} e={e} />)}
          </li>
        ))}
        {entities.map((ent) => (
          <li key={ent.id} id={`n-${ent.id}`} className="item">
            <header>
              <h3>{ent.value}</h3><span className="muted">{ent.type}</span>
              {flags[ent.id] && <span className={`chip v-${flags[ent.id]}`}>{flags[ent.id] === "mismatch" ? "Not the official site" : "Official"}</span>}
            </header>
            {about(ent.id).map((e) => <EvidenceCard key={e.id} e={e} />)}
          </li>
        ))}
        {tactics.length > 0 && (
          <li id="n-message" className="item">
            <header><h3>Pressure tactics in the message</h3></header>
            {tactics.map((s) => (
              <article key={s.id} className="ev">
                <header><span className="dir dir-warns">{s.kind.replace(/_/g, " ")}</span></header>
                <blockquote>{s.input_quote}</blockquote>
              </article>
            ))}
          </li>
        )}
      </ul>
    </section>
  );
}
