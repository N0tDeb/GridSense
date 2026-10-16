# GridSense

GridSense simulates how a distribution feeder loses and regains electricity
supply. Start with a healthy network, introduce an outage or a demand change,
and follow the measurements, alarms and switching decisions through restoration
and repair. OpenDSS solves the electrical state at every simulated step.

A connected route can still overload a line or transformer. A successful switch
command can still be waiting for confirmation. GridSense makes these differences
visible and records the evidence behind each decision.

The application runs locally on a CPU machine, using Python, OpenDSSDirect,
FastAPI, SQLite, React and TypeScript.

[Feature and flow verification](docs/FLOW_VERIFICATION.md) maps the complete
journeys to executed checks and identifies the remaining review scope.

![GridSense live restoration dashboard](docs/dashboard.png)

## Explore the grid

The dashboard has three areas:

| Area | What it is for |
| --- | --- |
| **Live Grid** | Pause, step or play a session. Introduce outages, change demand, delay measurements, and schedule simulated repair. |
| **Incident analysis** | Inspect a recorded timeline, trace alarms to raw readings, compare switching alternatives, and replay saved incidents. |
| **Controller tests** | Check electrical and service requirements, test switch confirmation, search for failures, and compare reference voltages. |

Nominal and guarded controllers plan from received measurements and device status.
The evaluator uses the actual simulated demand to check the outcome. Their
predictions and the resulting voltages, loading and service are recorded separately.
Live plans check assumed switching times and compare predicted unserved energy
through the sequence. Held uncertainty bounds age while commands wait. Fresh
overload readings also constrain corrective switching and later reconnection.
See [execution planning](docs/EXECUTION_PLANNING.md),
[architecture](docs/ARCHITECTURE.md) and [electrical models](docs/MODEL.md).

## Run locally

