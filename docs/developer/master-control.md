You are implementing Agent Relay v2 as a correctness-first architecture rewrite.

You must follow these rules for the entire job:

- Do not patch around the old mutable-session model.
- Treat the v2 architecture as the source of truth.
- Optimize for correctness, recoverability, and invariant enforcement over speed.
- Keep module boundaries clean: domain logic owns invariants and transitions; CLI is orchestration only.
- Do not skip phases.
- Do not implement future-phase behavior early unless a prior phase absolutely requires a narrow interface or hook for correctness.
- If any part of the requested phase is underspecified, resolve it in favor of safety, explicitness, and recoverability.
- Prefer immutable artifacts, append-only journal events, and rebuildable derived state.
- Any mutating workflow must be safe under interruption, concurrency, and corruption.
- Never silently hide corruption or degraded state.
- Never reintroduce canonical mutable state.json-style patterns.
- At the end of each phase, explicitly state whether the phase is safe to lock before proceeding.

System invariants that must hold throughout the rewrite:

- Sessions are resumable from disk alone
- Journal + immutable objects are canonical
- No required state exists only in memory
- Multi-step operations are atomic or recoverable
- No handoff or checkpoint can be silently overwritten
- Launch and resume validate freshness and integrity
- Lifecycle transitions are enforced by code, not convention
- Corrupted sessions are surfaced explicitly, never silently hidden
- Ownership transfer happens only through explicit resume semantics, not process exit alone
- Refs and derived views are rebuildable caches only, never source of truth

You must execute the entire rewrite using this exact loop for every phase:

1. Produce the implementation plan for that phase only
2. Validate the plan critically and rewrite it if unsafe or incomplete
3. Implement that phase only
4. Verify the implementation against invariants, edge cases, and tests
5. Produce a lock note stating what is now stable and what later phases may rely on
6. Only then proceed to the next phase

For every phase:

- keep outputs concrete
- use exact module/file recommendations when useful
- define schemas, transitions, guards, and failure rules explicitly
- add or update tests as part of implementation
- avoid vague prose
- prefer real engineering decisions over placeholders

Now execute the full rewrite from Phase 1 through Phase 8 in order.

PHASE 1 — Schema v2 domain model, journal, immutable object manifests, and replayer

Phase 1 scope:

- Build schema v2 domain model
- Define session manifest
- Define journal event schema
- Define immutable object manifests
- Define derived view / replayer
- Ship read-only first
- Do not implement mutating commands yet

Target architecture constraints:

- session.json is immutable session metadata
- journal/ is the only canonical evolving state
- objects/ stores immutable checkpoints, handoffs, launch receipts, and captured artifacts
- refs/ and derived/ are rebuildable caches only
- Every journal event includes sequencing and hash chaining
- Every object manifest includes file hashes
- inspect and read paths must rebuild derived state if needed

Expected file structure includes:

- .agent-relay/VERSION
- sessions/<session-id>/session.json
- journal/
- objects/
- refs/
- derived/
- recovery/

For Phase 1, do the following in order:

A. Produce a concrete implementation plan for Phase 1 only.
Include:

- Python module layout
- Domain model types and responsibilities
- JSON schemas for session manifest, journal event, checkpoint manifest, handoff manifest, and launch manifest
- Replayer design: input, validation behavior, output derived view structure
- How refs/ and derived/ are treated as disposable caches
- Read-path behavior for inspect and dashboard
- A test plan for Phase 1
- Explicit non-goals for this phase

Constraints:

- Do not implement locks, tx engine, checkpoint capture, or launch yet
- Do not introduce mutable state as canonical
- Do not rely on old v1 state.json patterns

B. Review the Phase 1 plan critically.
Check:

- Does it enforce the v2 source-of-truth model correctly?
- Does it keep journal canonical and refs/derived disposable?
- Are the schemas complete enough for later phases?
- Is the replayer deterministic and rebuildable from disk alone?
- Are there hidden ambiguities that will create migration pain later?
- Does the plan accidentally preserve old mutable-session assumptions?

If unsafe, incomplete, or ambiguous, rewrite the Phase 1 plan before proceeding.

C. Implement Phase 1 exactly as planned.
Requirements:

- Implement schema v2 domain model
- Implement immutable session manifest
- Implement journal event model
- Implement immutable object manifest models
- Implement derived view replayer
- Support read-only inspect/rebuild behavior where needed
- Add strong validation for schema loading/parsing
- Make derived view rebuildable from journal + objects only
- Surface corrupted inputs explicitly, not silently
- Add tests for parsing, replay, hashing, and derived view rebuild

