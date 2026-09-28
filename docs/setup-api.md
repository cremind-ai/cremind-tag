# Setup API (Cremind ↔ browser, Cremind ↔ Connect)

The wire contract for simple device setup ([connect-setup.md](connect-setup.md)).
Three groups: the **profile API** the Settings page and `cremind tags` use (JWT),
the **bootstrap API** Connect uses during a setup session (setup capability),
and the **connector additions** a worker uses once it has its credentials
(`CremindTag`, [connector-api.md](connector-api.md)).

Conventions: timestamps are ISO 8601 UTC with milliseconds; byte strings are
lower-case hex; ids are strings. Errors are Cremind's
`{"error": <code>, "message": <sentence>, "detail": <sentence>, …}`. Every
mutation accepts `Idempotency-Key: <uuid>` (or `"idempotency_key"` in the
body): a retry with the same key returns the first answer (409
`idempotency_key_reused` if the body differs).

## 1. Profile API (JWT, the caller's profile only)

### 1.1 Objects

**Device**

```json
{"id": "d0c1…", "binding_id": "b3a9…", "kind": "gateway|bridge|tag", "name": "Kitchen",
 "device_id": "8a9f…(32 hex)", "short_id": "1A2B3C4D",
 "state": "pairing|paired|ready|offline|recovery_pending|removal_pending|reconciling",
 "paused": false, "generation": 1, "fw": "0.2.0", "board": 19,
 "last_contact_at": "…"|null, "battery_mv": 2900|null, "rssi": -61|null,
 "capacity": {"max_tags": 20, "assigned": 3}|null,           // bridges
 "fontpack_ok": true|null,                                   // bridges
 "bridge_id": "d0c2…"|null,                                  // tags
 "delivery": {"pending_count": 2, "displayed_revision": 17, "desired_revision": 18,
              "clear_required": false, "status": "ok|pending|clear_pending|failed"}|null,  // tags
 "affected_tag_ids": ["d0c3…"]|null}                         // bridges: tags assigned to it
```

`state` `offline` is derived: paired or ready, but no contact for 2 minutes
(gateway: no worker heartbeat; bridge/tag: no report).

**Connection** (one per gateway = one worker)

```json
{"id": "<companion id>", "name": "Desk gateway",
 "status": "setting_up|connected|offline|paused|recovery_pending|removal_pending",
 "paused": false,
 "computer": {"installation_id": "…", "name": "DESKTOP-ABC", "platform": "windows|macos|linux",
              "version": "0.2.0", "last_seen_at": "…"}|null,
 "gateway": Device, "bridges": [Device], "tags": [Device],
 "last_seen_at": "…"|null, "created_at": "…"}
```

**Setup session**

```json
{"id": "…", "operation": "connect_gateway|recover|probe",
 "state": "waiting_for_connect|waiting_for_approval|waiting_for_confirmation|redeeming|connecting|completed|cancelled|expired|failed",
 "expires_at": "…", "created_at": "…",
 "computer": {"installation_id", "name", "platform", "version"}|null,
 "verification_phrase": "amber orbit lantern tidal"|null,
 "gateway": {"device_id", "short_id", "fw", "usable": true}|null,
 "native_approved": false, "browser_confirmed": false,
 "companion_id": null, "operation_id": null,
 "error": {"code", "message"}|null}
```

Order: `waiting_for_connect` → (Connect binds) `waiting_for_approval` → (native
approval with the gateway) `waiting_for_confirmation` → (browser confirms the
computer, gateway and phrase) `redeeming` → (Connect redeems) `connecting` →
(claim done + first heartbeat) `completed`. A `probe` session completes as soon
as Connect binds (it only tells the page which Connect answers on this
computer). Sessions expire 5 minutes after creation unless redeemed.

**Operation** (discovery, pairing, unpair, recovery…)

