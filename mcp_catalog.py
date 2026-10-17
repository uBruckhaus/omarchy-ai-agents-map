"""MCP servers to start with: the reference servers of the MCP project
(github.com/modelcontextprotocol/servers) and the official servers of a few
well-known makers, with a plain explanation of what each is for.

The entries are written in the registry's format (packages, remotes), so
mcp_registry turns them into the same options as a search result. Before a
server is shown, the version of each package is asked from npm or PyPI and
pinned (npx -y pkg@1.2.3, uvx pkg==1.2.3); offline, the package stays
unpinned (the newest one when it starts).
"""

import re
import urllib.parse
from dataclasses import dataclass, field

import mcp_registry
from i18n import N_

GROUPS = (("files", N_("Files and data")), ("web", N_("Web and browser")), ("code", N_("Coding")),
          ("think", N_("Thinking and memory")))
TRUST = {"reference": N_("Reference server · MCP project"), "official": N_("Official · {0}")}
TRUST_HINTS = {
    "reference": N_("Written by the people behind MCP as examples of how a server works. Fine for "
                    "everyday use on your own PC; they are kept simple on purpose."),
    "official": N_("Made by the company behind the service, from its own repository."),
}


@dataclass
class Pick:
    key: str                  # suggested server name
    title: str
    group: str
    maker: str                # "MCP project", "GitHub", …
    trust: str                # "reference" or "official"
    about: str                # one sentence: what it gives the agents
    tries: list               # things to ask an agent once it has the server
    needs: str                # what you need besides the program, in words
    repository: str
    spec: dict = field(default_factory=dict)    # registry format: packages, remotes
    key_url: str = ""         # where to get the key it needs


def _npm(name: str, **extra) -> dict:
    return {"registryType": "npm", "identifier": name, "version": "latest", "transport": {"type": "stdio"},
            **extra}


def _pypi(name: str, **extra) -> dict:
    return {"registryType": "pypi", "identifier": name, "version": "latest", "transport": {"type": "stdio"},
            **extra}


