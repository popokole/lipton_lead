export type AccountStatus =
  | 'CREATED'
  | 'AUTHENTICATING'
  | 'ONLINE'
  | 'OFFLINE'
  | 'ERROR'
  | 'DISABLED'
  | 'AUTH_REQUIRED';

export interface Account {
  id: string;
  label: string;
  tg_user_id: number | null;
  username: string | null;
  display_name: string | null;
  status: AccountStatus;
  enabled: boolean;
  proxy_id: string | null;
  worker_id: string | null;
  last_seen_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface Proxy {
  id: string;
  name: string;
  scheme: string;
  host: string;
  port: number;
  username: string | null;
  enabled: boolean;
}

export interface Dialog {
  tg_chat_id: number;
  title: string | null;
  username: string | null;
  is_group: boolean;
  is_channel: boolean;
  is_user: boolean;
}

export interface TDataAccount {
  index: number;
  tg_user_id: number;
  dc_id: number;
}

export interface ChatNode {
  id: string;
  tg_chat_id: number;
  type: string;
  title: string | null;
  username: string | null;
  monitored: boolean;
  has_avatar: boolean;
  last_message_at: string | null;
  messages_total: number;
  leads_count: number;
  replies_count: number;
  active_users: number;
}

export interface ChatTreeAccount {
  account_id: string;
  label: string;
  username: string | null;
  messages_total: number;
  leads_count: number;
  chats: ChatNode[];
}

export interface Chat {
  id: string;
  account_id: string;
  tg_chat_id: number;
  type: 'PRIVATE' | 'GROUP' | 'SUPERGROUP' | 'CHANNEL';
  title: string | null;
  username: string | null;
  monitored: boolean;
  last_message_at: string | null;
  has_avatar: boolean;
}

export interface Scenario {
  id: string;
  name: string;
  description: string | null;
  system_prompt: string;
  model: string | null;
  temperature: number | null;
  max_reply_length: number | null;
  context_messages: number | null;
  require_knowledge_grounding: boolean;
  reply_in_dm?: boolean;
  group_ack_text?: string | null;
  lead_criteria?: string | null;
  review_when_uncertain?: boolean;
  review_min_confidence?: number | null;
  fallback_text: string | null;
  enabled: boolean;
}

export type ActionType =
  | 'REPLY'
  | 'NOTIFY_ADMIN'
  | 'SAVE_LEAD'
  | 'TAG_USER'
  | 'ESCALATE_TO_HUMAN'
  | 'REQUEST_REVIEW'
  | 'IGNORE';

export interface Rule {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  priority: number;
  stop_on_match: boolean;
  scope: 'CHAT_MONITOR' | 'DIALOG' | 'ALL';
  scenario_id: string | null;
  filters: Record<string, unknown>;
  keywords: { terms?: string[]; exclude?: string[]; mode?: string };
  regex: string | null;
  ai_enabled: boolean;
  ai_threshold: number | null;
  cooldown: Record<string, number>;
  action: ActionType;
  action_config: Record<string, unknown>;
}

export interface Message {
  id: string;
  account_id: string;
  chat_id: string | null;
  chat_title: string | null;
  chat_username: string | null;
  chat_has_avatar: boolean;
  tg_chat_id: number;
  tg_message_id: number;
  sender_tg_id: number | null;
  sender_username: string | null;
  sender_display_name: string | null;
  text: string | null;
  date: string;
  is_incoming: boolean;
  is_bot_reply: boolean;
  processed_status: 'PENDING' | 'SKIPPED' | 'MATCHED' | 'REPLIED' | 'IGNORED' | 'ESCALATED' | 'ACTED' | 'FAILED';
  status_reason: string | null;
  rule_id: string | null;
}

export interface ActionRow {
  id: string;
  type: ActionType;
  status: string;
  account_id: string;
  chat_id: string | null;
  rule_id: string | null;
  reply_text: string | null;
  validation: { passed: boolean; checks: { name: string; passed: boolean; reason: string | null }[] } | null;
  error: string | null;
  sent_at: string | null;
  created_at: string;
}

export interface LogRow {
  id: number;
  ts: string;
  level: string;
  event_type: string;
  account_id: string | null;
  chat_id: string | null;
  chat_title: string | null;
  chat_username: string | null;
  chat_has_avatar: boolean;
  message_id: string | null;
  rule_id: string | null;
  duration_ms: number | null;
  status: string | null;
  error: string | null;
  extra: Record<string, unknown> | null;
}

export interface Lead {
  id: string;
  account_id: string;
  tg_user_id: number;
  username: string | null;
  display_name: string | null;
  intent: string | null;
  score: number;
  status: string;
  first_seen_at: string;
  last_seen_at: string | null;
  notes: string | null;
}

export interface Thread {
  chat_id: string;
  account_id: string;
  tg_chat_id: number;
  kind: 'dm' | 'group';
  title: string | null;
  username: string | null;
  has_avatar: boolean;
  monitored: boolean;
  message_count: number;
  last_message_at: string | null;
  last_text: string | null;
  awaiting_reply: boolean;
  lead_score: number | null;
  lead_status: string | null;
  ai_chat: boolean;
}

export interface Conversation {
  id: string;
  account_id: string;
  peer_tg_id: number;
  status: string;
  summary: string | null;
  message_count: number;
  ai_replies_in_row: number;
  last_message_at: string | null;
}

export interface Escalation {
  conversation_id: string;
  account_id: string;
  account_label: string | null;
  peer_tg_id: number;
  chat_id: string | null;
  chat_title: string | null;
  chat_username: string | null;
  chat_has_avatar: boolean;
  status: string;
  reason: string | null;
  suggested_reply: string | null;
  notification_id: string | null;
  summary: string | null;
  last_message_at: string | null;
  created_at: string;
}

export interface Worker {
  id: string;
  name: string;
  status: string;
  hostname: string | null;
  pid: number | null;
  version: string | null;
  accounts: string[];
  accounts_count: number;
  queue_size: number;
  last_error: string | null;
  updated_at: string | null;
}

export interface DashboardCounters {
  accounts_total: number;
  accounts_online: number;
  chats_monitored: number;
  messages_today: number;
  ai_analyzed_today: number;
  replies_today: number;
  leads_total: number;
  errors_today: number;
  workers_healthy: number;
}

export interface DailyPoint {
  day: string;
  value: number;
}

export interface DashboardSeries {
  messages: DailyPoint[];
  matches: DailyPoint[];
  replies: DailyPoint[];
  leads: DailyPoint[];
  errors: DailyPoint[];
}

export interface RuleStat {
  rule_id: string;
  rule_name: string;
  enabled: boolean;
  matches: number;
  replies: number;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string | null;
  role: 'ADMIN' | 'OPERATOR' | 'VIEWER';
  is_active: boolean;
  last_login_at: string | null;
}

export interface RealtimeEvent {
  type: string;
  ts: string;
  account_id: string | null;
  payload: Record<string, unknown>;
}
