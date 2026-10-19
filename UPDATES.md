# Agent Updates — administrator guide

Centralized, signed, outbound-only updates for CSP agents. The server **distributes** releases but cannot **forge** them:
only the holder of the private signing key can create a release an agent will install.

## 1. One-time setup (the signing key)

```
python tools/release_tool.py keygen --out D:\csp-signing-keys
```

* Writes `release_signing_private.key` (**keep it offline/backed up; never in the repo, never on the server or any CSP PC** —
  `keygen` refuses to write inside a git repository) and `release_public_keys.json`.
* Copy `release_public_keys.json` into **both** `agent/` (it ships to every PC) and `server/` (so uploads are checked).
  Alternatively point the server at it: `CSP_MONITOR_RELEASE_PUBKEYS=/path/release_public_keys.json`.
* Release files are stored in `CSP_MONITOR_RELEASE_DIR` (default next to the database). Put it on the same volume as the DB.
* Until a public key is installed the server **rejects every upload** and agents **reject every release** (safe default).
* Key rotation: add the new key to the `keys` list (keep the old one until every PC has updated), release as usual, later remove the old key.

## 2. Build and sign a release

```
python tools/release_tool.py build --private-key D:\csp-signing-keys\release_signing_private.key ^
       --version 2.1.0 --notes "What changed" --out D:\releases
```

Produces `csp-agent-2.1.0.zip`, `.manifest.json`, `.manifest.sig`. The tool stamps the version and a **sequence number**
(always increasing, kept in `release_state.json`) into the package. Verify before uploading:

```
python tools/release_tool.py verify --package P.zip --manifest M.json --signature M.sig
```

Only files with allowed extensions are packaged; a PC's own data (`agent_config.json`, history, state, lock) is **never** replaced.

## 3. Upload, approve, deploy (dashboard → **Agent Updates**, admin only, CSRF-protected, every action audited)

1. **Upload release** — all three files. The server re-verifies signature, hashes and package contents; a bad one is refused with a plain reason.
2. **Approve** the release (uploaded ≠ deployable).
3. **Create rollout** — choose individual CSPs or a group; kind **pilot** first. Only *compatible* CSPs (update-capable agent, sequence below the release, `min_agent_sequence` met) can be chosen.
4. Watch the per-CSP table. Then **Expand** the rollout to more CSPs, or **Pause / Resume / Cancel** pending ones.
5. **Retry** a failed CSP (increments the attempt). Duplicate clicks never duplicate work (one deployment per CSP per release).

Status labels: Pending → Assigned → Downloading → Verifying → Installing → Restarting → **Successful** / Failed / Rollback in progress / Rolled back / Cancelled; **Offline** is shown over any unfinished state when the PC is not reporting.
**Successful is only ever set when the NEW version sends a healthy heartbeat reporting the matching version** — never because a package was assigned or downloaded.

## 4. What happens on a CSP PC (all outbound; nothing connects in)

1. A normal heartbeat reply includes `update` only if this CSP has a pending deployment.
2. The agent fetches the signed manifest, checks signature, product, platform, **sequence (no downgrades)**, compatibility and free disk.
3. Downloads over HTTPS with resume (Range); verifies size, SHA-256 and **every file** against the signed list.
4. Stages into `update_staging/` and compile-checks the Python; the live install is untouched so far.
5. Hands over to `apply_update.py` (a separate, stdlib-only process; **no elevation, no server-supplied command is ever run**). It backs up replaced files, swaps them, starts the new agent and waits for it to prove itself.
6. Healthy → reported; unhealthy/crash/timeout → **automatic rollback** to the backup and restart of the previous version; power-cut mid-update → restored at next start.

## 5. Rolling back a bad release

```
python tools/release_tool.py rollback --private-key ... --base-package csp-agent-2.0.0.zip ^
       --base-manifest csp-agent-2.0.0.manifest.json --version 2.1.1 --notes "Rollback to 2.0.0 code" --out D:\releases
```

Agents refuse to go backwards, so a rollback is a **new, higher-sequence, re-signed release containing the old known-good code**.
Upload → approve → deploy it exactly like any release. (Revoke the bad release first so nothing new picks it up.)

## 6. Existing CSPs that cannot update themselves yet (one-time bootstrap)

PCs running an agent from before this feature have **no updater and cannot update themselves.** The dashboard shows them as
*needs one-time upgrade* and they are never targeted. They keep reporting normally with their existing API key.

To upgrade one: send the CSP the generic installer (`tools/make_installer_zip.py`, see `INSTALLER.md`) and ask them to run
`SETUP.bat` and enter their name and code. On a PC that already has an agent the installer **keeps its identity, API key,
device choices, hardware identities and history** and does **not** enroll again. From then on it updates remotely.
Do **not** rotate keys for this. Do the pilot on one or two friendly CSPs first.

## 7. Safety properties (each is covered by an automated test)

Signature must verify against a trusted key • tampered manifest/package refused • unknown key refused • sequence must rise (server and agent) •
zip-slip / zip-bomb / disallowed file types refused • protected files never replaced • failed/crashed update rolls back •
duplicate commands are idempotent • interrupted download resumes • offline PCs simply wait • every admin action is audit-logged.
