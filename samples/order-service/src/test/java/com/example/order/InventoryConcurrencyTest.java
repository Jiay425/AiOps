package com.example.order;

import org.junit.jupiter.api.RepeatedTest;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class InventoryConcurrencyTest {

    @RepeatedTest(5)
    void concurrentReserveShouldNotOversellSingleStock() throws Exception {
        InventoryRepository repository = new InventoryRepository();
        String skuId = "sku-2001";
        repository.initialize(skuId, 1);
        InventoryService service = new InventoryService(repository);

        int threads = 8;
        CountDownLatch ready = new CountDownLatch(threads);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        AtomicInteger success = new AtomicInteger();
        AtomicInteger rejected = new AtomicInteger();
        ExecutorService executor = Executors.newFixedThreadPool(threads);

        for (int i = 0; i < threads; i++) {
            executor.submit(() -> {
                ready.countDown();
                try {
                    start.await();
                    service.reserve(skuId, 1);
                    success.incrementAndGet();
                } catch (IllegalStateException e) {
                    rejected.incrementAndGet();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    done.countDown();
                }
            });
        }

        assertTrue(ready.await(5, TimeUnit.SECONDS));
        start.countDown();
        assertTrue(done.await(10, TimeUnit.SECONDS));
        executor.shutdownNow();

        assertEquals(1, success.get());
        assertEquals(threads - 1, rejected.get());
        assertEquals(0, service.stockOf(skuId));
        assertTrue(service.stockOf(skuId) >= 0);
    }
}