Constraints:

- Do not implement mutating commands
- Do not implement lock manager or tx engine yet
- Do not add placeholder shortcuts that bypass the architecture
- Keep domain logic separate from CLI rendering/orchestration

D. Verify the Phase 1 implementation.
Check:

- Is journal truly canonical?
- Can derived view be rebuilt from disk alone?
- Are refs/ and derived/ treated as caches only?
- Are schema validations strong enough?
- Are corrupted sessions surfaced explicitly?
- Is any hidden write path or mutable state becoming authoritative?
- Are the tests sufficient?

List:

- invariant violations
- weak points
- missing tests
- required fixes before Phase 1 can be locked

E. Produce a Phase 1 lock note.
Include:

- what is now stable and should not be casually rewritten
- the public/internal interfaces later phases may depend on
- the exact assumptions that Phase 2 may build on
- any intentional caveats deferred to later phases

Only after completing A through E should you continue.

PHASE 2 — Lock manager and transaction engine

Phase 2 scope:

- Add repo/session locking
- Add transaction staging/promote/commit engine
- Define recovery behavior for interrupted transactions
- No mutating command moves to v2 unless it commits through this system

Architecture constraints:

- Every mutating command acquires an exclusive per-session lock
- start, migrate, and repo-wide maintenance may require repo-level lock
- New immutable objects are first staged under recovery/pending-tx/<txid>/
- Hashes are computed before promotion
- Immutable objects are promoted to final object paths
- Exactly one committed journal event makes the operation visible
- refs/ and derived/ are updated only after journal commit
- If interrupted before journal write, the operation never happened
- If interrupted after journal write, recovery rebuilds refs/ and derived/
- Pending tx without committed journal event are abandoned or quarantined

For Phase 2, do the following in order:

A. Produce a concrete implementation plan for Phase 2 only.
Include:

- Locking API design
- Repo lock vs session lock behavior
- Transaction object lifecycle
- Staging, promotion, commit, and cleanup flow
- Recovery rules for partial/interrupted operations
- Failure/error model
- Test plan for concurrent writers, interruption before journal commit, interruption after journal commit, abandoned pending tx, stale refs rebuild

Constraints:

- Do not move checkpoint/failover/launch logic yet beyond infrastructure hooks
- Do not let any mutating flow bypass the tx engine
- Do not make derived caches authoritative

B. Review the Phase 2 plan critically.
Check:

- Does it guarantee atomic-or-recoverable multi-step operations?
- Can same-session concurrency still produce lost updates?
- Is there any path where a partial object becomes visible without a committed event?
- Are lock scopes correct and minimal?
- Are recovery rules explicit enough to prevent limbo states?

If unsafe or incomplete, rewrite the plan.

C. Implement Phase 2 exactly as planned.
Requirements:

- Implement lock manager
- Implement repo/session lock handling
- Implement tx staging/promote/commit engine
- Implement tx recovery support
- Add refs/derived rebuild hooks after committed journal events
- Add robust lock acquisition/release behavior
- Make interruption and cleanup behavior explicit
- Implement durable commit semantics around journal visibility
- Add tests for concurrency, interruption, and recovery
- Ensure read paths can detect stale refs and rebuild

Constraints:

- No mutating command may bypass this infrastructure
- Do not yet fully migrate all commands unless needed for infrastructure testing

D. Verify the Phase 2 implementation.
Check:

- Are multi-step operations atomic or recoverable?
- Can concurrent same-session writes still corrupt state?
- Can a staged object become visible without a committed event?
- Are lock files and pending tx cleaned up or recoverable?
- Can refs/derived always be rebuilt after a crash?
- Are any race conditions remaining?

List:

- invariant violations
- concurrency risks
- interruption risks
- missing tests
- required fixes before Phase 2 can be locked

E. Produce a Phase 2 lock note.
Include:

- stable infrastructure guarantees now available
- required usage contract for future mutating commands
- forbidden patterns that must never be reintroduced
- assumptions later phases can safely rely on

Only after completing A through E should you continue.

PHASE 3 — Immutable checkpoints with guaranteed workspace capture

Phase 3 scope:

- Implement immutable checkpoints
- Implement workspace capture evidence
- Support guaranteed checkpoint completeness
- Require Git-backed repo or explicit full snapshot mode for safe prepare/checkpoint semantics

