#!/bin/sh
# Double-click to start Kling Studio from source on a Mac (no console window).
# Needs Python 3.9+ with tkinter: python.org Python works; Homebrew needs python-tk.
cd "$(dirname "$0")"
exec python3 "Kling Studio.pyw"
