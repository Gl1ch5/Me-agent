"""Promo recording only (loaded via PYTHONPATH): send Groq transcription calls to the local fake transcriber."""
import os
import urllib.request as _r

_orig = _r.urlopen
_GROQ = "https://api.groq.com"


def _urlopen(url, *a, **k):
    base = os.environ.get("PROMO_GROQ")
    if base:
        if isinstance(url, _r.Request) and url.full_url.startswith(_GROQ):
            url.full_url = base + url.full_url[len(_GROQ):]
        elif isinstance(url, str) and url.startswith(_GROQ):
            url = base + url[len(_GROQ):]
    return _orig(url, *a, **k)


_r.urlopen = _urlopen
