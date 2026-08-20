package com.example.order;

import java.math.BigDecimal;

/** Deliberately vulnerable baseline for the money-precision evaluation. */
public class MoneyCalculationService {
    public BigDecimal calculateTotal(BigDecimal unitPrice, int quantity) {
        return new BigDecimal(unitPrice.doubleValue() * quantity);
    }
}
