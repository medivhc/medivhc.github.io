# Installation

## Requirements
- Python 3.11+ with `PyYAML` and `jsonschema` (`pip install pyyaml jsonschema`)
- Claude Code (verified: **2.1.319**). Hook field names were checked against that build:
  `Stop{stop_hook_active,last_assistant_message}`, `TaskCompleted{task_id,task_subject,task_description}`,
  `StopFailure{error,error_details}`, `SessionEnd{reason}`, `SessionStart{source}`.
  Re-check after upgrading (`rvd doctor`, and the live test below).

## Load the plugin
```bash
claude plugin validate /path/to/rvd-harness        # must print "Validation passed"
claude --plugin-dir /path/to/rvd-harness            # load for a session
```
Or add it to a marketplace / install it as you do other plugins. The plugin's `bin/` is put on
PATH by Claude Code, so `rvd` is available to the agent's Bash tool.

## Use the CLI outside Claude Code
```bash
python3 /path/to/rvd-harness/scripts/rvd.py --help
# or an editable install that provides an `rvd` console script:
pip install -e /path/to/rvd-harness
```

## Initialise a project
1. Create `<project>/.rvd/goal.yaml` (copy `templates/goal.yaml` or an `examples/*/.rvd/goal.yaml`).
2. A human runs `rvd init --confirm-hard --by <name>` when the contract has HARD items.
   Inside an agent session (`CLAUDECODE=1`) HARD confirmation, amendment approval, cancel/resume
   are refused by design.
3. Commit `.rvd/goal.yaml` and `.rvd/policy.yaml`; `.rvd/.gitignore` keeps `state.db`,
   `evidence/` and `reports/` out of Git.

State location: `<project>/.rvd/` by default; set `RVD_STATE_DIR=/somewhere` to keep state outside
the repo (one sub-directory per project root hash). The plugin directory never holds project state.

## Run the tests
```bash
cd /path/to/rvd-harness && python3 -m pytest -q          # unit + scenarios (no network, no API)
RVD_LIVE=1 python3 -m pytest -q -m live                    # real headless Claude Code session (uses API credits)
```
