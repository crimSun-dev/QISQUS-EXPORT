## Purpose

Automatically generates Excel workbooks at key pipeline milestones and early terminations, marks workbooks as Complete or Partial, and handles locked-file retry scenarios.

## ADDED Requirements

### Requirement: Intermediate automatic workbook generation
The system SHALL automatically generate an Excel workbook upon the completion of the conversation extraction phase, upon the completion of the broadcast log extraction phase, and whenever an in-progress export stops prematurely due to network errors, token expiration, or user cancellation.

#### Scenario: Workbook generated after conversation download phase finishes
- **WHEN** the conversation download phase completes successfully prior to starting or completing the broadcast log extraction phase
- **THEN** the system immediately builds and saves an Excel workbook reflecting all downloaded conversation data

#### Scenario: Partial workbook generated upon interrupted execution
- **WHEN** an export run halts prematurely due to network disconnection, authentication expiration, or manual cancellation
- **THEN** the system generates an intermediate Excel workbook containing all data safely committed up to the point of interruption

### Requirement: Workbook completeness classification and labeling
The system SHALL evaluate whether an exported workbook contains all enumerated conversation and broadcast records and prominently label the workbook as Complete or Partial in its filename, metadata sheet, and UI representation.

#### Scenario: All records exported without omissions
- **WHEN** a workbook is generated after all enumerated rooms, messages, broadcast jobs, and recipient logs are downloaded with zero missing items
- **THEN** the workbook is classified as Complete and recorded as such in the Export Info sheet and file metadata

#### Scenario: Incomplete export identifies missing components
- **WHEN** a workbook is generated while broadcasts or conversations remain pending, skipped, or failed
- **THEN** the workbook filename includes a partial indicator and the Export Info sheet enumerates the exact counts of missing or pending items

### Requirement: Locked file detection and recovery
The system SHALL detect when an export workbook cannot be saved because the destination file is open in another application (such as Microsoft Excel) and prompt the user to close the file and retry rather than aborting unrecoverably.

#### Scenario: Destination workbook open in external editor
- **WHEN** the system attempts to write the final workbook to disk and encounters an operating system file lock
- **THEN** the system informs the user that the file is open in another application, keeps the temporary intermediate output intact, and provides a retry action
