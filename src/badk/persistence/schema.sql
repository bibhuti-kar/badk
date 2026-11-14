CREATE TABLE IF NOT EXISTS runs (
  run_id      uuid PRIMARY KEY,
  status      text        NOT NULL,
  started_at  timestamptz NOT NULL,
  header      jsonb       NOT NULL,  -- {status, started_at, ended_at, input, workflow, selection, dag}
  header_eid  bigint      NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_started_at_idx ON runs (started_at DESC);

CREATE TABLE IF NOT EXISTS run_agents (
  run_id     uuid   NOT NULL,
  agent_key  text   NOT NULL,
  eid        bigint NOT NULL,
  capsule    jsonb  NOT NULL,
  result     jsonb  GENERATED ALWAYS AS (capsule #> '{metadata,result}') STORED,
  PRIMARY KEY (run_id, agent_key)
);
CREATE INDEX IF NOT EXISTS run_agents_result_idx ON run_agents USING gin (result jsonb_path_ops);

CREATE TABLE IF NOT EXISTS run_events (
  run_id      uuid   NOT NULL,
  eid         bigint NOT NULL,
  seq         bigint,            -- trace seq, null when no trace record
  agent_key   text,
  trace       jsonb,             -- TraceRecord or null
  logs        jsonb  NOT NULL,   -- [{ts, level, msg}]
  tokens      bigint NOT NULL,
  latency_ms  bigint NOT NULL,
  steps       int    NOT NULL,   -- 1 when the event has metrics of a model or tool call, else 0
  PRIMARY KEY (run_id, eid)
);
CREATE UNIQUE INDEX IF NOT EXISTS run_events_seq_idx ON run_events (run_id, seq) WHERE seq IS NOT NULL;
