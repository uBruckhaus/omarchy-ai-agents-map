"""Move every user of an app's older version to its newer one.

An app found in a newer version (mcp_scan.fold_versions: candidate.update_from)
while agents still start the older one, e.g. ~/Applications/gridcraft-0.6.1/
with gridcraft-0.7.0-linux-x86_64.AppImage next to it. One plan (preview,
backup, Undo) for all of it:
- each agent's entry that starts an older program gets the new program
  (args that point into the old folder move along);
- entries in the list of MCP servers likewise;
- links in ~/.local/bin and ~/bin into the old folder point into the new one
  (entries starting such a link stay as they are);
- the old folders are moved into the backup, deleted after
  connect.RETIRED_DAYS (Undo within that time brings them back).
The newer AppImage is unpacked (mcp_scan.unpack) before the plan is applied.
"""

import os
import shutil
from pathlib import Path

import connect
import mcp_ctl
import mcp_library

HOME = Path.home()
LINK_DIRS = (HOME / ".local/bin", HOME / "bin")


def _real(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def new_folder(candidate) -> Path:
    """Where the newer version lives (or will, once unpacked)."""
    import mcp_scan
    if candidate.appimage:
        return HOME / "Applications" / mcp_scan.appimage_stem(Path(candidate.appimage))
    real = Path(_real(candidate.program))
    rest = real.relative_to(HOME.resolve() / "Applications")
    return HOME / "Applications" / rest.parts[0]


def _moved(path: str, old_folders: list, folder: Path) -> str:
    """A path inside an old folder, at the same place in the new one."""
    real = _real(path)
    for old in old_folders:
        for form in (old, _real(old)):
            if _inside(real, form):
                return str(folder) + real[len(form.rstrip("/")):]
    return path


def links(candidate) -> list:
    """(link, its target now, new target): the user's links into the old folders."""
    folder = new_folder(candidate)
    found = []
    for place in LINK_DIRS:
        try:
            entries = sorted(place.iterdir())[:500]
        except OSError:
            continue
        for link in entries:
            if not link.is_symlink():
                continue
            target = os.readlink(link)
            absolute = target if os.path.isabs(target) else str(link.parent / target)
            moved = _moved(absolute, candidate.old_folders, folder)
            if moved != absolute:
                found.append((link, target, moved))
    return found


def _new_entry(entry: dict | None, candidate, linked: set) -> dict | None:
    """The entry starting the new version, or None when it is not an old one."""
    if not entry or entry["transport"] != "stdio":
        return None
    command = os.path.expanduser(entry["command"])
    program = shutil.which(command) or command
    if os.path.abspath(program) in linked:
        return None          # starts a link that is moved to the new version
    old = {_real(p) for p in candidate.replaces} | set(candidate.replaces)
    if program not in old and _real(program) not in old:
        return None
    folder = new_folder(candidate)
    new_program = _moved(program, candidate.old_folders, folder)
    if new_program == program:      # the old program outside its folder: the found one instead
        new_program = candidate.program
    args = [_moved(a, candidate.old_folders, folder) if a.startswith("/") else a for a in entry["args"]]
    return {**entry, "command": new_program, "args": args}


def plan(candidate, agents) -> connect.Plan:
    """The whole update as one plan. agents: commands of the installed agents."""
    folder = new_folder(candidate)
    moved_links = links(candidate)
    linked = {os.path.abspath(str(link)) for link, _old, _new in moved_links}
    plans = []
    for agent in agents:
        if not mcp_ctl.supported(agent):
            continue
        for name, raw in mcp_ctl.raw_entries(agent).items():
            if not mcp_ctl.NAME_RE.match(name):
                continue
            entry = _new_entry(mcp_ctl.normal(raw), candidate, linked)
            if entry is not None:
                plans.append(mcp_ctl.plan(agent, name, entry=entry))
    listed = connect.Plan("", "", candidate.name)
    servers = mcp_library.load()
    changed = {}
    for name, entry in servers.items():
        new = _new_entry(entry, candidate, linked)
        if new is not None:
            changed[name] = new
    if changed:
        path = mcp_library.library_file()
        old = connect._read(path)
        new_text = mcp_library.text_with({**servers, **changed})
        if new_text != old:
            listed.changes = [connect.Change(path, old, new_text)]
    for link, old_target, new_target in moved_links:
        listed.links_remove.append((link, old_target))
        listed.links_add.append((link, new_target))
    listed.retire = [Path(f) for f in candidate.old_folders if Path(f).is_dir() and Path(f) != folder]
    return connect.merge(plans + [listed])


def plan_all(candidates, agents) -> connect.Plan:
    """Every app's update as one plan (Check for updates): they edit the same
    settings files, so each is planned on top of the ones before."""
    return connect.chain([lambda c=c: plan(c, agents) for c in candidates])


def updated_entries(candidates, agents) -> dict:
    """name -> entry of the servers that start one of these apps' new
    versions now (directly or through a link), to test them after the update."""
    folders = [_real(str(new_folder(c))) for c in candidates]
    sources = [mcp_ctl.raw_entries(a) for a in agents if mcp_ctl.supported(a)] + [mcp_library.load()]
    found = {}
    for raw in sources:
        for name, value in raw.items():
            entry = mcp_ctl.normal(value) if mcp_ctl.NAME_RE.match(name) else None
            if not entry or entry["transport"] != "stdio" or name in found:
                continue
            command = os.path.expanduser(entry["command"])
            program = _real(shutil.which(command) or command)
            if any(_inside(program, folder) for folder in folders):
                found[name] = entry
    return found
