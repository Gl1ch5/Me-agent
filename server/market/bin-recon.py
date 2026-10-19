"""
name: bin-recon
description: first look at an unknown binary without running it: format (PE/ELF/Mach-O/.NET/APK/JAR/Godot pck), architecture, sections with entropy (packed?), imports, interesting strings, file hashes. Use to identify an unknown build, game file or library before modding or debugging it
icon: ⌬
color: cyan
---
Static only: nothing here executes the file. Start with `triage path=<file>`, then `strings` with a filter, `imports` for PE/ELF, `hexdump` to look at a spot.
Read the result as hypotheses: a high-entropy section suggests compressed or encrypted data.
"""
import hashlib, math, os, re, struct, zipfile
from pathlib import Path


def _entropy(b):
    if not b:
        return 0.0
    counts = [0] * 256
    for x in b:
        counts[x] += 1
    n = len(b)
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def _pe(data):
    out = {}
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[off:off + 4] != b"PE\0\0":
        return {"error": "MZ without PE header (DOS program?)"}
    machine, nsec, ts, _, _, optsize, chars = struct.unpack_from("<HHIIIHH", data, off + 4)
    out["machine"] = {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64", 0x1C4: "arm"}.get(machine, hex(machine))
    out["dll"] = bool(chars & 0x2000)
    opt = off + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    pe32p = magic == 0x20B
    out["subsystem"] = {2: "GUI", 3: "console"}.get(struct.unpack_from("<H", data, opt + 68)[0], "other")
    ddir = opt + (112 if pe32p else 96)
    dirs = [struct.unpack_from("<II", data, ddir + 8 * i) for i in range(16)]
    secs = []
    for i in range(nsec):
        s = opt + optsize + 40 * i
        name = data[s:s + 8].rstrip(b"\0").decode("latin1")
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, s + 8)
        secs.append({"name": name, "va": hex(va), "size": rsize, "entropy": round(_entropy(data[raw:raw + rsize]), 2), "_va": va, "_raw": raw, "_vs": max(vsize, rsize)})
    out["sections"] = secs

    def rva(r):
        for s in secs:
            if s["_va"] <= r < s["_va"] + s["_vs"]:
                return r - s["_va"] + s["_raw"]
        return None
    out["dotnet"] = dirs[14][0] != 0
    imports = {}
    p = rva(dirs[1][0]) if dirs[1][0] else None
    while p is not None and p + 20 <= len(data):
        oft, _, _, name_rva, ft = struct.unpack_from("<IIIII", data, p)
        if not name_rva:
            break
        n = rva(name_rva)
        dll = data[n:data.index(b"\0", n)].decode("latin1") if n is not None else "?"
        fns, t = [], rva(oft or ft)
        step, flag = (8, 1 << 63) if pe32p else (4, 1 << 31)
        while t is not None and len(fns) < 400:
            v = struct.unpack_from("<Q" if pe32p else "<I", data, t)[0]
            if not v:
                break
            if v & flag:
                fns.append(f"#{v & 0xFFFF}")
            else:
                h = rva(v & 0x7FFFFFFF)
                fns.append(data[h + 2:data.index(b"\0", h + 2)].decode("latin1") if h is not None else "?")
            t += step
        imports[dll] = fns
        p += 20
    out["imports"] = imports
    names = " ".join(s["name"] for s in secs).lower()
    hints = []
    if "upx" in names:
        hints.append("UPX packed (try `upx -d` on a copy)")
    if any(x in names for x in (".vmp", ".themida", ".enigma", ".aspack", ".petite")):
        hints.append("commercial protector section names")
    if any(s["entropy"] > 7.2 and s["size"] > 4096 for s in secs):
        hints.append("high-entropy section: packed or encrypted data")
    if out["dotnet"]:
        hints.append(".NET assembly: decompile with ILSpy/ilspycmd or dnSpyEx, not a native disassembler")
    if sum(len(v) for v in imports.values()) < 10 and not out["dotnet"]:
        hints.append("very few imports: likely packed or resolves APIs at runtime")
    out["hints"] = hints
    return out


