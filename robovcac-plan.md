# Plan v2: eufy X8 Pro (reports as **T2266**) → Home Assistant via damacus/robovac

Prepared: 2026-07-03 · Supersedes v1 after log evidence
Error observed: `custom_components/robovac/vacuum.py:667 — Model T2266 is not supported`

---

## Root cause — confirmed

The Eufy cloud API reports your robot's model code as **T2266**, and **T2266 does not exist in the integration's `ROBOVAC_MODELS` registry** (`custom_components/robovac/vacuums/__init__.py`). The integration aborts at model lookup — it never even attempts a local connection. So this is not a protocol, network, or key problem (yet); it's a missing model definition.

**Why this is very fixable:** T2266 and T2276 are the *same robot*. eufy's own user guide covers both under "X8 Pro Series (For T2266 & T2276)" — T2266 ships with a plain Charging Base, T2276 with the Self-Empty Station. Only T2276 was ever added to the integration (PR #341, Feb 2026, live-tested on hardware: protocol 3.5, human-readable DPS 1–142). The T2276 config should apply to T2266 nearly 1:1, minus SES-only bits (dust collection, DPS 126).

> Note: you referred to the device as T2276. If the eufy app / sticker under the robot says "X8 Pro SES" while the API returns T2266, mention that in the eventual PR — useful signal for the maintainer. Either way the fix is identical: support whatever string the API returns.

