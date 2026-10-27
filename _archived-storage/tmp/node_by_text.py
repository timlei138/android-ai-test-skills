import re
import sys
from pathlib import Path

ev = Path(sys.argv[1])
xml = (ev / "dump.xml").read_text(encoding="utf-8", errors="replace")
nodes = re.findall(r"<node[^>]*>", xml)


def g(s, k):
    m = re.search(k + r'="([^"]*)"', s)
    return m.group(1) if m else ""


want = sys.argv[2] if len(sys.argv) > 2 else ""
for s in nodes:
    t = g(s, "text")
    if want and t != want:
        continue
    print(repr(t), "| rid=", g(s, "resource-id").split("/")[-1],
          "| class=", g(s, "class"),
          "| clickable=", g(s, "clickable"),
          "| enabled=", g(s, "enabled"),
          "| desc=", g(s, "content-desc"),
          "| bounds=", g(s, "bounds"))