Architecture constraints:

- A checkpoint is an immutable object with unique checkpoint_id
- It contains manifest, snapshot metadata, summary, and captured artifacts
- Checkpoint becomes real only when its journal event is committed
- Artifacts may include repo-state.json, git-head.txt, workspace.patch, untracked-manifest.json, validation.json
- No partial checkpoint is visible unless journal commit exists

For Phase 3, do the following in order:

A. Produce a concrete implementation plan for Phase 3 only.
Include:

- checkpoint manifest schema details
- workspace capture strategy
- Git-backed mode behavior
- explicit full snapshot mode behavior
- what causes checkpoint/prepare to fail
- how checkpoint summary and validation evidence are stored
- command-facing behavior for checkpoint and prepare
- test plan for Git repo with clean state, dirty repo with patch/untracked changes, non-Git repo without snapshot mode, interrupted checkpoint, corrupted checkpoint artifact hashes

Constraints:

- no mutable checkpoint state
- no silent fallback to incomplete evidence
- no handoff/launch logic yet except dependencies
- fail safe rather than capture partial state

B. Review the Phase 3 plan critically.
Check:

- Does it guarantee checkpoint completeness strongly enough?
- Are non-Git repos handled safely?
- Is there any path where prepare/failover could proceed from weak capture evidence?
- Are all artifacts immutable and hash-verifiable?
- Are failure modes explicit and safe?

If anything is unsafe or underspecified, rewrite the plan.

C. Implement Phase 3 exactly as planned.
Requirements:

- Implement immutable checkpoint object creation
- Implement workspace capture evidence
- Route checkpoint and prepare through tx/journal flow
- Implement validation artifacts and hashes
- Add checkpoint creation domain logic
- Support Git-backed evidence capture
- Support explicit snapshot mode if part of the plan
- Ensure journal commit is the visibility boundary
- Add thorough tests for checkpoint completeness and interruption

Constraints:

- every checkpoint must be immutable
- no silent partial capture
- use Phase 2 transaction/lock infrastructure
- do not implement handoff or launch semantics beyond necessary references
- CLI should orchestrate, not own invariants

D. Verify the Phase 3 implementation.
Check:

- Are checkpoints immutable and uniquely identifiable?
- Can checkpoint creation survive interruption without partial visibility?
- Are all required artifacts hash-verified?
- Can prepare still happen with insufficient evidence anywhere?
- Are non-Git repos handled safely?
- Are tests strong enough?

List:

- invariant violations
- weak capture paths
- missing tests
- fixes required before locking Phase 3

E. Produce a Phase 3 lock note.
Include:

- what checkpoint completeness now means in code
- what later phases can assume about checkpoint objects
- what failure behavior is intentional
- what patterns must not be weakened later

Only after completing A through E should you continue.

PHASE 4 — Immutable handoffs, launch receipts, and resume

Phase 4 scope:

- Implement immutable handoff objects
- Implement handoff_id
- Implement launch receipts and logs
- Implement resume semantics
- Remove any path where agent ownership changes on subprocess exit alone

Architecture constraints:

- failover creates a new immutable handoff every time
- no path reuse
- handoff includes manifest.json, packet.md, packet.sha256, launch-spec.json
- launch operates by handoff_id, never latest file path
- launch validates freshness and integrity
- launch.started and launch.finished are journal events
- interrupted launch must be recoverable, never stuck in limbo
- ownership transfer happens only on resume.received

For Phase 4, do the following in order:

A. Produce a concrete implementation plan for Phase 4 only.
Include:

- handoff manifest schema
- launch receipt schema
- resume event payload and semantics
- failover flow
- launch flow
- resume flow
- validation rules for stale checkpoint rejection, packet existence/hash checks, prepared-handoff reference checks, superseded handoff rejection
- logging/artifact strategy for stdout/stderr
- test plan for same-target repeated failovers, missing packet, stale handoff after newer checkpoint, interrupted launch, resume against invalid handoff, logs captured on failure

Constraints:

- no mutable resume/<target>.md path semantics
- no “latest handoff wins” shortcut
- no ownership transfer on process exit
- use tx/journal infrastructure

B. Review the Phase 4 plan critically.
Check:

- Does it fully eliminate overwritten packet paths?
- Can stale or missing packets still be launched?
- Can launch end in limbo after interruption?
- Is ownership transfer tied only to resume?
- Are handoff and launch artifacts immutable and auditable?