**Version prerequisite still applies:** the protocol-3.5 machinery (session-key negotiation, AES-GCM, no-heartbeat handling) that the X8 Pro needs only exists from **v2.2.0-beta.1**; take **v2.4.3** (first stable containing it). All earlier 2.x tags were HACS-hidden pre-releases. Verify your installed tag in **HACS** (the HA integration page shows the same manifest version for beta and stable — issue #542).

---

## Phase A — Quick unblock today (~15 min, local hack)

Goal: get the vacuum working *now* and simultaneously prove the T2276 config fits your hardware.

1. Confirm version ≥ v2.4.3 in HACS (upgrade + **remove/re-add the integration entry** if you crossed the v2.0.0 boundary).
2. Edit `/config/custom_components/robovac/vacuums/__init__.py` (Samba/SSH add-on):

   ```python
   from .T2276 import T2276   # already imported

   ROBOVAC_MODELS: Dict[str, Type[RobovacModelDetails]] = {
       # ... existing entries ...
       "T2266": T2276,  # TEMP alias: X8 Pro (charging base) — same robot as X8 Pro SES
   }
   ```

3. Restart HA. Model lookup now succeeds → integration proceeds to the protocol-3.5 connection.
4. Smoke test (Developer Tools → Actions): `vacuum.start`, `vacuum.return_to_base`, `vacuum.locate`, and watch `vacuum.<name>` state through a full cycle.
5. Log every mismatch (fan-speed names, status strings, missing sensors) — that's your Phase C diff list.

⚠️ Caveat: any HACS update overwrites this edit. It's a bridge to Phase C, not the fix.

---

## Phase B — Ground truth with tinytuya (~30 min) — now doubles as PR evidence

1. **devId + localKey:** after a successful config-flow login they're in the config entry (device → *Download diagnostics*, or `.storage/core.config_entries`). Fallbacks: [Rjevski/eufy-clean-local-key-grabber](https://github.com/Rjevski/eufy-clean-local-key-grabber), [markbajaj/eufy-device-id-python](https://github.com/markbajaj/eufy-device-id-python). Key **rotates** on Wi-Fi re-pairing → re-add integration to refresh.
2. From a machine on the same network, **eufy app force-closed** (Tuya devices accept ~1 local client):

   ```python
   import tinytuya
   d = tinytuya.Device(DEVID, "192.168.x.y", LOCAL_KEY, version=3.5)
   d.set_socketPersistent(True)
   print(d.status())
   d.set_value(103, True)   # locate → beep = write access confirmed
   ```

3. **Save the status dump** — the T2266 DPS fingerprint for the PR. Diff it against the T2276 reference table below. Expected: identical structure, possibly no DPS 126 (SES dust-collect). Watch value *casing* (T2276 confirmed lowercase `"auto"` on DPS 5, unlike T2128's `"Auto"`).
4. If 3.5 fails: try 3.4/3.3 (note which works — firmware variance goes in the PR), then check key freshness / IP / app-holding-the-socket before concluding anything.

---

## Phase C — The proper fix: add T2266 upstream (the coding session)

Follow the repo's own guide: <https://damacus.github.io/robovac/adding-new-vacuum/>. Concrete steps:

**C.1 Setup**

```bash
git clone https://github.com/<you>/robovac && cd robovac   # fork first
git checkout -b feat/add-t2266-support
uv sync            # or pip install -r requirements-dev.txt; devcontainer available
task test          # green baseline
python -m custom_components.robovac.model_validator_cli --list   # no T2266 yet
```

**C.2 Model file** — `custom_components/robovac/vacuums/T2266.py`. Two options, decide after checking repo precedent (how are twins T2261/T2262 done? X10 Pro was added as an "alias T2351" per changelog):

- *Minimal:* `class T2266(T2276):` with docstring `"""RoboVac X8 Pro (T2266)"""` — zero duplication, inherits future T2276 fixes.
- *Explicit:* copy T2276's `homeassistant_features`, `robovac_features`, `commands`, `activity_mapping`; drop SES-only entries per your Phase-B dump. More verbose, but matches the guide's template and survives T2276-specific divergence.

Either way: `protocol_version` must stay **3.5**.

**C.3 Register** — `custom_components/robovac/vacuums/__init__.py`: import the class, add `"T2266": T2266` to `ROBOVAC_MODELS`.

**C.4 Tests** — `tests/test_vacuum/test_t2266_command_mappings.py`, mirroring `test_t2276_command_mappings.py` (assert DPS codes 2/5/15/101/102/103/104/106 and key values). Run `task lint && task test`; validator CLI must now list T2266.

**C.5 Live verification checklist** (on your vacuum, debug logging on):

- [ ] start → robot undocks and cleans (`{5:"auto", 2:true}`)
- [ ] pause (`{2:false}`) / resume
- [ ] return home (`{101:true, 2:true}`) → docks
- [ ] locate beep (`{103:true}`)
- [ ] fan speed change reflected on device (Quiet/Standard/Turbo/Boost)
- [ ] battery + cleaning time/area sensors update
- [ ] **full cycle state mapping:** every `DPS 15` value (`Running/Locating/Recharge/Charging/standby/Paused/completed`) lands on the right `VacuumActivity`; especially confirm `cleaning → returning → docked` completes (the #542 stuck-state trap on SES siblings — if you see it on v2.4.3 stable, A/B against v2.4.3-beta.1 and add your data to that issue)

**C.6 Docs & PR**

- Add T2266 row to `site_docs` supported-models table (X Series, protocol 3.5). Bonus one-liner: the T2276 row still says 3.4 while the code uses 3.5 since #341 — fix it in the same PR.
- Conventional commit: `feat(T2266): add RoboVac X8 Pro support` (release-please builds the changelog from it).
- PR body: link this error, note T2266 ≡ T2276 hardware (shared eufy manual), include firmware version + tinytuya dump + the live checklist above. The maintainer merges hardware-tested model PRs readily (#341, #547/T2256, #522/T2258) and has said reviewers can't substitute for a real vacuum — you own the scarce resource.

---

## Phase D — Environment prerequisites (needed once it connects)

- DHCP reservation → integration options: **disable autodiscovery, set static IP** (UDP 6666/6667 broadcast discovery is fragile across VLANs/Docker; only TCP **6668** HA→vacuum is needed with a manual IP).
- Keep the eufy app closed while testing; power-cycle the vacuum to clear a stale socket if you see `Broken pipe` right after connect.
- Remove any leftover LocalTuya/CodeFoodPixels config for this device — two local clients fight over the single slot.

---

## Reference — T2276 DPS map (expected T2266 baseline, from PR #341, live-tested)

| DPS | Name | Type | Values / notes |
|-----|------|------|----------------|
| 2 | Start/Pause | bool | `true`=start, `false`=pause; also execution trigger after 101/124 |
| 5 | Mode | string | `auto` (lowercase!), `Edge`, `SmallRoom`, `Nosweep`, `room` |
| 15 | Status | string | `Running`, `Locating`, `Recharge`, `Charging`, `standby`, `Paused`, `completed` |
| 101 | Return home | bool | `true` triggers (boolean, not string `"return"`) |
| 102 | Fan speed | string | `Quiet`, `Standard`, `Turbo`, `Boost` (UI "Pure" ↔ device `Quiet`) |
| 103 | Locate | bool | `true` → beep |
| 104 | Battery | int | % |
| 106 | Error | int | error code |
| 109 / 110 | Clean time / area | int | min / m² |
| 118 | BoostIQ | bool | |
| 122 | Clean mode | string | `Continue`, `Pause`, `Nosweep` |
| 124 | Room selection | base64 | needs `2:true` ~1 s later (race fixed in #341) |
| 126 | Dust collect | bool | **SES-only — likely absent on T2266** |
| 142 | Events | base64 | `start_clean`, `clean_result`, `reloc`, `key` |

Protocol-3.5 quirks baked into `tuyalocalapi.py` (don't "fix" away): 3-step `SESS_KEY_NEG` handshake first; **no heartbeats** (device closes TCP on ping); connect-only state via gratuitous 0x08 pushes (explicit GET → `json obj data unvalid`); SET via `CONTROL_NEW (0x0d)` with `{"protocol":5,"t":…,"data":{"dps":{…}}}`; ~30 s idle timeout, 5 s reconnect cooldown.

## Log signature quick reference

| Signature | Cause | Fix |
|---|---|---|
| `Model T2266 is not supported` | Missing registry entry | Phase A alias → Phase C PR |
| Endless `Incomplete read` + seq-number timeouts | Pre-#341 version / wrong protocol framing | Upgrade ≥ v2.4.3 |
| `Cannot update vacuum X: IP address not set` | Autodiscovery failed | Static IP in options |
| `Broken pipe` right after connect | App/other client holds the single local slot | Close app, power-cycle |
| State stuck on `cleaning` after docking | SES regression on v2.4.3 stable (#542) | A/B v2.4.3-beta.1, report |

## Links

- Add-a-model guide: <https://damacus.github.io/robovac/adding-new-vacuum/>
- PR #341 (T2276 fix, DPS + v3.5 details): <https://github.com/damacus/robovac/pull/341>
- Issue #42 (history + tinytuya recipe): <https://github.com/damacus/robovac/issues/42>
- Issue #542 (SES stuck-state on v2.4.3 stable): <https://github.com/damacus/robovac/issues/542>
- Changelog / tags: <https://github.com/damacus/robovac/blob/main/CHANGELOG.md>
- eufy X8 Pro manual covering **both** T2266 & T2276 (hardware-equivalence evidence for the PR): support.eufy.com "eufy Clean X8 Pro Series User Guide (For T2266 & T2276)"
- tinytuya: <https://github.com/jasonacox/tinytuya>

## Session checklist (condensed)

- [ ] HACS: confirm/upgrade to v2.4.3; remove + re-add integration entry
- [ ] Phase A alias `"T2266": T2276` → restart → smoke test
- [ ] tinytuya v3.5 dump saved (PR evidence); locate-beep write test
- [ ] Static IP, autodiscovery off, app closed
- [ ] Fork, branch `feat/add-t2266-support`, `T2266.py` + registry + tests
- [ ] `task lint && task test`; validator lists T2266
- [ ] Full-cycle live checklist incl. state transitions
- [ ] Docs row (+ fix T2276 "3.4" typo) → PR with dump + firmware + checklist