```json
{"id": "…", "kind": "discovery|pair_bridge|pair_tag|unpair|recover_gateway|release_gateway|claim_gateway",
 "state": "queued|running|succeeded|failed|cancelled|pending_device",
 "stage": "…", "stage_detail": "Waiting for the tag to wake"|null,
 "device": Device|null, "error": {"code", "message"}|null,
 "created_at": "…", "updated_at": "…"}
```

### 1.2 Endpoints

| Method | Path | Body → answer |
|---|---|---|
| GET | `/api/tags/connections` | → `{simple_setup, connections: [Connection], computers: [computer], active: {sessions: [Setup session], operations: [Operation]}}` |
| GET | `/api/tags/connect` | → `{latest_version, downloads: {"windows-x64"|"macos-arm64"|"macos-x64"|"linux-x64-deb"|"linux-x64-tar": {url, sha256?}}}` |
| POST | `/api/tags/setup-sessions` | `{operation, server_url, companion_id?}` → 201 `{session, launch_url}` (`launch_url` only here) |
| GET | `/api/tags/setup-sessions/{id}` | → `{session}` |
| POST | `/api/tags/setup-sessions/{id}/confirm` | `{}` → `{session}`; 409 `not_approved` before native approval |
| DELETE | `/api/tags/setup-sessions/{id}` | → `{session}` (cancelled); 409 `already_redeemed` |
| POST | `/api/tags/discovery` | `{role: bridge|tag, setup_code, gateway_id?, duration_s?}` → 201 `{discovery}` |
| GET | `/api/tags/discovery/{id}` | → `{discovery}` |
| POST | `/api/tags/pairings` | `{discovery_id, candidate_id, name?}` → 201 `{pairing}` |
| GET / DELETE | `/api/tags/pairings/{id}` | → `{pairing}` |
| POST | `/api/tags/devices/{id}/unpair` | `{}` → `{operation, device}` |
| POST | `/api/tags/devices/{id}/pause` / `resume` | `{}` → `{device}` (gateway: the whole connection) |
| POST | `/api/tags/devices/{id}/test` | `{}` → 201 `{delivery}` (a test card) |
| POST | `/api/tags/recoveries` | `{companion_id, server_url}` → 201 `{recovery, session, launch_url}` |
| GET | `/api/tags/recoveries/{id}` | → `{recovery}` |

**Discovery**

```json
{"id": "…", "role": "tag", "short_id": "1A2B3C4D",
 "state": "scanning|found|not_found|failed|cancelled", "started_at": "…", "expires_at": "…",
 "candidates": [{"id": "c1", "gateway_id": "<companion id>", "bridge_id": "<device id>"|null,
                 "bridge_name": "Hall"|null, "rssi": -61, "seen_at": "…",
                 "capacity": {"max_tags": 20, "assigned": 3}|null, "eligible": true, "reason": null|"bridge_full"}],
 "recommended": "c1"|null, "error": {"code", "message"}|null}
```

For a bridge a candidate is the gateway that heard its beacon; for a tag, a
ready bridge of the profile that heard it in setup mode. `recommended` is the
strongest recent signal among eligible candidates.

**Pairing**: an Operation plus `"role"`, `"first_tag": bool` (the profile's
first tag: the page offers "Send this profile's activity", on by default).

**Recovery**: an Operation plus `"companion_id"` and
`"devices": [{"id", "kind", "name", "state": "pending|rekeyed|recovery_pending|failed"}]`.

Error codes: `simple_setup_disabled` (403), `setup_code_invalid`,
`setup_code_wrong_role` (422), `no_gateway`, `gateway_required`,
`gateway_offline`, `no_ready_bridge` (409), `device_owned` (409),
`candidate_not_eligible` (409), `not_found` (404), `session_expired` (410),
`not_approved`, `already_redeemed` (409).

## 2. Bootstrap API `/api/tag-setup/v1/` (Connect, during a session)

`Authorization: CremindSetup <session_id>.<token>` on every call (the token
from the launch link). After `bind`, every call also carries a **proof**: in
the JSON body (`"proof"`) for POST, in `X-Cremind-Connect-Proof` for GET:

