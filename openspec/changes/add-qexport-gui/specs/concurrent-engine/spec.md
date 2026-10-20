## Purpose

Provides safe concurrent local data access and coordinated host-level rate limiting so multiple extraction and export operations can run safely at the same time without database contention or API rate violations.

## ADDED Requirements

### Requirement: Concurrent database access with busy timeout
The system SHALL configure the local persistent data store to allow concurrent reader processes and serial writers with an automatic wait timeout, preventing immediate lock contention failures when multiple background operations run simultaneously.

#### Scenario: Writer waits for busy store during concurrent transactions
- **WHEN** a background task attempts to write to the data store while another task is executing a write transaction
- **THEN** the waiting task retries up to the configured busy timeout rather than failing immediately with a database locked error

#### Scenario: Read operations proceed concurrently with writes
- **WHEN** a reporting or status inspection task reads from the store while a background download is committing records
- **THEN** the read task completes without blocking or aborting the active writer

### Requirement: Consistent database snapshot for reporting
The system SHALL generate an isolated point-in-time snapshot of the persistent store for report generation, ensuring that analytical queries and derived action computations never block or mutate the active store during live downloads.

#### Scenario: Snapshot generated during active download
- **WHEN** a report generation task is triggered while conversation or broadcast downloads are actively inserting records
- **THEN** the system creates a consistent database snapshot and executes report computations against the snapshot without interrupting ongoing ingestion

#### Scenario: Snapshot cleanup after report completion or failure
- **WHEN** a report generation finishes successfully or encounters an error
- **THEN** temporary snapshot resources are promptly released and discarded

### Requirement: Shared host rate limiting across concurrent extraction tasks
The system SHALL enforce a unified, shared token bucket rate limiter across all tasks making HTTP requests to the same Qiscus API host, guaranteeing that combined request throughput does not exceed configured safe thresholds.

#### Scenario: Simultaneous conversation and broadcast extraction respects combined rate
- **WHEN** conversation extraction and broadcast log extraction run concurrently against the same host
- **THEN** the total combined request rate across both tasks remains within the configured host request rate limit

#### Scenario: Global backoff penalty upon receiving HTTP 429
- **WHEN** any extraction task receives an HTTP 429 Too Many Requests response from the remote server
- **THEN** the shared rate limiter decreases the shared request rate for all tasks targeting that host and pauses new requests until the penalty period elapses
