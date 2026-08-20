package com.example.order;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.math.BigDecimal;
import org.junit.jupiter.api.Test;

class MoneyCalculationTest {
    @Test
    void decimalTotalsDoNotUseBinaryFloatingPoint() {
        MoneyCalculationService service = new MoneyCalculationService();
        assertEquals(new BigDecimal("0.30"), service.calculateTotal(new BigDecimal("0.10"), 3));
    }
}
