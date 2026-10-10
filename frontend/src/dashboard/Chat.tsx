import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";
import { Icon, Linked } from "../ui";

interface Message {
  role: "user" | "bot" | "error";
  text: string;
  copy?: boolean;
}

const conversation = crypto.randomUUID(); // one Hermes conversation per page load

/** Chat with Hermes through /api/chat. `onReplied` runs after every answer, so the lists can refresh. */
export function useChat(onReplied: () => void) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);
  const [input, setInput] = useState("");

  async function say(text: string, shown = text, copy = false) {
    setPending(true);
    setMessages((m) => [...m, { role: "user", text: shown }]);
    try {
      const { reply } = await api<{ reply: string }>("/api/chat", {
        method: "POST", body: JSON.stringify({ message: text, conversation }),
      });
      setMessages((m) => [...m, { role: "bot", text: reply || "(no reply)", copy: copy && !!reply }]);
    } catch (e) {
      setMessages((m) => [...m, { role: "error", text: `Couldn't reach Continuum. ${(e as Error).message}` }]);
    } finally {
      setPending(false);
      onReplied();
    }
  }

  return { messages, pending, input, setInput, say };
}

export function Chat({ chat }: { chat: ReturnType<typeof useChat> }) {
  const { messages, pending, input, setInput, say } = chat;
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [messages, pending]);
  useEffect(() => { // grow with the text, up to the CSS max-height
    const t = box.current;
    if (t) { t.style.height = "auto"; t.style.height = `${t.scrollHeight + 2}px`; }
  }, [input]);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || pending) return;
    setInput("");
    say(text);
  }

  function enter(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
  }

  return (
    <section className={`chat sheet${messages.length ? " filled" : ""}`} aria-labelledby="chat-h">
      <div className="head"><h2 id="chat-h">Talk to Continuum</h2></div>
      <div className="messages" aria-live="polite">
        {messages.length === 0 && (
          <div className="hello">
            <p>Tell me what you're working on, what you promised, or what you're waiting for.</p>
            <p>Ask "What am I waiting on?" any time.</p>
            <p>Got a message asking you to pay? Paste it and ask "Is this legit?"</p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <Linked text={m.text} />
            {m.copy && <CopyButton text={m.text} />}
          </div>
        ))}
        {pending && <p className="writing">Continuum is writing…</p>}
        <div ref={end} />
      </div>
      <form className="composer" onSubmit={submit}>
        <label className="sr-only" htmlFor="say">Message</label>
        <textarea id="say" ref={box} rows={1} value={input} placeholder="Type a message…"
          onChange={(e) => setInput(e.target.value)} onKeyDown={enter} />
        <button className="btn primary" type="submit" disabled={pending || !input.trim()}>
          <Icon name="send" />Send
        </button>
      </form>
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [label, setLabel] = useState("Copy");
  async function copy() {
    try { await navigator.clipboard.writeText(text); setLabel("Copied"); }
    catch (e) { setLabel(`Copy failed: ${(e as Error).message}`); }
  }
  return <button className="btn small copy" type="button" onClick={copy}>{label}</button>;
}
