package com.example.order;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.TimeZone;
import org.junit.jupiter.api.Test;

class OrderDateServiceTest {

    @Test
    void convertsInstantUsingCustomerTimezone() {
        OrderDateService service = new OrderDateService();
        assertEquals(LocalDate.of(2026, 1, 1),
                service.toOrderDate(Instant.parse("2025-12-31T23:30:00Z"), ZoneId.of("Asia/Shanghai")));
    }

    @Test
    void usesCustomerZoneNotSystemDefault() {
        TimeZone originalDefault = TimeZone.getDefault();
        try {
            TimeZone.setDefault(TimeZone.getTimeZone("UTC"));
            OrderDateService service = new OrderDateService();
            assertEquals(LocalDate.of(2026, 1, 1),
                    service.toOrderDate(Instant.parse("2025-12-31T23:30:00Z"), ZoneId.of("Asia/Shanghai")));
        } finally {
            TimeZone.setDefault(originalDefault);
        }
    }

    @Test
    void convertsInstantUsingNegativeUtcOffset() {
        OrderDateService service = new OrderDateService();
        assertEquals(LocalDate.of(2025, 12, 31),
                service.toOrderDate(Instant.parse("2026-01-01T00:30:00Z"), ZoneId.of("America/Los_Angeles")));
    }
}

