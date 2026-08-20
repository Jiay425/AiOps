package com.example.order;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import org.junit.jupiter.api.Test;

class OrderDateServiceTest {
    @Test
    void usesTheCustomerTimezoneAtTheDateBoundary() {
        OrderDateService service = new OrderDateService();
        assertEquals(LocalDate.of(2026, 1, 1),
                service.toOrderDate(Instant.parse("2025-12-31T23:30:00Z"), ZoneId.of("Asia/Shanghai")));
    }
}
