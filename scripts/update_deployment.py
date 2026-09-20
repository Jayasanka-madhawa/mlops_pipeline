import re
import sys
from pathlib import Path

image = sys.argv[1]

if not re.fullmatch(r"scan-quality:ci-\d+-[a-f0-9]{12}", image):
    raise SystemExit(f"Unexpected image reference: {image}")

path = Path("k8s/application.yaml")
original = path.read_text()

updated, count = re.subn(
    r"(?m)^([ \t]*image:[ \t]*)scan-quality:[A-Za-z0-9_.-]+[ \t]*$",
    lambda match: match.group(1) + image,
    original,
)

if count != 2:
    raise SystemExit(
        f"Expected exactly 2 scan-quality image references, found {count}"
    )

path.write_text(updated)
print(f"Updated both containers to {image}")