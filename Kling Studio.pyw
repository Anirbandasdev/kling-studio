"""Double-click to start Kling Studio (no console window)."""

import traceback

try:
    import app
    app.main()
except Exception:
    import tkinter.messagebox
    tkinter.messagebox.showerror("Kling Studio couldn't start", traceback.format_exc()[-2000:])
