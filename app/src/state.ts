// Состояние окна. Один объект, без магии; экраны читают его и дёргают bus.

export interface AgentState {
  agent: string;
  owner: string;
  level: "ok" | "live" | "warn" | "error";
  phrase: string;
  action: { label: string; target: string } | null;
  runner: { alive: boolean; age_s: number | null; busy: boolean; run: string; since: number };
  brain: {
    configured: boolean;
    model: string;
    base_url: string;
    last_call_at: number | null;
    last_error: string | null;
    last_error_raw: string | null;
  };
  relay: { used: boolean; authorized: boolean };
  telegram: { enabled: boolean };
  next_wake: string | null;
  alarms: Array<{ kind: string; text: string }>;
}

export interface Run {
  id: string;
  kind: string;
  status: string;
  terminal_status?: string;
  chat_id?: string | number | null;
  chat_title?: string;
  goal_head?: string;
  created_at?: string;
  forge_task_id?: string;
}

export interface Room {
  key: string;
  name: string;
  live: boolean;
  count: number;
}

export type View = "talk" | "plans" | "frame" | "files" | "journal" | "anatomy" | "settings";

export const S = {
  agent: "Агент",
  agentState: null as AgentState | null,
  view: "talk" as View,
  room: "window",
  roomName: "Окно",
  runs: [] as Run[],
  rooms: [] as Room[],
  evOpen: new Set<string>(),
  evCache: new Map<string, unknown>(),
  mdSel: "" as string,
  stream: "" as string,
  captures: [] as string[],
  connected: false,
};

export const WINDOW_ROOM = "window";
