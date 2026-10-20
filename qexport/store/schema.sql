-- qexport checkpoint store. PRAGMA journal_mode/foreign_keys are set by CheckpointStore.
CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  app_code TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  room_cursor TEXT,                 -- NULL = not started
  enumeration_done INTEGER NOT NULL DEFAULT 0,
  broadcast_cursor TEXT,
  broadcast_enumeration_done INTEGER NOT NULL DEFAULT 0,
  last_completed_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
  room_id TEXT PRIMARY KEY,
  customer_room_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  customer_identifier TEXT NOT NULL,
  customer_phone TEXT NOT NULL,     -- normalize_phone(customer_identifier); joins broadcast_logs.phone
  room_type TEXT NOT NULL,
  is_resolved INTEGER NOT NULL,
  is_handled_by_bot INTEGER NOT NULL,
  last_activity_utc TEXT,
  fetched_last_activity_utc TEXT,   -- value at time of last successful fetch (incremental runs)
  fetch_state TEXT NOT NULL DEFAULT 'pending'
     CHECK (fetch_state IN ('pending','done','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  msg_id INTEGER PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
  timestamp_utc TEXT NOT NULL,
  sender_type TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  message_type TEXT NOT NULL,
  text TEXT NOT NULL,
  attachment_url TEXT, attachment_name TEXT, reply_to_text TEXT,
  is_deleted INTEGER NOT NULL, delivery_status TEXT,
  reply_to_msg_id INTEGER,
  wamid TEXT,
  context_wamid TEXT,
  button_text TEXT,
  broadcast_template TEXT
);

CREATE TABLE IF NOT EXISTS broadcast_jobs (
  job_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  template_name TEXT NOT NULL,
  category TEXT NOT NULL,
  status TEXT NOT NULL,
  channel_id INTEGER,
  channel_name TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  started_at_utc TEXT,
  finished_at_utc TEXT,
  total_recipient INTEGER NOT NULL,
  total_sent INTEGER NOT NULL,
  total_delivered INTEGER NOT NULL,
  total_read INTEGER NOT NULL,
  total_failed INTEGER NOT NULL,
  signature TEXT NOT NULL,          -- status+totals; a change means recipient logs changed
  fetched_signature TEXT,
  logs_state TEXT NOT NULL DEFAULT 'pending'
     CHECK (logs_state IN ('pending','done','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS broadcast_logs (
  log_id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES broadcast_jobs(job_id) ON DELETE CASCADE,
  phone TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  status TEXT NOT NULL,
  sent_at_utc TEXT,
  wamid TEXT,
  failure_reason TEXT,
  buttons TEXT,
  params TEXT,
  is_retried INTEGER NOT NULL
);

-- Derived on every `write-xlsx`; never fetched.
CREATE TABLE IF NOT EXISTS actions (
  msg_id INTEGER PRIMARY KEY,
  room_id TEXT NOT NULL,
  customer_phone TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  action_type TEXT NOT NULL,
  detail TEXT NOT NULL,
  form_json TEXT,
  campaign_job_id INTEGER,
  campaign_name TEXT,
  attribution TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_messages_room_ts ON messages(room_id, timestamp_utc, msg_id);
CREATE INDEX IF NOT EXISTS ix_messages_context ON messages(context_wamid);
CREATE INDEX IF NOT EXISTS ix_rooms_state ON rooms(fetch_state);
CREATE INDEX IF NOT EXISTS ix_rooms_phone ON rooms(customer_phone);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON broadcast_jobs(logs_state);
CREATE INDEX IF NOT EXISTS ix_logs_wamid ON broadcast_logs(wamid);
CREATE INDEX IF NOT EXISTS ix_logs_phone ON broadcast_logs(phone, sent_at_utc);
CREATE INDEX IF NOT EXISTS ix_logs_job ON broadcast_logs(job_id);
CREATE INDEX IF NOT EXISTS ix_actions_job_phone ON actions(campaign_job_id, customer_phone);
CREATE INDEX IF NOT EXISTS ix_actions_phone ON actions(customer_phone, timestamp_utc);
