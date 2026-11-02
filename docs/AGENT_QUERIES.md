# Questions an agent can ask about its own pipeline

Connect your coding agent to the hosted PostHog MCP server (`https://mcp.posthog.com/mcp`;
Claude Code: `claude mcp add --transport http posthog https://mcp.posthog.com/mcp -s user`),
or ask PostHog AI inside the app. Plain questions work; the SQL below is what you (or the agent)
can run in PostHog's SQL editor to get exact numbers. Replace `'demo-pipeline'` with your pipeline name.

**Status: all SQL here is untested against a live project.** It targets the schema in
[EVENTS.md](EVENTS.md). Boolean properties are compared via `toString(...) = 'true'` so the query
works whether PostHog has typed the property as Boolean or String yet.

### 1. How many generations per model per day?
```sql
SELECT toDate(timestamp) AS day, properties.$ai_model AS model, count() AS generations
FROM events
WHERE event = '$ai_generation' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 30 DAY
GROUP BY day, model
ORDER BY day DESC, generations DESC
```

### 2. What is the p50 / p95 latency of each stage?
```sql
SELECT properties.$ai_span_name AS stage, count() AS runs,
       round(quantile(0.5)(toFloat(properties.$ai_latency)), 2) AS p50_s,
       round(quantile(0.95)(toFloat(properties.$ai_latency)), 2) AS p95_s
FROM events
WHERE event = '$ai_span' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 14 DAY
GROUP BY stage
ORDER BY p95_s DESC
```

### 3. Which stage fails most?
```sql
SELECT properties.stage AS stage, count() AS exceptions,
       countIf(toString(properties.handled) = 'false') AS crashed_stage,
       uniq(properties.$ai_trace_id) AS runs_affected
FROM events
WHERE event = '$exception' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 30 DAY
GROUP BY stage
ORDER BY exceptions DESC
```

### 4. What is the error rate of model calls per stage?
```sql
SELECT properties.stage AS stage, count() AS calls,
       countIf(toString(properties.$ai_is_error) = 'true') AS errors,
       round(errors / calls, 3) AS error_rate
FROM events
WHERE event = '$ai_generation' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 14 DAY
GROUP BY stage
ORDER BY error_rate DESC
```

### 5. How many tokens did each of the last 20 runs use?
```sql
SELECT properties.$ai_trace_id AS run, min(timestamp) AS started,
       sum(toFloat(properties.$ai_input_tokens)) AS input_tokens,
       sum(toFloat(properties.$ai_output_tokens)) AS output_tokens,
       count() AS generations
FROM events
WHERE event = '$ai_generation' AND properties.pipeline = 'demo-pipeline'
GROUP BY run
ORDER BY started DESC
LIMIT 20
```

### 6. What does one drafted output cost (CLI estimate)?
```sql
SELECT toDate(timestamp) AS day,
       sum(toFloat(properties.cost_usd_total)) AS cost_usd,
       sum(toFloat(properties.counter_drafted)) AS drafted,
       round(cost_usd / nullIf(drafted, 0), 4) AS usd_per_draft
FROM events
WHERE event = 'pipeline_run_finished' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 30 DAY
GROUP BY day
ORDER BY day DESC
```

### 7. Which runs failed, where, and why?
```sql
SELECT timestamp, properties.$ai_trace_id AS run, properties.failed_stage AS stage, properties.error_message AS error
FROM events
WHERE event = 'pipeline_run_finished' AND properties.pipeline = 'demo-pipeline' AND toString(properties.ok) = 'false'
ORDER BY timestamp DESC
LIMIT 20
```

### 8. Is the funnel healthy? (fetched -> scored -> drafted per day)
```sql
SELECT toDate(timestamp) AS day,
       sum(toFloat(properties.counter_fetched)) AS fetched,
       sum(toFloat(properties.counter_scored)) AS scored,
       sum(toFloat(properties.counter_drafted)) AS drafted
FROM events
WHERE event = 'pipeline_run_finished' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 30 DAY
GROUP BY day
ORDER BY day DESC
```

### 9. Did the pipeline run today? (silent-failure canary)
```sql
SELECT max(timestamp) AS last_finish, dateDiff('hour', max(timestamp), now()) AS hours_ago,
       argMax(toString(properties.ok), timestamp) AS last_ok,
       argMax(properties.telemetry_errors, timestamp) AS last_telemetry_errors
FROM events
WHERE event = 'pipeline_run_finished' AND properties.pipeline = 'demo-pipeline'
```
Alert if `hours_ago > 26` for a daily job. A missing run produces no events, so only a query like this catches it.

### 10. What are the slowest model calls this week, and in which run?
```sql
SELECT timestamp, properties.$ai_span_name AS call, properties.$ai_model AS model,
       toFloat(properties.$ai_latency) AS latency_s, properties.$ai_output_tokens AS out_tokens,
       properties.$ai_trace_id AS run
FROM events
WHERE event = '$ai_generation' AND properties.pipeline = 'demo-pipeline' AND timestamp > now() - INTERVAL 7 DAY
ORDER BY latency_s DESC
LIMIT 10
```

## Prompts to try with the MCP

- "Using PostHog, which stage of `demo-pipeline` failed most in the last 30 days? Show the top stack trace."
- "Compare p95 latency of the `score` stage this week vs last week."
- "Did `demo-pipeline` finish in the last 26 hours? If not, what was the last error?"
- "What is the estimated cost per drafted output over the last 30 days?"
