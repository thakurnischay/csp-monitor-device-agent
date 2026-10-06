"""Single source of truth for the agent's own version string, sent with
every heartbeat (see reporter.py) so the dashboard can show which CSPs are
running which build. Bump this when shipping a new agent zip."""
AGENT_VERSION = "1.2.0"

# Heartbeat payload shape version - separate from AGENT_VERSION so the server
# can tell "old agent, new field missing" apart from "new agent, old
# behavior" without guessing from the agent version string.
SCHEMA_VERSION = 3
