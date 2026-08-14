package com.example.order;

import java.math.BigDecimal;

public class OrderSubmitResponse {

    private final String orderId;

    private final BigDecimal totalAmount;

    public OrderSubmitResponse(String orderId, BigDecimal totalAmount) {
        this.orderId = orderId;
        this.totalAmount = totalAmount;
    }

    public String getOrderId() {
        return orderId;
    }

    public BigDecimal getTotalAmount() {
        return totalAmount;
    }
}
