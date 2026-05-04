# Cooperation Example

This example shows three stateless workers cooperating through the Quadro Board:
a researcher creates a research result, a small chief policy posts a downstream
draft task, and the draft then follows the built-in review lifecycle. It uses
only Quadro core primitives, with no LLM calls and no API key.

Prerequisites:

```sh
pip install quadro
```

Run it from the repository root:

```sh
python examples/cooperation/main.py
```
