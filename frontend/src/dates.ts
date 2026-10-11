import type { Loop } from "./api";

const DAY = 86_400_000;

/** A YYYY-MM-DD date as local midnight (no timezone shift). */
export function localDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}

export const shortDate = (d: Date) => d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

export const stamp = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

export function fromToday(days: number): Date {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + days);
  return d;
}

export function nextMonday(): Date {
  const d = fromToday(1);
  while (d.getDay() !== 1) d.setDate(d.getDate() + 1);
  return d;
}

export const isoDay = (d: Date) =>
  [d.getFullYear(), d.getMonth() + 1, d.getDate()].map((n) => String(n).padStart(2, "0")).join("-");

export function dueInfo(iso: string): { text: string; late: boolean; soon: boolean } {
  const days = Math.round((localDate(iso).getTime() - fromToday(0).getTime()) / DAY);
  const rel = days === 0 ? "today" : days === 1 ? "tomorrow" : days === -1 ? "1 day overdue"
    : days < 0 ? `${-days} days overdue` : `in ${days} days`;
  return { text: `${shortDate(localDate(iso))} · ${rel}`, late: days < 0, soon: days >= 0 && days <= 1 };
}

export const isSnoozed = (l: Loop) =>
  l.status === "open" && !!l.snoozed_until && localDate(l.snoozed_until) > fromToday(0);
