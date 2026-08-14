package com.example.order;

import java.math.BigDecimal;

public class OrderSubmitRequest {

    private String userId;

    private String skuId;

    private String requestId;

    private Integer quantity;

    private BigDecimal unitPrice;

    public OrderSubmitRequest() {
    }

    public OrderSubmitRequest(String userId, String skuId, Integer quantity, BigDecimal unitPrice) {
        this(userId, skuId, null, quantity, unitPrice);
    }

    public OrderSubmitRequest(String userId, String skuId, String requestId, Integer quantity, BigDecimal unitPrice) {
        this.userId = userId;
        this.skuId = skuId;
        this.requestId = requestId;
        this.quantity = quantity;
        this.unitPrice = unitPrice;
    }

    public String getUserId() {
        return userId;
    }

    public String getSkuId() {
        return skuId;
    }

    public String getRequestId() {
        return requestId;
    }

    public Integer getQuantity() {
        return quantity;
    }

    public BigDecimal getUnitPrice() {
        return unitPrice;
    }

    public void setUserId(String userId) {
        this.userId = userId;
    }

    public void setSkuId(String skuId) {
        this.skuId = skuId;
    }

    public void setRequestId(String requestId) {
        this.requestId = requestId;
    }

    public void setQuantity(Integer quantity) {
        this.quantity = quantity;
    }

    public void setUnitPrice(BigDecimal unitPrice) {
        this.unitPrice = unitPrice;
    }
}