def _elf(data):
    cls, endian = data[4], data[5]
    e = "<" if endian == 1 else ">"
    etype, machine = struct.unpack_from(e + "HH", data, 16)
    out = {"bits": 64 if cls == 2 else 32, "type": {1: "relocatable", 2: "executable", 3: "shared/PIE", 4: "core"}.get(etype, etype),
           "machine": {3: "x86", 62: "x86-64", 40: "arm", 183: "aarch64", 243: "riscv", 8: "mips"}.get(machine, machine)}
    if cls == 2:
        shoff, = struct.unpack_from(e + "Q", data, 40)
        shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", data, 58)
    else:
        shoff, = struct.unpack_from(e + "I", data, 32)
        shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", data, 46)
    secs = []
    hdr = []
    for i in range(min(shnum, 200)):
        b = shoff + i * shentsize
        if cls == 2:
            name, typ, _, _, off, size = struct.unpack_from(e + "IIQQQQ", data, b)
        else:
            name, typ, _, _, off, size = struct.unpack_from(e + "IIIIII", data, b)
        hdr.append((name, typ, off, size))
    if hdr and shstrndx < len(hdr):
        so = hdr[shstrndx][2]
        for name, typ, off, size in hdr:
            n = data[so + name:data.find(b"\0", so + name)].decode("latin1")
            if n:
                secs.append({"name": n, "size": size, "entropy": round(_entropy(data[off:off + size]), 2) if typ != 8 else 0})
    out["sections"] = secs
    names = {s["name"] for s in secs}
    out["stripped"] = ".symtab" not in names
    out["hints"] = (["no section headers: stripped hard or packed"] if not secs else []) + \
        (["UPX packed"] if b"UPX!" in data[:4096] else []) + (["Go binary (use GoReSym / redress)"] if ".gopclntab" in names or b"Go build ID" in data[:65536] else []) + \
        (["Rust binary"] if b"rustc" in data or b"/rustc/" in data else [])
    return out


