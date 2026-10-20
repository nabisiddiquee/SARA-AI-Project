from __future__ import annotations


def notify(title: str, message: str) -> bool:
    """
    Windows toast notification.

    Optional dependency:
      python -m pip install -e ".[notify]"
    """
    try:
        from winotify import Notification  # type: ignore
    except Exception:
        return False

    try:
        toast = Notification(app_id="my_python_project", title=title, msg=message)
        toast.show()
        return True
    except Exception:
        return False
