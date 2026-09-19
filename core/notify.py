"""Desktop notifications (Windows toast), never blocking the caller."""
from __future__ import annotations

import platform
import threading


def toast(title: str, message: str, seconds: int = 6) -> None:
    def go():
        try:
            if platform.system() == "Windows":
                from win10toast import ToastNotifier
                ToastNotifier().show_toast(f"J.A.R.V.I.S — {title}", message[:240],
                                           duration=seconds, threaded=False)
                return
        except Exception:
            pass
        try:
            from plyer import notification
            notification.notify(title=f"J.A.R.V.I.S — {title}", message=message[:240], timeout=seconds)
        except Exception as e:
            print(f"[Notify] {title}: {message} ({e})")
    threading.Thread(target=go, daemon=True, name="toast").start()
