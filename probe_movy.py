import os, pathlib

t = pathlib.Path(os.path.join(os.environ["TEMP"], "movy_full.py")).read_text(encoding="utf-8")
i = t.find("resolve_movy_streams")
print(t[i:i + 6000])