If anything is unsafe or too permissive, rewrite the plan.

C. Implement Phase 4 exactly as planned.
Requirements:

- Implement immutable handoff objects
- Implement failover by handoff_id
- Implement launch receipts/log capture
- Implement launch started/finished events
- Implement interrupted launch recovery behavior
- Implement resume.received semantics
- Capture launch stdout/stderr into immutable artifacts
- Surface failure information clearly
- Add tests for stale/missing/corrupt handoff behavior

Constraints:

- ownership transfer must happen only on resume
- launch must validate packet existence, hash, freshness, and prepared state
- same-target repeated failovers must remain safe
- all writes must use tx/journal infrastructure
- no fallback to old mutable handoff behavior

D. Verify the Phase 4 implementation.
Check:

- Can repeated failovers to the same target overwrite history anywhere?
- Can launch proceed with stale or missing packet state?
- Can interrupted launch leave the session in launching forever?
- Does resume alone transfer ownership?
- Are logs and receipts preserved as immutable artifacts?
- Are tests sufficient?

List:

- invariant violations
- stale-handoff risks
- interruption/recovery risks
- missing tests
- required fixes before locking Phase 4

E. Produce a Phase 4 lock note.
Include:

- stable semantics of failover, launch, and resume
- what later phases can rely on
- what behavior is now forbidden permanently
- any intentionally deferred improvements

Only after completing A through E should you continue.

PHASE 5 — Central lifecycle state machine

Phase 5 scope:

- Implement a central lifecycle state machine
- Separate relay phase from task_status
- Enforce command guards and transition rules in one domain layer
- Ensure CLI cannot mutate phase directly

Architecture constraints:

- phase values include active, paused, ready_for_handoff, launching, awaiting_resume, completed
- task_status values inside checkpoints include working, blocked, done
- command-specific preconditions and postconditions must be enforced centrally
- invalid transitions must fail explicitly

For Phase 5, do the following in order:

A. Produce a concrete implementation plan for Phase 5 only.
Include:

- state machine model
- all valid transitions
- command guard rules for start, checkpoint, prepare, failover, launch, resume, inspect, repair
- how journal events interact with phase changes
- how task_status is recorded without conflating it with relay phase
- test plan for valid and invalid transitions

Constraints:

- no transition logic scattered across CLI commands
- no blind status assignment from flags
- invalid state must be explicit and debuggable

B. Review the Phase 5 plan critically.
Check:

- Are relay phase and task_status clearly separated?
- Are all command guards explicit?
- Can a completed session still prepare or launch a handoff?
- Can invalid transitions still slip through indirect paths?
- Is the state machine centralized enough to remain maintainable?

If weak or ambiguous, rewrite the plan.

C. Implement Phase 5 exactly as planned.
Requirements:

- Implement central lifecycle state machine
- Implement command guards and transition validation
- Separate phase vs task_status
- Refactor CLI orchestration so it no longer mutates state directly
- Add a single authoritative transition layer
- Add tests for valid and invalid transitions
- Preserve clarity and maintainability

Constraints:

- all transitions must be enforced by domain logic
- invalid transitions must fail clearly
- do not spread guard logic across commands
- use existing journal/tx infrastructure

D. Verify the Phase 5 implementation.
Check:

- Is lifecycle enforcement centralized?
- Can commands still mutate phase/status directly anywhere?
- Are invalid transitions rejected consistently?
- Are phase and task_status semantically distinct in code and storage?
- Are tests sufficient to prevent regressions?

List:

- invariant violations
- leaked transition logic
- missing tests
- required fixes before locking Phase 5

E. Produce a Phase 5 lock note.
Include:

- final lifecycle semantics
- stable transition guarantees
- what other code is forbidden from doing directly
- assumptions later phases can rely on

Only after completing A through E should you continue.

PHASE 6 — Integrity validation, degraded-session surfacing, and repair

Phase 6 scope:

- Implement integrity validation
- Surface degraded/corrupted sessions explicitly
- Implement repair flows
- Ensure broken sessions are never silently hidden

Architecture constraints:

- every object file gets a sha256 in its manifest
- every journal event includes prev_event_hash and event_hash
- inspect must report health, last_valid_event, broken_paths, suggested repair
- repair --rebuild-view regenerates refs/ and derived/ from journal
- repair --rollback-pending removes abandoned tx staging
- repair --promote-last-good recovers from last verified event and quarantines corrupted tail files
- mutating commands should block when integrity requires repair

