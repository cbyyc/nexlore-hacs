# NEXLORE Home Assistant integration

This package exposes one selected NEXLORE space as a standard Home Assistant
to-do list entity.

## Distribution layout

This directory is an isolated HACS integration repository root:

- custom_components/nexlore contains the complete Home Assistant integration.
- hacs.json and this README are at the distribution root.

This package is maintained in a private Gitea repository as the canonical
source. A public GitHub push mirror will be used for HACS, which requires
public GitHub repositories. The GitHub mirror link will be added here once its
repository has been created.

## HACS installation after distribution

1. In Home Assistant, open HACS, choose Custom repositories, add the public
   GitHub mirror
   as an Integration, and install NEXLORE.
2. Restart Home Assistant, then add NEXLORE from Settings > Devices & services
   > Add integration.
3. Enter the NEXLORE base URL, a write-level API token, and a writable space.

The integration can also be installed manually by copying
custom_components/nexlore into the Home Assistant configuration directory's
custom_components folder and restarting Home Assistant.

## Setup requirements

Create a NEXLORE API token with write permission and access to the desired
space. The integration checks the token with GET /api/v1/me and discovers spaces
with GET /api/v1/spaces. It offers only spaces whose role is write or manage.

The URL is the server origin, for example https://notes.example.com. The token
is stored in the Home Assistant config entry. The integration never logs it.

## Supported behavior

- Polls GET /api/v1/tasks?space=<space>&status=all every five minutes.
- Creates tasks through POST /api/v1/inbox and reconciles the returned inbox
  path with a fresh task-list read.
- Completes and reopens tasks through POST /api/v1/tasks/complete.
- Refreshes the list after Home Assistant writes and imports NEXLORE-side
  completion, reopening, and task changes on the next poll.
- Displays NEXLORE due dates when present, but does not let Home Assistant set
  or edit them.
- Excludes cancelled tasks from the Home Assistant list.
- Supports creation and status changes only. It does not support deleting,
  renaming, descriptions, moving, or setting due dates.

The generic Home Assistant update feature is needed to support completion and
reopening. The entity rejects title, description, or due-date edits rather than
silently ignoring them.

## Identity and safe retries

NEXLORE returns current path, line, raw task text, and file hash as write
locators. It does not provide a permanent task ID. The integration persists a
mapping from Home Assistant item UIDs to the latest locator fingerprints and
uses path plus exact or status-normalized task text to keep UIDs stable when
lines move or a task is completed/reopened. It never uses a title alone. If
multiple identical tasks in one note cannot be distinguished after a change,
the integration does not guess; an item may receive a new UID or a status write
may fail with a conflict. External title or note-path changes also cannot be
linked safely to the old item without a permanent NEXLORE task ID, so they may
appear with a new UID.

Inbox creation has no idempotency key. Before posting, the integration saves a
pending intent and a baseline. If the response is lost, it reads NEXLORE and
accepts the create only when exactly one new matching task can be identified in
the inbox. It never automatically repeats an uncertain create. If the result
remains ambiguous, it reports that uncertainty and blocks another create with
the same title until a later refresh reconciles it or the user confirms the
inbox state.

The persistent identity store contains note paths and hashes of task locators,
not the API token or copied task text. The API token is kept in Home Assistant's
config-entry storage.

## API reference

See the [NEXLORE API documentation](https://github.com/DerKezorm/nexlore/blob/main/docs/api.md).
