## ADDED Requirements

### Requirement: Git commands start no background maintenance
The Git backend SHALL run every Git command with automatic maintenance and automatic garbage collection disabled.
When a Git command of the backend completes, no maintenance or garbage collection process that the command started SHALL remain.

#### Scenario: Refresh starts no maintenance
- **WHEN** a Git store refreshes its cache
- **THEN** Git starts no maintenance command
- **AND** Git starts no garbage collection command