For Phase 6, do the following in order:

A. Produce a concrete implementation plan for Phase 6 only.
Include:

- integrity checker design
- health model
- degraded/corrupt session surfacing rules
- repair command behavior
- quarantine behavior
- read-path vs write-path behavior under corruption
- test plan for corrupt journal event, broken journal hash chain, missing object, object hash mismatch, stale refs/derived caches, repair success/failure flows

Constraints:

- do not silently skip broken sessions
- do not permit unsafe mutation through corruption
- keep repair explicit and auditable

B. Review the Phase 6 plan critically.
Check:

- Are corrupted sessions surfaced instead of hidden?
- Can the system recover to last valid event safely?
- Are repair behaviors explicit and auditable?
- Can mutation still happen through degraded state accidentally?
- Are quarantine semantics safe and understandable?

If unsafe or incomplete, rewrite the plan.

C. Implement Phase 6 exactly as planned.
Requirements:

- Implement integrity validation
- Implement health surfacing
- Implement degraded-session handling
- Implement repair flows
- Implement quarantine behavior
- Use journal/object hashes as the basis for trust
- Ensure inspect/dashboard/read paths surface health clearly
- Add tests for corruption and repair behavior

Constraints:

- corrupted sessions must be visible
- mutating commands must block when repair is required
- repairs must be explicit and auditable

D. Verify the Phase 6 implementation.
Check:

- Are corrupt sessions explicitly surfaced?
- Can the system recover to the last good verified event?
- Are broken refs/derived caches rebuildable?
- Are mutating commands blocked correctly under degraded health?
- Are repair flows safe and auditable?
- Are any silent failure paths still present?

List:

- invariant violations
- recovery risks
- silent-failure risks
- missing tests
- required fixes before locking Phase 6

E. Produce a Phase 6 lock note.
Include:

- stable health and repair semantics
- what corrupted-session handling now guarantees
- forbidden silent-failure behaviors
- assumptions later phases may rely on

Only after completing A through E should you continue.

PHASE 7 — Adapter updates and safe launch semantics

Phase 7 scope:

- Update adapters / built-in agent profiles
- Make launch semantics safe by default
- Ensure packet-aware defaults where supported
- Ensure ownership transfer still happens only on resume

Architecture constraints:

- built-in launch profiles should consume the resume packet where supported
- if a template does not consume {resume_path} or equivalent packet input, launch --execute should warn or refuse according to safe policy
- dispatching a subprocess is not the same as successful handoff
- resume remains the transfer-of-ownership event

For Phase 7, do the following in order:

A. Produce a concrete implementation plan for Phase 7 only.
Include:

- adapter/profile model changes
- launch template validation rules
- safe default behavior for built-in profiles
- warning/refusal policy for templates that ignore packet input
- README/help/output changes needed to reflect safe semantics
- test plan for packet-aware profile, non-packet-aware profile, launch warning/refusal behavior, and preserved resume semantics

Constraints:

- do not make subprocess exit equal handoff success
- do not let unsafe built-in defaults remain
- keep adapter logic separate from lifecycle/domain invariants

B. Review the Phase 7 plan critically.
Check:

- Are built-in defaults safe?
- Can launch --execute still misleadingly appear successful without using the packet?
- Are warning/refusal semantics clear?
- Is ownership transfer still isolated to resume?
- Are docs/help aligned with actual behavior?

If misleading or unsafe, rewrite the plan.

C. Implement Phase 7 exactly as planned.
Requirements:

- update adapters/profiles
- validate packet-aware launch templates
- add safe warning/refusal behavior
- update user-facing docs/help where necessary
- implement profile validation
- update built-in agent profiles
- add tests for packet-aware and unsafe-template cases
- update README/help text to match safe semantics

Constraints:

- unsafe template defaults must not remain
- subprocess launch must not equal ownership transfer
- preserve domain invariants
- tests and docs must reflect actual behavior

D. Verify the Phase 7 implementation.
Check:

- Do built-in profiles safely consume the resume packet where supported?
- Can unsafe templates still execute without warning/refusal?
- Can users still confuse process launch with successful handoff?
- Are docs/help aligned with actual behavior?
- Are tests sufficient?

List:

- invariant or UX violations
- misleading behaviors
- missing tests/docs
- required fixes before locking Phase 7

E. Produce a Phase 7 lock note.
Include:

