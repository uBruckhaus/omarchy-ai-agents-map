"""Return from reviewed GTK setup pages without changing ordinary map navigation."""


def guide_origin(app):
    page_name = getattr(app, "page_name", None)
    page = page_name() if callable(page_name) else None
    if page == "firstagent" or (page == "addons" and getattr(app, "return_page", None) == "firstagent"):
        return "firstagent"
    return None


def resume_guide(app, state):
    resume = getattr(app, "first_resume", None)
    if state and state.get("origin_page") == "firstagent" and callable(resume):
        resume()
        return True
    return False


def abandon_handoffs(app):
    """An explicit Map/Setup choice must not later jump back to the guide."""
    for name in ("installing", "connecting", "models_state"):
        state = getattr(app, name, None)
        if isinstance(state, dict):
            state.pop("origin_page", None)
    if getattr(app, "return_page", None) == "firstagent":
        app.return_page = None


def abandon_read(app):
    abandon = getattr(app, "first_abandon_read", None)
    if callable(abandon):
        abandon()


def defer_return(app, page, state):
    """Finish background work while Settings/Help keeps the foreground.

    Its Back action should return to the guide rather than a completed form.
    Ordinary forms fall back to their existing map/setup view.
    """
    if getattr(app, "return_page", None) == page:
        app.return_page = "firstagent" if state and state.get("origin_page") == "firstagent" else None
