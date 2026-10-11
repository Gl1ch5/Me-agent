"""
name: sysinfo
description: machine overview — OS, CPU, RAM, disk, top processes, ports
icon: ▤
color: green
---
Use for "what's eating my disk/RAM", environment questions.
"""
import os, platform, shutil, subprocess


@action
def overview(ctx):
    "OS, CPU, memory, disk"
    du = shutil.disk_usage(os.path.expanduser("~"))
    info = {"os": f"{platform.system()} {platform.release()}", "python": platform.python_version(), "cpus": os.cpu_count(),
            "disk_free_gb": round(du.free / 1e9, 1), "disk_total_gb": round(du.total / 1e9, 1)}
    if os.path.exists("/proc/meminfo"):
        m = dict(l.split(":") for l in open("/proc/meminfo").read().splitlines()[:3])
        info["mem"] = {k: v.strip() for k, v in m.items()}
    return info


@action
def biggest(ctx, path=".", top=15):
    "Largest files under a path"
    files = []
    walk = list(os.walk(path))
    for d, _, fs in ctx.progress(walk, "scanning"):
        for f in fs:
            p = os.path.join(d, f)
            try:
                files.append((os.path.getsize(p), p))
            except OSError:
                pass
    return "\n".join(f"{s / 1e6:9.1f} MB  {p}" for s, p in sorted(files, reverse=True)[:top])


@action
def ports(ctx):
    "Listening ports"
    cmd = "netstat -ano | findstr LISTENING" if os.name == "nt" else "ss -ltnp 2>/dev/null || lsof -iTCP -sTCP:LISTEN -n -P"
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout[-6000:]