def _kind(data, p):
    if data[:2] == b"MZ":
        return "pe"
    if data[:4] == b"\x7fELF":
        return "elf"
    if data[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return "macho"
    if data[:4] == b"GDPC":
        return "godot-pck"
    if data[:4] == b"dex\n":
        return "dex"
    if data[:4] == b"PK\x03\x04":
        try:
            names = zipfile.ZipFile(p).namelist()
        except zipfile.BadZipFile:
            return "zip"
        if "AndroidManifest.xml" in names:
            return "apk"
        if any(n.endswith(".class") for n in names):
            return "jar"
        return "zip"
    if data[:4] == b"\0asm":
        return "wasm"
    return "unknown"


@action
def triage(ctx, path: str):
    "Format, architecture, hashes, sections with entropy, packer/compiler hints. Reads the file only."
    p = Path(path).expanduser()
    data = p.read_bytes()
    ctx.status(f"{p.name}: {len(data)} bytes")
    k = _kind(data, p)
    info = {"file": str(p), "size": len(data), "format": k, "md5": hashlib.md5(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(), "entropy": round(_entropy(data), 2)}
    try:
        if k == "pe":
            pe = _pe(data)
            imps = pe.pop("imports", {})
            info.update(pe)
            info["imports"] = {d: len(f) for d, f in imps.items()}
        elif k == "elf":
            info.update(_elf(data))
        elif k == "godot-pck":
            ver = struct.unpack_from("<IIII", data, 4)
            info["hints"] = [f"Godot pack format {ver[0]}, engine {ver[1]}.{ver[2]}.{ver[3]}: open with GDRE Tools (gdsdecomp)"]
        elif k == "apk":
            info["hints"] = ["Android app: jadx -d out app.apk (Java/Kotlin), apktool d for resources/smali; native libs in lib/<abi>/*.so"]
        elif k == "jar":
            info["hints"] = ["Java archive: decompile with CFR, Vineflower or jadx"]
        elif k == "macho":
            info["hints"] = ["Mach-O: otool -L / nm / Ghidra; codesign -dv for signing info"]
    except (struct.error, ValueError, IndexError) as e:
        info["parse_error"] = f"{type(e).__name__}: {e} (truncated or malformed headers)"
    secs = info.pop("sections", None)
    if secs:
        ctx.table([{k2: v for k2, v in s.items() if not k2.startswith("_")} for s in secs], "sections")
    lines = [f"{k2}: {v}" for k2, v in info.items() if k2 != "hints"]
    for h in info.get("hints") or []:
        lines.append(f"hint: {h}")
    if secs:
        lines.append("sections: " + ", ".join(f"{s['name']}({s['size']}, H={s['entropy']})" for s in secs[:30]))
    return "\n".join(lines)


@action
def imports(ctx, path: str, dll: str = ""):
    "Imported functions of a PE file (optionally one DLL). Suspicious APIs are flagged."
    data = Path(path).expanduser().read_bytes()
    if data[:2] != b"MZ":
        return "imports works on PE files; for ELF use `nm -D` or `readelf -Ws --dyn-syms`"
    imps = _pe(data).get("imports", {})
    flag = re.compile(r"VirtualAlloc|WriteProcessMemory|CreateRemoteThread|LoadLibrary|GetProcAddress|IsDebuggerPresent|"
                      r"CheckRemoteDebugger|NtQueryInformationProcess|SetWindowsHookEx|InternetOpen|URLDownload|WinHttp|Crypt|RegSetValue|"
                      r"ShellExecute|CreateProcess|AdjustTokenPrivileges", re.I)
    out = []
    for d, fns in imps.items():
        if dll and dll.lower() not in d.lower():
            continue
        out.append(f"{d} ({len(fns)}): " + ", ".join(f + (" [!]" if flag.search(f) else "") for f in fns))
    return "\n".join(out) or "no imports found"


@action
def strings(ctx, path: str, min_len: int = 6, filter: str = "", limit: int = 300):
    "Printable ASCII and UTF-16LE strings with offsets; filter is a regex (e.g. 'http|\\.dll|password|version')."
    data = Path(path).expanduser().read_bytes()
    n = int(min_len)
    rx = re.compile(filter, re.I) if filter else None
    found = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % n, data):
        found.append((m.start(), m.group().decode()))
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % n, data):
        found.append((m.start(), m.group().decode("utf-16le") + "  (utf16)"))
    found.sort()
    hits = [f"{o:#08x}  {s[:200]}" for o, s in found if not rx or rx.search(s)]
    return f"{len(hits)} strings" + (f" matching /{filter}/" if filter else "") + ":\n" + "\n".join(hits[:int(limit)])


@action
def hexdump(ctx, path: str, offset: str = "0", length: int = 256):
    "Hex + ASCII view of length bytes at offset (decimal or 0x hex)."
    off = int(offset, 0)
    with open(Path(path).expanduser(), "rb") as f:
        f.seek(off)
        b = f.read(min(int(length), 4096))
    rows = []
    for i in range(0, len(b), 16):
        chunk = b[i:i + 16]
        rows.append(f"{off + i:08x}  {chunk.hex(' '):<47}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)}")
    return "\n".join(rows)


@action
def find_bytes(ctx, path: str, pattern: str, limit: int = 50):
    "Find a byte pattern (hex with ?? wildcards, e.g. '48 8B 05 ?? ?? ?? ?? C3') or a quoted text; returns file offsets."
    data = Path(path).expanduser().read_bytes()
    if pattern[:1] in "\"'":
        rx = re.compile(re.escape(pattern.strip("\"'").encode()))
    else:
        rx = re.compile(b"".join(b"." if t == "??" else re.escape(bytes([int(t, 16)])) for t in pattern.split()), re.S)
    hits = [f"{m.start():#x}" for m in rx.finditer(data)][:int(limit)]
    return f"{len(hits)} hit(s): " + ", ".join(hits) if hits else "not found"