Use Python **3.12** and Node **24** to match the verified environment. The pinned
packages require Python 3.12+ and Node 22.12+. Installation needs internet access;
the application needs no account, API key, GPU or external database.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install -e . --no-deps
cd frontend
npm ci
npm run build
cd ..
gridsense serve
```

Open **http://127.0.0.1:8000**. [SETUP.md](SETUP.md) includes Windows PowerShell
commands, browser setup, CLI workflows and troubleshooting. Windows execution
and hosted CI have not yet been verified.

## Follow an outage through restoration

1. In **Live Grid**, keep the nominal policy and create a live session.
2. Click **Add disturbance**, leave `line:feed_a` selected. Check **Schedule simulated
   repair**, then click **Schedule disturbance**.
3. Step once: the feeder section becomes unavailable, its loads lose supply,
   and a switch command waits for confirmation. Step again to see verified alternate supply.
4. Use **View alarms**, **Switch commands** and **Restoration decision** beside the
   topology to inspect the raw readings, confirmations and checked alternatives.
5. Continue through repair. The equipment becomes available first; a checked
   switching sequence then returns the feeder to normal. The brief interruption
   during break-before-make switching counts toward unserved energy.
6. Open **Incident analysis → Live session timeline** to inspect an earlier tick,
   export the captured record or replay the saved session.

[Live dashboard](docs/LIVE_DASHBOARD.md) explains the controls and what each status
means. The dashboard selects nominal control for a new session. The API and CLI
default to control off; guarded and off policies are also available in the dashboard.

An alternate route restores supply without repairing damaged equipment. Simulated
repair makes that equipment available again, but reconnection still needs a separate
decision. An MV tie cannot bypass a failed transformer that supplies an LV load.

For the same workflow without the browser, run
`python examples/restoration_episode.py`. The example checks normal recovery and
a missing-confirmation case. `python examples/equipment_availability.py` shows
individual equipment transitions. [Live sessions](docs/LIVE_SESSIONS.md) describes
the shared API and CLI engine.

## Investigate a stale reading

The snapshot lab compares alternatives from the same isolated starting state:

1. Open **Incident analysis → Restoration lab**, keep **Stale reading** selected,
   and click **Run restoration**.
2. Select **Nominal**. Its west-backup choice overloads the incoming west line
   when evaluated against actual demand.
3. Select **Guarded** to inspect the choice after checking the assumed demand range.
4. Open **Decision evidence** and compare the received readings, predictions and results.
5. Click **Verify replay**, export the incident, or use it as a controller test.

The [walkthrough](docs/WALKTHROUGH.md) traces this example into the code. These
snapshot comparisons are separate experiments; selecting a branch does not switch
equipment in a live session.

## Results and scope

| Study | Scope |
| --- | --- |
| Clocked restoration | 12 fixed scenarios under off, nominal and guarded policies; sequence and uncertainty checks |
| Boundary challenge | 25 paired/control scenarios under three policies, with complete 16-second traces; [findings](docs/SESSION_CHALLENGE.md) |
| Measurement refresh | Three received-evidence cases retaining partial donor supply; [results](docs/MEASUREMENT_REFRESH.md) |
| Native solve reuse | Eight frozen planning inputs at two budgets, with a full-demand reference; [results](docs/NATIVE_REUSE.md) |
| Controller fault injection | 224 pairs, five seeded mechanisms and negative controls |
| Switch status | 24 episodes with delayed feedback and failed actuation |
| Controller and search comparisons | Complete case grids, paired seeds, computation costs and service trade-offs |
| Constant-PQ reference v1 | 56/56 frozen targets pass with an explicit load-window and convergence profile; [configuration](docs/REFERENCE_VALIDATION.md#constant-power-profile) |
| Native-default reference audit | 52/56 checks pass; four model/comparator mismatches are explicitly reported |
| Model-aware reference | Published voltages, conditional load-law envelopes and numerical balance checked separately |
| Software and browser checks | [Verification](docs/VERIFICATION.md), with commands, counts and runtime checks |

[EVALUATION.md](docs/EVALUATION.md) explains the experiments and how to reproduce
them. [ASSURANCE.md](docs/ASSURANCE.md) covers fault injection and the electrical
mismatches. Results include service losses, unsuccessful searches, and cases where
guarded control or directed search offers no advantage.

The networks and measurements are synthetic. Outages are configured whole-device
trips; the simulator does not diagnose fault waveforms or predict equipment failure.
The snapshot guarded policy checks three demand values. The session policy checks
up to 15 combinations across six load groups. These finite checks do not establish
field safety or research novelty. [Limitations](docs/LIMITATIONS.md) explains the
model, controller and deployment boundaries.

## Verify and develop

After installing Chromium as described in SETUP.md:

```bash
python scripts/verify.py
```

This runs repository integrity, backend and browser tests, reference and evidence
checks, and replay of the included examples. The report checks the versioned 56/56 constant-PQ profile
and retains the known mismatches under the native defaults.
[DEVELOPMENT.md](DEVELOPMENT.md) covers formatting, tests, replay identity and packaging.

[Development](DEVELOPMENT.md), [contribution guidance](CONTRIBUTING.md),
[repository publication](docs/REPOSITORY.md) and [security](SECURITY.md) describe
maintenance and release checks.

## Repository guide

| Path | Responsibility |
| --- | --- |
| `backend/gridsense/electrical.py`, `ieee13.py` | Native circuits, switching, solving and constraints |
| `backend/gridsense/episodes.py`, `telemetry.py`, `rules.py` | Simulation clock, sensor delivery and alarms |
| `backend/gridsense/restoration.py`, `control.py`, `switching.py` | Session planning, confirmation and recovery |
| `backend/gridsense/session_store.py`, `session_api.py` | Atomic session actions, pacing and replay |
| `backend/gridsense/restoration_reference.py`, `episode_metrics.py` | Sequence reference and fixed-horizon outcomes |
| `backend/gridsense/planning.py`, `controllers.py` | Snapshot candidates and controller selection |
| `backend/gridsense/service.py`, `regression.py` | Observation/truth boundary, incidents, contracts and replay |
| `backend/gridsense/failure_search.py`, `temporal.py` | Failure search, reduction and switch-event experiments |
| `backend/gridsense/assurance.py`, `reference_checks.py` | Defect study and electrical reference audit |
| `backend/gridsense/api.py`, `storage.py` | HTTP workers and incident storage |
| `frontend/src/` | Dashboard and topology views |
| `backend/tests/`, `frontend/tests/` | Electrical, behavioral and browser tests |
| `experiments/` | Protocols, complete results and replay examples |
| `docs/verification/` | Current release verification summary |
| `docs/figures/` | Rendered architecture/storage diagrams and a data-derived result chart |

## Attribution and licensing

GridSense's original source code and original documentation are licensed under
the [Apache License 2.0](LICENSE). Copyright 2026
[N0tDeb](https://github.com/N0tDeb). The [NOTICE](NOTICE) records attribution
and the scope of the project license.

Bundled third-party components retain their respective licenses. Model
provenance is in [MODEL.md](docs/MODEL.md); the inherited IEEE13 model license
is in [License.txt](backend/gridsense/circuits/ieee13/License.txt). Frontend and
font licenses are in [THIRD_PARTY_NOTICES.txt](frontend/THIRD_PARTY_NOTICES.txt).
