package com.example.order;

import org.junit.jupiter.api.RepeatedTest;

import java.math.BigDecimal;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OrderSubmitServiceConcurrencyTest {

    @RepeatedTest(5)
    void duplicateFlashSaleRequestShouldCreateOnlyOneOrder() throws Exception {
        String skuId = "sku-2001";
        InventoryRepository inventoryRepository = new InventoryRepository();
        inventoryRepository.initialize(skuId, 10);
        InventoryService inventoryService = new InventoryService(inventoryRepository);
        OrderRepository orderRepository = new OrderRepository();
        IdempotencyService idempotencyService = new IdempotencyService();
        OrderSubmitService service = new OrderSubmitService(orderRepository, inventoryService, idempotencyService);

        int threads = 8;
        CountDownLatch ready = new CountDownLatch(threads);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        AtomicInteger success = new AtomicInteger();
        AtomicInteger duplicate = new AtomicInteger();
        ExecutorService executor = Executors.newFixedThreadPool(threads);

        for (int i = 0; i < threads; i++) {
            int userIndex = i;
            executor.submit(() -> {
                ready.countDown();
                try {
                    start.await();
                    OrderSubmitRequest request = new OrderSubmitRequest(
                            "user-" + userIndex,
                            skuId,
                            "req-duplicate-001",
                            1,
                            new BigDecimal("19.90"));
                    service.submitFlashSale(request);
                    success.incrementAndGet();
                } catch (IllegalStateException e) {
                    if (e.getMessage() != null && e.getMessage().contains("Duplicate requestId")) {
                        duplicate.incrementAndGet();
                    }
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
        assertEquals(threads - 1, duplicate.get());
        assertEquals(1, orderRepository.countCreatedOrders());
        assertEquals(9, inventoryService.stockOf(skuId));
    }
}

