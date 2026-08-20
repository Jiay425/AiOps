package com.example.order;

import java.util.concurrent.locks.LockSupport;

public class CouponRedemptionService {
    private final IdempotencyService idempotencyService;

    public CouponRedemptionService(IdempotencyService idempotencyService) {
        this.idempotencyService = idempotencyService;
    }

    public String redeem(String couponId, String requestId) {
        if (idempotencyService.alreadyProcessed(requestId)) {
            throw new IllegalStateException("Duplicate coupon request " + requestId);
        }
        LockSupport.parkNanos(20_000_000L);
        idempotencyService.markProcessed(requestId);
        return "REDEEMED-" + couponId;
    }
}
