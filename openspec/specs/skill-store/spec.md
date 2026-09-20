# skill-store Specification

## Purpose
TBD - created by archiving change remote-skill-store. Update Purpose after archive.
## Requirements
### Requirement: Persistent multi-store catalog
The server SHALL read directory, Git, and S3 stores through one backend interface.
The server SHALL persist registry updates atomically with mode 0600.
The server SHALL permit at most one writable store and SHALL reject writable Git stores.

#### Scenario: Restart preserves state
- **WHEN** the server restarts after a successful write
- **THEN** it restores the writable selection and identical skill contents

### Requirement: Safe publication and migration
The server SHALL publish immutable catalog snapshots after durable writes.
The server SHALL verify target bytes before deleting a migration source.
The server SHALL preserve Git sources and reject overlapping storage aliases.

#### Scenario: Migration verification fails
- **WHEN** target verification fails
- **THEN** the source remains intact

### Requirement: Remote discovery
The server SHALL expose tools, qualified prompts, supporting resources, and live instructions.
The server SHALL cap instructions at 8192 bytes and provide paginated discovery.

#### Scenario: Ambiguous short name
- **WHEN** two stores contain the requested short name
- **THEN** the server returns an error with both qualified names

### Requirement: Runtime administration
The server SHALL publish eight flat actions with password metadata for credentials.
The server SHALL exclude credentials from results and diagnostics.

#### Scenario: Add a store without restart
- **WHEN** an administrator adds a valid store
- **THEN** the catalog reflects its skills without restarting the server

