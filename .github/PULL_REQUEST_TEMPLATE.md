## What this PR does

## How to test
1. `pytest` — all tests pass
2. (any additional manual verification)

## Checklist
- [ ] All tests pass (`pytest`)
- [ ] No new dependencies added to core (`pyproject.toml [project].dependencies` is still empty)
- [ ] A2A boundary not bypassed (no direct method calls between board, chief, workers)
- [ ] If new event type: discussed in an issue first (frozen taxonomy)
