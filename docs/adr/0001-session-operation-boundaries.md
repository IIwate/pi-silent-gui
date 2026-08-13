# ADR 0001: Session operation and trust boundaries

Status: Accepted — caller-visible ceremony superseded by ADR 0002; internal queue, owner-loss, and Job cleanup still apply.

## Context

A session spans a Pi runtime, short-lived Python helpers, one persistent broker, a private desktop, and a Job Object. Failures previously crossed those boundaries through mutable files, inherited process state, and concurrent helper calls.

## Decision

- The creating Pi process remains the owner. The broker keeps watching that owner after a natural target exit; owner-loss terminates the Job and removes state. A later runtime may remove stale state but never resume GUI operations.
- Each runtime capability derives the Job name. Registered cleanup pins the broker process identity while opening the Job so a recycled name cannot authorize a different object. A pinned broker that misses its stop deadline may be force-terminated; disk-only orphan identities never receive that privilege.
- Message and capture operations are serialized per session. Kill closes admission and rejects active and queued callers through one queue signal before cleanup starts. Shared cleanup has an internal deadline; each caller observes cancellation independently. A failed kill remains terminal for GUI operations but may be retried.
- Capture may bind a message to the returned `hwnd`. The backend revalidates that HWND against current Job PIDs before dispatch and before every batch action.
- Backend requests use stdin only and are limited to 1 MiB. Stdout responses use one versioned, command-bound protocol frame. Helper deadlines bound the caller-facing Promise instead of trusting process close events.
- Target environments are clean by default. `clean_env: false` is an explicit compatibility escape hatch and may expose Pi process variables.
- Capture output never overwrites by default. Explicit overwrite uses a same-directory temporary file and atomic replacement; concurrent overwrite of the same normalized path is serialized across sessions. The caller supplies a random internal pending name and retries exact-path deletion after helper termination. Output parent chains reject reparse points under the documented same-user trust boundary.
- Session deletion preserves `session.json` until all other entries are gone. Pre-root error state may be removed only after the Job and broker identities are confirmed absent.
- Capture clears the GDI surface before `PrintWindow`, limits both total pixels and scanline bytes, and streams bounded IDAT chunks.

## Consequences

- Changing the environment and overwrite defaults is a breaking pre-1.0 change.
- Kill may cancel a message or capture that was already running.
- HWND plus Job membership cannot distinguish handle reuse by the same process. Revisit only if a real target demonstrates that failure mode.
- Private desktops and environment cleaning do not create a sandbox. Same-user hostile processes remain outside the threat model.
- Real audio and system-GUI compatibility gates require an interactive Windows runner with an audio endpoint; hosted headless CI cannot replace that evidence.
