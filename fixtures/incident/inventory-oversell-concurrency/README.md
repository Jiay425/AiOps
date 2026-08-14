# inventory oversell concurrency incident fixture

This fixture describes a higher-fidelity Incident-to-Fix scenario for the CodeOps agent chain.

## Incident

- Service: `order-service`
- Endpoint: `POST /api/orders/submit`
- Symptom: flash sale traffic creates negative stock and duplicate orders.
- Primary signals:
  - `inventory_negative_stock_total` increases.
  - Duplicate `requestId` values are processed more than once.
  - Trace samples point to `InventoryService.reserve` and `OrderSubmitService.submitFlashSale`.

## Intended bug

The sample code intentionally contains two production-style defects:

1. `InventoryService.reserve` performs a non-atomic check-then-update:
   `read stock -> compare -> sleep -> write stock`.
   Concurrent requests can all read the same stock value and oversell inventory.
2. `IdempotencyService` checks and marks duplicate request IDs in separate calls backed by a plain `HashSet`.
   Concurrent duplicate requests can both pass the check and create duplicate orders.

## Expected agent path

1. Ops evidence analysis should identify negative stock, duplicate request handling and flash sale traffic.
2. Code localization should map evidence to `InventoryService`, `InventoryRepository`, `IdempotencyService` and `OrderSubmitService.submitFlashSale`.
3. Patch generation should propose an atomic inventory reservation strategy and atomic idempotency claim.
4. Test verification should add or run concurrent tests that assert:
   - stock never becomes negative;
   - successful orders never exceed initial stock;
   - the same `requestId` can create at most one order.
5. Release risk analysis should call out lock contention, order success rate, 5xx/409 rate, 400/409 behavior and rollback.
