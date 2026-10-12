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
import ast, base64, difflib, fnmatch, glob, hashlib, io, json, os, platform, queue, re
import shutil, signal, subprocess, sys, tempfile, textwrap, threading, time, unicodedata
import urllib.error, urllib.request, uuid, zipfile, zlib
from pathlib import Path

VERSION = "0.5.6"
SERVER = os.environ.get("ME_SERVER", "https://me-agent.ru")
HOME = Path(os.environ.get("ME_HOME", Path.home() / ".me"))
WIN = os.name == "nt"
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
