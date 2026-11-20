"""GUI-only state for checked guide reads and saves; no settings or GTK work here."""

READ_ERROR = "Your setup could not be checked. Your last choices are kept."
CHOICE_ERROR = "Your setup choice could not be saved. Try again."
SAVE_ERROR = "Your setup progress could not be saved. Try again."


def valid_steps(steps):
    """Only the checklist shape the native page can render and safely activate."""
    return isinstance(steps, list) and bool(steps) and all(
        isinstance(step, (tuple, list)) and len(step) == 6
        and isinstance(step[0], str) and isinstance(step[1], bool)
        and all(isinstance(step[index], str) for index in (2, 3, 5))
        and (step[4] is None or callable(step[4])) for step in steps)


def merge_changes(previous, changes):
    """Verification adds membership; other fields keep the latest explicit value."""
    merged = {**previous, **changes}
    if isinstance(previous.get("verified"), list) and isinstance(changes.get("verified"), list):
        merged["verified"] = list(dict.fromkeys(previous["verified"] + changes["verified"]))
    return merged


def remaining_retry(previous, changes, saved=None):
    """A new choice supersedes its old value; unrelated failures stay explicitly retryable."""
    remaining = {key: value for key, value in previous.items() if key not in changes or key == "verified"}
    if saved is None:
        return remaining
    for key, value in list(remaining.items()):
        actual = saved.get(key)
        confirmed = (all(command in actual for command in value)
                     if key == "verified" and isinstance(value, list) and isinstance(actual, list)
                     else actual == value)
        if confirmed:
            remaining.pop(key)
    return remaining


class FirstAgentState:
    def __init__(self):
        self.catalog = None
        self.hardware = None
        self.saved = None
        self.loading = False
        self.saving = False
        self.write_pending = False
        self.error = ""
        self.retry_changes = None
        self._read = 0
        self._write = 0

    @property
    def busy(self):
        return self.loading or self.write_pending

    def begin_read(self):
        if self.busy:
            return None
        self._read += 1
        self.loading = True
        self.error = ""
        return self._read

    def read_current(self, token):
        return token == self._read and self.loading

    def read_done(self, token, catalog, hardware, saved):
        if not self.read_current(token):
            return False
        self.catalog, self.hardware, self.saved = catalog, hardware, saved
        self.loading = False
        if self.retry_changes is not None:
            # Another confirmed answer may have added verification meanwhile.
            self.retry_changes = remaining_retry(self.retry_changes, {}, saved) or None
        self.error = self._save_error() if self.retry_changes is not None else ""
        return True

    def read_failed(self, token):
        if not self.read_current(token):
            return False
        self.loading = False
        self.error = self._save_error() if self.retry_changes is not None else READ_ERROR
        return True

    def abandon_read(self):
        self._read += 1
        self.loading = False

    def begin_save(self, changes):
        if self.busy:
            return None
        self._write += 1
        self.saving = self.write_pending = True
        self.retry_changes = dict(changes)
        self.error = ""
        return self._write

    def _save_error(self):
        return CHOICE_ERROR if self.retry_changes is not None and "route" in self.retry_changes else SAVE_ERROR

    def save_timeout(self, token):
        if token != self._write or not self.saving:
            return False
        # A blocked writer may still finish: no second write until it exits.
        self.saving = False
        self.error = self._save_error()
        return True

    def save_done(self, token, saved=None, failed=False):
        if token != self._write or not self.write_pending:
            return False
        self.write_pending = False
        timely = self.saving
        self.saving = False
        if not timely:
            return True
        if failed:
            self.error = self._save_error()
        else:
            self.saved = saved
            self.retry_changes = None
            self.error = ""
        return True
