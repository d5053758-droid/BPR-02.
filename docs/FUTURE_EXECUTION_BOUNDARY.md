# Future execution boundary (not enabled)

## Current status

BPR-02 remains a **research-only** project. It does not connect to Pocket Option, MT5 or another broker, and it does not place, modify or close orders. The new `bpr02.execution` module is only a typed boundary for future development:

- `OrderIntent` represents a proposed, explicitly approved action with source, instrument, time, size and expiry metadata.
- `ExecutionAdapter` describes the interface an authorized provider adapter would implement in the future.
- `DisabledExecutionAdapter` is the only implementation included now. It always raises `ExecutionNotEnabledError` and performs no network activity.
- Neither the signal bridge nor the CLI imports or calls the execution module. A signal is not an order and is not approval.

This interface is not a working connector or a promise that any platform permits automated orders.

## Required conditions before any provider adapter is written

1. Obtain current written authorization and official documentation from the provider for the exact API/SDK and intended actions.
2. Define a separate paper/demo adapter first. Keep live credentials and live execution out of the research process.
3. Obtain an approval decision from a trusted human-controlled workflow. A model-generated event, a non-empty `approval_reference`, or a log entry alone is not proof of approval.
4. Enforce independent risk controls outside model reasoning: instrument allowlist, maximum stake, expiry allowlist, maximum open exposure, daily loss limit, stale-data cutoff, market/session checks and a kill switch.
5. Make submissions idempotent. Record an intent identifier before sending; on a timeout with unknown provider state, reconcile before retrying to avoid duplicate orders.
6. Validate provider responses and reconcile accepted, rejected, filled, cancelled and unknown states. Never infer success from an HTTP response alone.
7. Add integration tests against a provider-sanctioned demo environment; first verify data/instrument/time alignment. Require review before enabling any live capability.

## Current API and permissions status

Pocket Option has not been confirmed in this project as offering a documented, authorized order API. See [POCKET_OPTION_CONNECTOR_RESEARCH.md](POCKET_OPTION_CONNECTOR_RESEARCH.md). Do not replace this missing authorization with browser automation, reverse-engineered private endpoints, session cookies, or unofficial libraries.

## Design sequence

```text
closed candle
    -> validation and source identity
    -> research signal (CALL / PUT / NO TRADE)
    -> append-only event log
    -> independent human approval workflow
    -> validated OrderIntent
    -> provider-specific adapter (not implemented)
    -> provider acknowledgement and reconciliation
```

The current implementation stops at research events and manual review. The final two execution steps do not exist in BPR-02. Until an authorized adapter and separate approval/risk controls are implemented and audited, use only historical analysis, replay and manual comparison.
