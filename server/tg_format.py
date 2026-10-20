"""Telegram bot: Markdown for rich messages and the live progress view of a running task."""


def md_chunks(text, n):
    """Split long Markdown at paragraph breaks into parts of at most n characters (a rich message holds 32768)."""
    out, cur = [], ""
    for para in (text or "…").split("\n\n"):
        while len(para) > n:
            out, cur, para = out + ([cur] if cur else []) + [para[:n]], "", para[n:]
        if len(cur) + len(para) + 2 > n:
            out.append(cur)
            cur = ""
        cur = cur + "\n\n" + para if cur else para
    return out + [cur] if cur else out


def close_md(md):
    """A streamed answer cut mid code block: close the fence so the partial Markdown still renders."""
    return md + "\n```" if md.count("```") % 2 else md


def code(s):
    return "`" + str(s).replace("`", "'") + "`"


def steps_md(steps):
    """The tools a turn ran, folded into one collapsible block above the answer."""
    if not steps:
        return ""
    return (f"<details><summary>{len(steps)} {'step' if len(steps) == 1 else 'steps'}</summary>\n\n"
            + "\n".join("- " + code(s) for s in steps[-40:]) + "\n\n</details>\n\n")


def progress_md(steps, answer, elapsed):
    """What a running task shows: the latest steps, then the answer as it streams, then the clock."""
    head = "\n".join("> " + code(s) for s in steps[-4:]) if steps and not answer else ""
    return close_md("\n\n".join(x for x in (head, answer.strip(), f"_working… {elapsed:.0f}s_") if x))
