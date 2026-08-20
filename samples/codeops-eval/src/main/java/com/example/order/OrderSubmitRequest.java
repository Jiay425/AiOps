package com.example.order;

import java.math.BigDecimal;

public record OrderSubmitRequest(String userId, int quantity, BigDecimal unitPrice) {
    public void validate() {
        // Baseline intentionally has no validation contract.
    }
}
