"""Oh My Pi's hooks (adapters/omp.toml): connecting a local LLM.

Oh My Pi is a fork of Pi with YAML settings: the provider goes in ~/.omp/agent/models.yml
(`providers: <key>:` with baseUrl, api, auth: none, models) and the model it uses in
~/.omp/agent/config.yml (`modelRoles: default: <key>/<model>`). Both are edited as text, as
Hermes's config.yaml: what Oh My Pi or the user wrote there stays as it is.
(Oh My Pi also finds Ollama, llama.cpp and LM Studio on their usual ports by itself, under its own
names; an entry of ours works for every local LLM and address, and is what the map shows.)
"""

import json
import re

ROLE = "default"


def _quoted(value: str) -> str:
    """A YAML scalar for any model name (JSON strings are valid YAML)."""
    return json.dumps(str(value), ensure_ascii=False)


def models_text(text: str, provider: str, url: str, models: list, context: dict, remove: bool = False) -> str:
    import connect
    entry = [f"  {provider}:", "    auth: none", f"    baseUrl: {_quoted(url)}", "    api: openai-completions",
             "    models:"]
    for name in models:
        entry += [f"      - id: {_quoted(name)}", f"        name: {_quoted(name)}"]
        if context.get(name):
            entry.append(f"        contextWindow: {int(context[name])}")
    if not models:
        entry[-1] = "    models: []"
    entry_text = "\n".join(entry) + "\n"
    block = connect._top_block(text, "providers")
    if block is None:
        if remove:
            return text
        return text + ("" if text.endswith("\n") or not text else "\n") + "providers:\n" + entry_text
    start, end = block
    header, _nl, rest = text[start:end].partition("\n")
    # Our entry: from "  <provider>:" to the next two-space key.
    rest = re.sub(rf"^  {re.escape(provider)}:.*\n(?:(?:    |\t\t).*\n?|\s*\n)*", "", rest, flags=re.MULTILINE)
    if not remove:
        rest = rest.rstrip("\n") + ("\n" if rest.strip() else "") + entry_text
    header = "providers:" if header.strip() in ("providers: {}", "providers:{}") else header
    body = header + "\n" + rest if rest.strip() else ("" if remove else header + "\n")
    return text[:start] + body + text[end:]


def _role_value(text: str):
    import connect
    block = connect._top_block(text, "modelRoles")
    if block is None:
        return None
    found = re.search(rf"^  {ROLE}:\s*(.*?)\s*$", text[block[0]:block[1]], re.MULTILINE)
    return found.group(1).strip("'\"") if found else None


def config_text(text: str, provider: str, model: str, use_now: bool, remove: bool = False) -> str:
    import connect
    block = connect._top_block(text, "modelRoles")
    if remove:
        # The model would point at a provider that is gone: Oh My Pi falls back to its own choice.
        value = _role_value(text)
        if block is None or value is None or not value.startswith(provider + "/"):
            return text
        start, end = block
        header, _nl, rest = text[start:end].partition("\n")
        rest = re.sub(rf"^  {ROLE}:.*\n?", "", rest, count=1, flags=re.MULTILINE)
        body = header + "\n" + rest if rest.strip() else ""
        return text[:start] + body + text[end:]
    if not use_now or not model:
        return text
    line = f"  {ROLE}: {_quoted(provider + '/' + model)}"
    if block is None:
        return text + ("" if text.endswith("\n") or not text else "\n") + "modelRoles:\n" + line + "\n"
    start, end = block
    header, _nl, rest = text[start:end].partition("\n")
    header = "modelRoles:" if header.strip() in ("modelRoles: {}", "modelRoles:{}") else header
    if re.search(rf"^  {ROLE}:", rest, re.MULTILINE):
        rest = re.sub(rf"^  {ROLE}:.*$", lambda _m: line, rest, count=1, flags=re.MULTILINE)
    else:
        rest = line + "\n" + rest
    return text[:start] + header + "\n" + rest + text[end:]


def local_llm_connected(texts, provider):
    import connect
    block = connect._top_block(texts[0], "providers")
    return bool(block) and re.search(rf"^  {re.escape(provider)}:", texts[0][block[0]:block[1]],
                                     re.MULTILINE) is not None


def local_llm_active(texts, provider):
    value = _role_value(texts[1])
    return value is not None and value.startswith(provider + "/")


def local_llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    return [models_text(texts[0], provider, url, models, context, remove),
            config_text(texts[1], provider, model, use_now, remove)]
