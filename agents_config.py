"""
Centralized Agent Configuration

Single-agent configuration for Phantom (Browser Automation Agent).
"""

# Agent definitions - single source of truth
AGENTS = {
    "phantom": {
        "name": "Phantom",
        "role": "Browser Automation Agent",
        "emoji": "👻",
        "spec": "PHANTOM_SPEC.md",
        "mentions": ["phantom", "Phantom", "@phantom"],
    },
}


def get_agent(agent_id: str) -> dict:
    """Get agent config by ID (case-insensitive)."""
    return AGENTS.get(agent_id.lower())


def list_agents() -> list:
    """Get list of all agent IDs."""
    return list(AGENTS.keys())


def get_agent_by_name(name: str) -> dict:
    """Get agent config by display name."""
    for agent in AGENTS.values():
        if agent["name"].lower() == name.lower():
            return agent
    return None
