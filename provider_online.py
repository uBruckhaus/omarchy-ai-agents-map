"""Online providers: their name, the ids agents use for them in their settings ("anthropic/claude-…",
Pi's defaultProvider, Hermes's provider) and the agents that come with them (Claude Code with
Anthropic …)."""

from providers import Provider


def _online(key, name, ids=(), agents=()):
    return Provider(key, name, "online", ids=tuple(ids), agents=tuple(agents))


ONLINE = [
    _online("anthropic", "Anthropic", ("anthropic",), ("claude",)),
    _online("openai", "OpenAI", ("openai", "openai-codex"), ("codex",)),
    _online("github-copilot", "GitHub Copilot", ("github-copilot",), ("copilot",)),
    _online("cursor", "Cursor", (), ("cursor-agent",)),
    _online("google", "Google", ("google", "gemini"), ("gemini",)),
    _online("xai", "xAI", ("xai",), ("grok",)),
    _online("meta", "Meta", (), ("muse",)),
    _online("openclaw", "OpenClaw", (), ("openclaw",)),
    _online("nous", "Nous Research", ("nous",)),
    _online("openrouter", "OpenRouter", ("openrouter",)),
    _online("groq", "Groq", ("groq",)),
    _online("mistral", "Mistral", ("mistral",)),
    _online("deepseek", "DeepSeek", ("deepseek",)),
    _online("fireworks", "Fireworks", ("fireworks",)),
    _online("together", "Together AI", ("together",)),
    _online("opencode-zen", "OpenCode Zen", ("opencode",)),
    _online("zai", "Z.ai", ("zai",)),
]
