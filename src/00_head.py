#!/usr/bin/env python3
"""me — a minimal, cloud-synced coding agent in one file.

Layers (top → bottom), each usable on its own:
  Frontends : TUI (default) · headless `-p` · JSON-lines `--rpc`
  Agent     : model ⇄ tools loop, emits events (dicts) to a frontend
  Provider  : any OpenAI-compatible API (chat/completions or responses)
  Tools     : read write edit bash ls grep skill
  Skills    : single .py/.md files; .py actions run in a subprocess and
              stream progress/log events back to the frontend
  Store     : ~/.me (config, skills, sessions)  ⇄  Cloud sync
Stdlib only. Python 3.8+.
"""
import ast, atexit, base64, collections, difflib, fnmatch, glob, hashlib, http.client, io, json, os, platform, queue, re
import shlex, shutil, signal, stat, subprocess, sys, tempfile, textwrap, threading, time, traceback, unicodedata
import urllib.error, urllib.request, uuid, zipfile, zlib
from pathlib import Path

VERSION = "0.13.0"
SERVER = os.environ.get("ME_SERVER", "https://me-agent.ru")
HOME = Path(os.environ.get("ME_HOME", Path.home() / ".me"))
WIN = os.name == "nt"
# Windows: children get their own hidden console, so they can neither read our keystrokes nor change our console modes
NO_WINDOW = {"creationflags": 0x08000000} if WIN else {}
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
