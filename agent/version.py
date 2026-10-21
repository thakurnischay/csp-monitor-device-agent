"""Single source of truth for the agent's own version string, sent with
every heartbeat (see reporter.py) so the dashboard can show which CSPs are
running which build. Bump this when shipping a new agent zip."""
AGENT_VERSION = "2.2.0"

# Heartbeat payload shape version - separate from AGENT_VERSION so the server
# can tell "old agent, new field missing" apart from "new agent, old
# behavior" without guessing from the agent version string.
SCHEMA_VERSION = 4

# Monotonic release number, stamped by tools/release_tool.py into every signed release. An agent
# only installs a release whose sequence is HIGHER than this (downgrade protection). 0 = a development
# build that was never produced by the release tool.
AGENT_SEQUENCE = 0

# What this agent can do, reported with every heartbeat so the dashboard knows whether it can be
# updated centrally. A legacy agent (before this field existed) cannot, and shows as "needs one-time bootstrap".
UPDATER_VERSION = "1.0"
CAPABILITIES = {"remote_update": True, "updater": UPDATER_VERSION}

