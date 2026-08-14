package com.example.order;
import java.math.BigDecimal;
public class OrderSubmitService {
    private final OrderRepository orderRepository;
    private final InventoryService inventoryService;
    private final IdempotencyService idempotencyService;
    public OrderSubmitService(OrderRepository orderRepository) { this(orderRepository, null, null); }
    public OrderSubmitService(OrderRepository orderRepository, InventoryService inventoryService, IdempotencyService idempotencyService) {
        this.orderRepository = orderRepository; this.inventoryService = inventoryService; this.idempotencyService = idempotencyService;
    }
    public OrderSubmitResponse submit(OrderSubmitRequest request) {
        if (request.getUnitPrice() == null || request.getQuantity() == null) throw new IllegalArgumentException("Unit price and quantity must not be null");
        BigDecimal totalAmount = request.getUnitPrice().multiply(BigDecimal.valueOf(request.getQuantity()));
        String orderId = orderRepository.create(request.getUserId(), request.getSkuId(), request.getQuantity(), totalAmount);
        return new OrderSubmitResponse(orderId, totalAmount);
    }
    public OrderSubmitResponse submitFlashSale(OrderSubmitRequest request) {
        if (request.getRequestId() == null || request.getRequestId().isBlank()) throw new IllegalArgumentException("requestId must not be blank");
        if (request.getUserId() == null || request.getSkuId() == null) throw new IllegalArgumentException("userId and skuId must not be null");
        if (request.getUnitPrice() == null || request.getQuantity() == null) throw new IllegalArgumentException("Unit price and quantity must not be null");
        if (idempotencyService == null) throw new IllegalStateException("IdempotencyService not configured");
        if (inventoryService == null) throw new IllegalStateException("InventoryService not configured");
        if (!idempotencyService.tryMarkProcessed(request.getRequestId())) {
            throw new IllegalStateException("Duplicate requestId " + request.getRequestId());
        }
        inventoryService.reserve(request.getSkuId(), request.getQuantity());
        BigDecimal totalAmount = request.getUnitPrice().multiply(BigDecimal.valueOf(request.getQuantity()));
        try { String orderId = orderRepository.create(request.getUserId(), request.getSkuId(), request.getQuantity(), totalAmount); return new OrderSubmitResponse(orderId, totalAmount); }
        catch (RuntimeException e) { inventoryService.release(request.getSkuId(), request.getQuantity()); throw e; }
    }
}

