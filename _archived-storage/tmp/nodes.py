import re
import sys
from pathlib import Path

ev = Path(sys.argv[1])
xml = (ev / "dump.xml").read_text(encoding="utf-8", errors="replace")
nodes = re.findall(r"<node[^>]*>", xml)

def g(s, k):
    m = re.search(k + r'="([^"]*)"', s)
    return m.group(1) if m else ""

mode = sys.argv[2] if len(sys.argv) > 2 else "text"
if mode == "text":
    seen = set()
    for s in nodes:
        t = g(s, "text")
        rid = g(s, "resource-id").split("/")[-1]
        if t and (t, rid) not in seen:
            seen.add((t, rid))
            print(repr(t), "| rid=", rid, "| bounds=", g(s, "bounds"))
else:
    for rid in mode.split(","):
        hits = [n for n in nodes if 'id/%s"' % rid in n]
        if not hits:
            print(rid, "| NOT FOUND")
            continue
        for s in hits:
            print(rid, "| text=", repr(g(s, "text")), "| enabled=", g(s, "enabled"),
                  "| clickable=", g(s, "clickable"), "| checked=", g(s, "checked"),
                  "| selected=", g(s, "selected"), "| bounds=", g(s, "bounds"))
