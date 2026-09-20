# Remote skill store

## Why
Agents need one remote catalog with persistent skills and supporting files.
The gateway prerequisites exist, but the real store does not.

## What Changes
- Add an independent `skill-store` Python package and stdio server.
- Support directory, read-only Git, and S3 stores.
- Publish discovery tools, prompts, resources, and eight management actions.
- Persist the registry and skill contents across process restarts.

## Impact
The store owns all skill behavior. The gateway retains authentication and its generic actions page.
