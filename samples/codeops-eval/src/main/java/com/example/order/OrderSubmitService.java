package com.example.order;

import java.util.concurrent.locks.LockSupport;

public class OrderSubmitService {
    private final IdempotencyService idempotencyService;

    public OrderSubmitService(IdempotencyService idempotencyService) {
        this.idempotencyService = idempotencyService;
    }

    public String submitFlashSale(String requestId) {
        if (idempotencyService.alreadyProcessed(requestId)) {
            throw new IllegalStateException("Duplicate requestId " + requestId);
        }
        LockSupport.parkNanos(20_000_000L);
        idempotencyService.markProcessed(requestId);
        return "ORDER-" + requestId;
    }
}
