package com.example.order;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

class CouponRedemptionServiceTest {
    @Test
    void couponIsRedeemedOnceForOneRequest() throws Exception {
        CouponRedemptionService service = new CouponRedemptionService(new IdempotencyService());
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger accepted = new AtomicInteger();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        for (int i = 0; i < 2; i++) {
            pool.submit(() -> { try { start.await(); service.redeem("coupon-1", "req-1"); accepted.incrementAndGet(); }
                catch (IllegalStateException ignored) { } catch (InterruptedException e) { Thread.currentThread().interrupt(); } });
        }
        start.countDown();
        pool.shutdown();
        pool.awaitTermination(5, TimeUnit.SECONDS);
        assertEquals(1, accepted.get());
    }
}
