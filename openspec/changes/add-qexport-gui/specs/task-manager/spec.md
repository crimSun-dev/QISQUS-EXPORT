## Purpose

Manages background execution of data extraction and report generation tasks with independent progress reporting, execution lifecycle controls (pause, resume, cancel), and mutual exclusion conflict prevention.

## ADDED Requirements

### Requirement: Multi-task background execution and monitoring
The system SHALL support executing long-running extraction and workbook building operations as distinct background tasks, streaming live progress status including completed count, total items, current item identifier, processing rate, and estimated time remaining.

#### Scenario: Concurrent independent tasks run and stream progress
- **WHEN** conversation downloading and workbook building are initiated concurrently
- **THEN** both tasks execute concurrently in the background and independently emit progress metrics without freezing the interface

#### Scenario: Progress calculation includes throughput and ETA
- **WHEN** a task processes items sequentially over time
- **THEN** the system continuously calculates and reports instantaneous throughput (items/sec) and estimated completion time based on remaining items

### Requirement: Task lifecycle controls
The system SHALL provide Pause, Resume, and Cancel operations for active background tasks, persisting completed state so cancelled or interrupted tasks can resume seamlessly without repeating already-committed work.

#### Scenario: Pausing and resuming an active download task
- **WHEN** user issues a pause command to a running extraction task
- **THEN** the task finishes the item currently in-flight, transitions to a paused state, stops issuing network requests, and resumes cleanly when instructed

#### Scenario: Cancelling an active task preserves committed progress
- **WHEN** user issues a cancel command to a running task
- **THEN** the task cleanly halts further execution while leaving all previously committed database records and checkpoint markers intact

### Requirement: Task conflict prevention
The system SHALL enforce mutual exclusion rules to prevent simultaneous execution of conflicting tasks (such as two identical room enumerators on the same app code), presenting a clear explanatory notice instead of raising an unhandled conflict error.

#### Scenario: Attempting to launch a duplicate extraction task
- **WHEN** a user initiates a conversation download while another conversation download is already running on the same store
- **THEN** the system prevents the new task from starting and displays an informational message explaining that a conversation download is already active
