# Upstream requests

agora is a communication protocol. When a framework cannot yet carry a seat, the
gap is stated in the **contract's** terms (`docs/harness_contract.md`), not in
agora's — and there is ONE request per framework, not one per package it happens
to be built from. Its maintainers should not have to be assembled by hand.

The framework can check itself at any time:

```bash
agora harness-check <harness>          # structural probes, no LLM calls
agora harness-check <harness> --live   # plus one real turn
```

| file | framework | status |
|---|---|---|
| `abstractcode-tui.md` | abstractcode-tui | DRIVABLE WITH LIMITATIONS — `tool-reach` unverifiable statically, `evidence=exit-code-only`, identity is process-scoped |
| `abstractcode-tui-acceptance.sh` | — | runnable acceptance tests for the request above |

Nothing here blocks a seat from working **in-session** today.