- final safe adapter semantics
- user-visible launch behavior guarantees
- forbidden misleading behaviors
- assumptions Phase 8 can rely on

Only after completing A through E should you continue.

PHASE 8 — Hardening tests and migration

Phase 8 scope:

- Add hardening tests
- Implement migration from v1 to v2
- Preserve v1 safely
- Ensure degraded legacy sessions migrate without data loss

Architecture constraints:

- schema bump to 2
- v2 CLI supports read-only inspection of legacy v1 sessions
- mutating any v1 session requires migration first
- migrate runs under repo lock and writes v2 transactionally
- clean v1 sessions are imported into immutable checkpoint/handoff objects and journal events
- inconsistent v1 sessions are migrated as health=degraded
- raw legacy files are preserved under legacy-v1/
- backward compatibility is read-only, not write-compatible

Required hardening test areas:

- concurrent writers
- SIGINT during every mutating command
- same-target repeated failovers
- missing packet
- corrupt journal
- corrupt object hash
- migration of degraded sessions

For Phase 8, do the following in order:

A. Produce a concrete implementation plan for Phase 8 only.
Include:

- migration architecture
- v1 detection and read-only behavior
- migrate command semantics
- import strategy for clean sessions
- degraded migration strategy
- hardening test matrix
- CI/test organization strategy
- rollout strategy

Constraints:

- do not mutate legacy sessions in place unsafely
- do not promise write compatibility to v1
- preserve raw legacy data until v2 commit completes

B. Review the Phase 8 plan critically.
Check:

- Is migration transactionally safe?
- Are legacy sessions preserved?
- Can degraded v1 sessions still be surfaced and imported safely?
- Are the hardening tests broad enough to challenge the real invariants?
- Is read-only compatibility clear and safe?

If weak or incomplete, rewrite the plan.

C. Implement Phase 8 exactly as planned.
Requirements:

- implement v1 detection and read-only support
- implement migration command and import flow
- implement degraded migration handling
- preserve legacy raw files
- add the hardening tests defined in the plan
- keep behavior auditable and explicit

Constraints:

- no unsafe in-place mutation of v1 data
- migration must use repo lock and tx/journal semantics
- preserve raw legacy files until commit is complete
- hardening tests must target real failure modes

D. Verify the Phase 8 implementation.
Check:

- Can v1 sessions be inspected safely in read-only mode?
- Is migration transactional and recoverable?
- Are degraded sessions preserved and surfaced?
- Does the hardening suite actually test concurrency, interruption, corruption, stale handoffs, and degraded migration?
- Are there remaining release blockers?

List:

- invariant violations
- migration risks
- hardening test gaps
- remaining blockers before release

E. Produce a Phase 8 lock note.
Also produce:

- a release-readiness assessment for Agent Relay v2
- a list of any remaining blockers
- a list of post-v1 improvements intentionally deferred

Only after completing A through E should you continue.

FINAL SYSTEM-WIDE RELEASE AUDIT

After all phases are complete, run one final adversarial audit.

System expectations:

- journal + immutable objects are canonical
- refs/derived are rebuildable caches
- checkpoints, handoffs, launches are immutable and uniquely identifiable
- all mutating operations are atomic or recoverable
- locking prevents same-session lost updates
- stale, missing, or superseded handoffs are rejected
- ownership transfers only on resume
- lifecycle transitions are enforced centrally
- integrity validation surfaces degraded sessions explicitly
- repair and migration paths are available and safe

Final audit tasks:

1. Simulate the full lifecycle:
   - start
   - checkpoint
   - prepare
   - failover
   - launch
   - resume
   - repeat
   - inspect
2. Stress test:
   - same-session concurrency
   - SIGINT during each mutating command
   - corrupt journal tail
   - corrupt object hash
   - missing packet
   - stale handoff after newer checkpoint
   - degraded v1 migration
3. Validate invariants one by one
4. Identify any remaining P0/P1 issues
5. Give a final ship / no-ship verdict

Output requirements for the entire job:

- For each phase, clearly label:
  - Plan
  - Plan Validation
  - Implementation
  - Verification
  - Lock Note
- Be concrete, not abstract
- Use exact structures and flows where possible
- Prefer code-level decisions over commentary
- If you find a reason not to proceed at any phase, say so explicitly and explain what must be fixed before continuing
- At the end, provide the final release verdict and remaining blockers, if any

Now begin with Phase 1 Plan.