REF = "https://github.com/modelcontextprotocol/servers"
PICKS = [
    Pick("filesystem", "Filesystem", "files", "MCP project", "reference",
         N_("Lets the agents read, write, move and search files, but only in the folder you choose."),
         [N_("Sort the PDFs in my folder into subfolders by year"), N_("Which files mention the invoice 2041?")],
         N_("A folder the agents may use."), REF + "/tree/main/src/filesystem",
         {"packages": [_npm("@modelcontextprotocol/server-filesystem", packageArguments=[
             {"type": "positional", "isRequired": True, "valueHint": "~/Documents",
              "description": N_("Folder")}])]}),
    Pick("git", "Git", "code", "MCP project", "reference",
         N_("Reads and changes Git repositories: status, history, diffs, branches and commits."),
         [N_("What changed in this repository last week?"), N_("Commit my staged changes with a good message")],
         N_("Nothing: it works on the repository the agent names."), REF + "/tree/main/src/git",
         {"packages": [_pypi("mcp-server-git")]}),
    Pick("fetch", "Fetch", "web", "MCP project", "reference",
         N_("Opens a web address and hands the page to the agent as plain text."),
         [N_("Read this article and give me the three main points: https://…")],
         N_("Nothing."), REF + "/tree/main/src/fetch",
         {"packages": [_pypi("mcp-server-fetch")]}),
    Pick("playwright", "Playwright", "web", "Microsoft", "official",
         N_("A real browser the agent can drive: open pages, click, fill in forms, take screenshots."),
         [N_("Open my web app on localhost:3000 and check that the login works")],
         N_("Nothing: it downloads its own browser on first use."), "https://github.com/microsoft/playwright-mcp",
         {"packages": [_npm("@playwright/mcp")]}),
    Pick("chrome-devtools", "Chrome DevTools", "web", "Google", "official",
         N_("Inspects a page in Chrome: console messages, network requests, performance."),
         [N_("Why does this page load so slowly?")],
         N_("Google Chrome."), "https://github.com/ChromeDevTools/chrome-devtools-mcp",
         {"packages": [_npm("chrome-devtools-mcp")]}),
    Pick("brave-search", "Brave Search", "web", "Brave", "official",
         N_("Web, news and image search for the agents through Brave's search service."),
         [N_("Search for reviews of the Framework 13 from this year")],
         N_("A Brave Search API key (there is a free plan)."), "https://github.com/brave/brave-search-mcp-server",
         {"packages": [_npm("@brave/brave-search-mcp-server", environmentVariables=[
             {"name": "BRAVE_API_KEY", "isRequired": True, "isSecret": True,
              "description": N_("Your Brave Search API key")}])]},
         "https://brave.com/search/api/"),
    Pick("github", "GitHub", "code", "GitHub", "official",
         N_("Works with GitHub for the agents: repositories, issues, pull requests, Actions."),
         [N_("List the open issues of my repository labelled bug"), N_("Summarise pull request 42")],
         N_("A GitHub personal access token. The server runs at GitHub; nothing is installed."),
         "https://github.com/github/github-mcp-server",
         {"remotes": [{"type": "streamable-http", "url": "https://api.githubcopilot.com/mcp/", "headers": [
             {"name": "Authorization", "value": "Bearer {GITHUB_TOKEN}", "isRequired": True, "isSecret": True,
              "variables": {"GITHUB_TOKEN": {"isRequired": True, "isSecret": True,
                                             "description": N_("Your GitHub personal access token")}}}]}]},
         "https://github.com/settings/personal-access-tokens"),
    Pick("context7", "Context7", "code", "Upstash", "official",
         N_("Current documentation and code examples of libraries, so the agent does not guess old APIs."),
         [N_("How do I stream a response with the newest FastAPI? use context7")],
         N_("Nothing; a free API key gives higher limits."), "https://github.com/upstash/context7",
         {"packages": [_npm("@upstash/context7-mcp", environmentVariables=[
             {"name": "CONTEXT7_API_KEY", "isSecret": True, "description": N_("API key (optional)")}])],
          "remotes": [{"type": "streamable-http", "url": "https://mcp.context7.com/mcp", "headers": [
              {"name": "Authorization", "value": "Bearer {CONTEXT7_API_KEY}", "isSecret": True,
               "variables": {"CONTEXT7_API_KEY": {"isSecret": True, "description": N_("API key (optional)")}}}]}]},
         "https://context7.com/dashboard"),
    Pick("time", "Time", "think", "MCP project", "reference",
         N_("The current time and conversions between time zones."),
         [N_("What time is it in Tokyo when it is 9:00 here?")],
         N_("Nothing."), REF + "/tree/main/src/time",
         {"packages": [_pypi("mcp-server-time")]}),
    Pick("memory", "Memory", "think", "MCP project", "reference",
         N_("A memory that lasts between chats: the agent stores facts as a small graph in a local file."),
         [N_("Remember that my server is called atlas and runs Debian")],
         N_("Nothing."), REF + "/tree/main/src/memory",
         {"packages": [_npm("@modelcontextprotocol/server-memory")]}),
    Pick("sequential-thinking", "Sequential Thinking", "think", "MCP project", "reference",
         N_("Gives the agent a way to think through a hard problem step by step and revise its steps."),
         [N_("Plan the move of my photos to the new disk, step by step")],
         N_("Nothing."), REF + "/tree/main/src/sequentialthinking",
         {"packages": [_npm("@modelcontextprotocol/server-sequential-thinking")]}),
    Pick("everything", "Everything", "think", "MCP project", "reference",
         N_("A test server with every MCP feature. Useful to see whether an agent handles MCP at all."),
         [N_("Which tools does the everything server offer?")],
         N_("Nothing."), REF + "/tree/main/src/everything",
         {"packages": [_npm("@modelcontextprotocol/server-everything")]}),
]


def latest_version(kind: str, name: str) -> str:
    """The newest version on npm or PyPI, "" when it cannot be asked."""
    import netfetch
    url = (f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}/latest" if kind == "npm" else
           f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json")
    try:
        data = netfetch.fetch_json(url, max_bytes=4 * 1024 * 1024, timeout=10, redirects=1,
                                   headers={"Accept": "application/json"})
    except (netfetch.FetchError, ValueError):
        return ""
    version = (data.get("version") if kind == "npm" else (data.get("info") or {}).get("version")) \
        if isinstance(data, dict) else None
    return version if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,60}", version) else ""


def resolve(pick: Pick, ask=latest_version) -> mcp_registry.Server:
    """The pick as a registry Server, its packages pinned to their newest version."""
    packages = []
    for package in pick.spec.get("packages", []):
        version = ask(package["registryType"], package["identifier"])
        packages.append({**package, "version": version or "latest"})
    data = {"servers": [{"server": {"name": pick.key, "title": pick.title, "description": pick.about,
                                    "repository": {"url": pick.repository},
                                    "packages": packages, "remotes": pick.spec.get("remotes", [])}}]}
    server = mcp_registry.parse(data)[0]
    server.short = pick.key
    server.version = next((p["version"] for p in packages if p["version"] != "latest"), "")
    return server
