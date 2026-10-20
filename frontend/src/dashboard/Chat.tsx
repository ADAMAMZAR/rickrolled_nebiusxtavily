import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";
import { Icon, Linked, unbold } from "../ui";

interface Message {
  role: "user" | "bot" | "error";
  text: string;
  copy?: boolean;
  paste?: string; // a chat message Hermes couldn't take: offer to save it without chat
}

type Mode = "chat" | "paste";

// One Hermes conversation per page load. Not crypto.randomUUID: browsers only allow it on https or localhost.
const conversation = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

/** Chat with Hermes through /api/chat, or save a pasted message without it (/api/messages). Opens on Paste when
 * Hermes isn't running here. `onReplied` runs after every answer, so the lists can refresh. */
export function useChat(onReplied: () => void) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<Mode>("chat");
  const [hermes, setHermes] = useState<boolean | null>(null); // null: not known yet

  useEffect(() => {
    api<{ running: boolean }>("/api/chat/status").then(
      ({ running }) => { setHermes(running); if (!running) setMode("paste"); },
      (e) => console.warn("chat status:", (e as Error).message),
    );
  }, []);

  async function run(shown: string, ask: () => Promise<string>, failed: (e: Error) => Message, copy = false) {
    setPending(true);
    setMessages((m) => [...m, { role: "user", text: shown }]);
    try {
      const reply = await ask();
      setMessages((m) => [...m, { role: "bot", text: reply || "(no reply)", copy: copy && !!reply }]);
    } catch (e) {
      setMessages((m) => [...m, failed(e as Error)]);
    } finally {
      setPending(false);
      onReplied();
    }
  }

  /** `offerPaste`: a message the user typed, which Paste could save if Hermes can't take it. */
  const say = (text: string, shown = text, copy = false, offerPaste = false) => run(shown,
    async () => (await api<{ reply: string }>("/api/chat", { method: "POST", body: JSON.stringify({ message: text, conversation }) })).reply,
    (e) => ({ role: "error", text: `Couldn't reach Troth. ${e.message}`, paste: offerPaste ? text : undefined }), copy);

  const save = (text: string) => run(text,
    async () => (await api<{ reply: string }>("/api/messages", { method: "POST", body: JSON.stringify({ text }) })).reply,
    (e) => ({ role: "error", text: e.message }));

  return { messages, pending, input, setInput, say, save, mode, setMode, hermes };
}

export function Chat({ chat }: { chat: ReturnType<typeof useChat> }) {
  const { messages, pending, input, setInput, say, save, mode, setMode, hermes } = chat;
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const paste = mode === "paste";

  // Braces: newer browsers return a Promise from scrollIntoView, which React would call as a cleanup.
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [messages, pending]);
  useEffect(() => { // grow with the text, up to the CSS max-height
    const t = box.current;
    if (t) { t.style.height = "auto"; t.style.height = `${t.scrollHeight + 2}px`; }
  }, [input]);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || pending) return;
    setInput("");
    if (paste) save(text); else say(text, text, false, true);
  }

  function enter(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
  }

  function saveInstead(text: string) {
    setMode("paste");
    save(text);
  }

  return (
    <section className={`chat sheet${messages.length ? " filled" : ""}`} aria-labelledby="chat-h">
      <div className="head">
        <h2 id="chat-h">{paste ? "Paste a message" : "Talk to Troth"}</h2>
        <span className="tools mode-pick" role="group" aria-label="Chat or paste">
          <button className="link-btn" type="button" aria-pressed={!paste} onClick={() => setMode("chat")}>Chat</button>
          <button className="link-btn" type="button" aria-pressed={paste} onClick={() => setMode("paste")}>Paste</button>
        </span>
      </div>
      <div className="messages" aria-live="polite">
        {messages.length === 0 && (paste ? (
          <div className="hello">
            <p>Paste a message or write a note. Troth saves the tasks, promises and things you're waiting on.</p>
            <p>{hermes === false ? "Chat needs Hermes, which isn't running here. Paste works without it." : "No chat needed: it works without Hermes."}</p>
            <p>Got something wrong? Edit or delete it under Open loops.</p>
          </div>
        ) : (
          <div className="hello">
            <p>Tell me what you're working on, what you promised, or what you're waiting for.</p>
            <p>Ask "What am I waiting on?" any time.</p>
            <p>Got a message asking you to pay? Paste it and ask "Is this legit?"</p>
          </div>
        ))}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <Linked text={m.text} />
            {m.copy && <CopyButton text={m.text} />}
            {m.paste && (
              <div className="copy">
                <button className="btn small" type="button" disabled={pending} onClick={() => saveInstead(m.paste!)}>Save it without chat</button>
              </div>
            )}
          </div>
        ))}
        {pending && <p className="writing">{paste ? "Saving…" : "Troth is writing…"}</p>}
        <div ref={end} />
      </div>
      <form className="composer" onSubmit={submit}>
        <label className="sr-only" htmlFor="say">{paste ? "Message to save" : "Message"}</label>
        <textarea id="say" ref={box} rows={1} value={input} placeholder={paste ? "Paste a message or write a note…" : "Type a message…"}
          onChange={(e) => setInput(e.target.value)} onKeyDown={enter} />
        <button className="btn primary" type="submit" disabled={pending || !input.trim()}>
          <Icon name={paste ? "done" : "send"} />{paste ? "Save" : "Send"}
        </button>
      </form>
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [label, setLabel] = useState("Copy");
  async function copy() {
    try { await navigator.clipboard.writeText(unbold(text)); setLabel("Copied"); }
    catch (e) { setLabel(`Copy failed: ${(e as Error).message}`); }
  }
  return <button className="btn small copy" type="button" onClick={copy}>{label}</button>;
}
