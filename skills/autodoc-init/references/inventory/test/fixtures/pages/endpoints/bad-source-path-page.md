---
type: endpoint
title: Bad Source Path Page
created: 2026-06-24
updated: 2026-06-24
project: fixture
source_repo: example-org/sample-repo
source_paths:
  - canonical/app.py
  - canonical/deleted.py
status: active
---

# Bad Source Path Page

Has one valid source_path (`canonical/app.py`) and one that points at a file
that does not exist in the repo (`canonical/deleted.py`). Links to
[[Merge Util]] so it is not an orphan.