```
proof = Ed25519(installation_sk, "cremind-connect/v1/" ‖ action ‖ 0x00 ‖ session_id ‖ 0x00 ‖
                server_nonce ‖ 0x00 ‖ SHA-256(canonical JSON of the body without "proof"))
```

`action` ∈ `bind, poll, approve, redeem, fail`; `bind` uses an empty
`server_nonce` (it is created by the bind). Canonical JSON: keys sorted, no
spaces, UTF-8 (`json.dumps(body, sort_keys=True, separators=(",", ":"))`).

| Method | Path | Body → answer |
|---|---|---|
| POST | `sessions/{id}/bind` | `{installation: {id, public_key, computer, platform, version}, proof}` → `{session: {id, operation, state, expires_at}, server: {installation_id, name, origin, authority_pub, authority_id}, profile: {name, id}, verification_phrase, server_nonce, recover: {companion_id, gateway_device_id, gateway_name}|null}` |
| GET | `sessions/{id}` | → `{state, native_approved, browser_confirmed, error}` |
| POST | `sessions/{id}/approve` | `{gateway: {device_id, ik, fw, proto, board, owner_state, gen, authority_id|null, challenge}, proof}` → `{state}` |
| POST | `sessions/{id}/redeem` | `{idempotency_key, controller_pub, credentials: {hardware_sha256, content_sha256}, proof}` → `{companion_id, credentials: {hardware_id, content_id}, operation_id, profile, server}` |
| POST | `sessions/{id}/fail` | `{code, message, proof}` → `{state}` |

- A second `bind` from the same installation key returns the same answer; from
  another key 409 `already_bound`.
- `approve` refuses `proto < 2` (422 `v1_firmware`), a gateway owned by another
  authority or bound to another profile (409 `device_owned`), a recover session
  whose gateway `device_id` differs (409 `wrong_gateway`).
- `redeem` needs both approvals (409 `not_confirmed`); repeated with the same
  `idempotency_key` it returns the same ids. Cremind stores only the SHA-256 of
  the two credential secrets Connect generated; the credential ids come back.

## 3. Connector additions `/api/tag-connector/v1/` (worker credentials)

| Method | Path | Kind | Body → answer |
|---|---|---|---|
| GET | `whoami` | any | + `api_version: 2, capabilities, mode, worker: {generation, state, paused}` |
| POST | `lease` | hardware | `{}` → `{lease_id, expires_at, ttl_s: 60, renew_s: 20, state, paused, generation}` |
| GET | `state` | hardware | → `{generation, state, paused, bindings: [{device_id, role, hw_id, generation, state, paused}], revoked: [device_id], operations: [{id, kind, state}]}` |
| GET | `operations/{id}` | hardware | → `{operation: {id, kind, state, stage, args, setup_secret|null}}` |
| POST | `operations/{id}/progress` | hardware | `{stage?, detail?, state?, candidates?, device?, result?, error?}` → `{operation}` |
| POST | `grants` | hardware | `{operation_id, op, device_id, role, ik?, gen_from, challenge}` → `{grant, sig, authority_pub}` |
| PUT | `vault/{device_id}` | hardware | `{expected_version|null, stage: pending|committed, generation, state}` → `{version}`; 409 `version_conflict {version}` |
| GET | `vault` | hardware | → `{entries: [{device_id, version, generation, stage, state}]}` (recovering worker only) |

- `state` of a worker: `active`, `paused`, `recovering`, `removing`. A worker
  whose lease has not been renewed for 60 s starts no new work.
- Operations arrive as commands `run_operation {operation_id, kind}` on the
  existing `commands` long-poll; the worker reports the command's result when
  the operation ends.
- `op` ∈ `claim, recover, pair, rekey, release, maint`. Cremind signs only a
  grant that an open operation of this worker needs, for the binding's current
  generation, with `controller` = this worker's controller key.
- `vault` entries are JSON objects of the worker's own design (roots, `mk`,
  assignments, epoch floors, the controller key under `device_id` `"worker"`);
  Cremind encrypts them at rest (connect-setup.md §10) and never interprets them.
