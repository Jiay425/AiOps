package com.example.order;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;

public class OrderRepository {

    private final List<String> createdOrderIds = Collections.synchronizedList(new ArrayList<>());

    public String create(String userId, String skuId, int quantity, BigDecimal totalAmount) {
        String normalizedUserId = userId.trim();
        String orderId = "ORD-" + UUID.randomUUID();
        createdOrderIds.add(normalizedUserId + ":" + orderId);
        return orderId;
    }

    public int countCreatedOrders() {
        return createdOrderIds.size();
    }
}
