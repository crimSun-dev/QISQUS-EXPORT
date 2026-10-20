## Purpose

Provides an accessible, responsive desktop graphical interface for non-technical users to monitor local data safety, execute tasks, configure export parameters, manage workbook files, and manage application credentials.

## ADDED Requirements

### Requirement: Startup data safety dashboard
The system SHALL inspect the local database upon application startup without requiring internet connectivity and display a plain-language summary of data safety, showing total, completed, pending, and skipped counts for conversations and broadcasts alongside an actionable verdict.

#### Scenario: Fully downloaded store presents safe verdict
- **WHEN** the application opens and discovers all enumerated conversations and broadcast campaigns have been fetched
- **THEN** the dashboard displays a clear success message such as "All 6,826 conversations downloaded" with data store size and last completed timestamp

#### Scenario: Interrupted store prompts with direct continuation action
- **WHEN** the application opens and detects an unfinished previous extraction run
- **THEN** the dashboard presents an alert detailing the pending item counts and provides a single primary button to continue where it stopped

#### Scenario: Unclean shutdown triggers integrity check
- **WHEN** the application detects that the previous session terminated abnormally (such as after power loss or forced process termination)
- **THEN** the application performs an automatic data store integrity check and alerts the user of database health before enabling new operations

### Requirement: Interactive credential setup and health monitoring
The system SHALL provide a step-by-step graphical setup wizard for entering and validating Qiscus credentials, continuously verify token health, and prompt the user with a streamlined refresh flow when an active task encounters an expired authentication token.

#### Scenario: Guided setup wizard saves credentials
- **WHEN** a user completes the setup wizard by pasting required Qiscus dashboard credentials
- **THEN** the system validates credentials against the authentication endpoint, updates the local environment configuration, and confirms successful login

#### Scenario: Expired token detection and one-click resumption
- **WHEN** a running task encounters an authentication failure due to expired credentials
- **THEN** the interface prompts the user to refresh their login and presents a one-click button to resume the paused task immediately after validation succeeds

### Requirement: Files panel and artifact management
The system SHALL provide a dedicated files panel listing all generated Excel workbooks in the exports directory, displaying their creation date, file size, record counts, and Complete or Partial status badge, with direct operating system integration buttons.

#### Scenario: Viewing export files with status badges and counts
- **WHEN** the user views the files panel
- **THEN** the system lists each workbook with date, file size, conversation count, message count, campaign count, and a Complete or Partial badge

#### Scenario: Opening file and containing directory in Windows Explorer
- **WHEN** the user selects "Open file location" for a workbook in the files panel
- **THEN** the system opens Windows File Explorer with the specific workbook file selected

#### Scenario: Safe cleanup of old or partial workbooks
- **WHEN** the user clicks the "Delete old" action
- **THEN** the system prompts for confirmation and removes obsolete partial files without deleting the user's latest complete exports

### Requirement: Single-instance lifecycle and safe exit handling
The system SHALL restrict application execution to a single running instance per data store and intercept window closure requests when background tasks are active to prevent data loss.

#### Scenario: Launching a secondary instance
- **WHEN** a user attempts to launch a second instance of the GUI while one is already running
- **THEN** the second instance terminates cleanly and signals the existing application window to come to the foreground

#### Scenario: Attempting to close window while tasks are running
- **WHEN** the user clicks the window close button while extraction or export tasks are active
- **THEN** the system displays a confirmation dialog offering choices to continue running in background, safely stop tasks, or cancel closure

### Requirement: Plain-language configuration and diagnostic support
The system SHALL expose extraction options using standard form controls with safe defaults, and provide a one-click diagnostics export that collects system and task logs with all authentication tokens strictly redacted.

#### Scenario: Configuring extraction parameters without command-line arguments
- **WHEN** the user adjusts settings such as date ranges, inclusion of broadcast logs, or update-only mode
- **THEN** the settings are applied to subsequent task runs with advanced rate-limiting controls collapsed under safe default values

#### Scenario: Copying sanitized diagnostic details for technical support
- **WHEN** the user clicks "Copy diagnostics"
- **THEN** the system compiles application version information, operating system details, recent task log lines, and store summary counts with all authentication secrets redacted, and copies the text to the clipboard
