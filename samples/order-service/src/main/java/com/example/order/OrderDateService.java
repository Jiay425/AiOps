package com.example.order;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;

/** Small repository fixture used by the timezone/date evaluation case. */
public class OrderDateService {

    public LocalDate toOrderDate(Instant createdAt, ZoneId customerZone) {
        if (createdAt == null || customerZone == null) {
            throw new IllegalArgumentException("createdAt and customerZone must not be null");
        }
        return createdAt.atZone(customerZone).toLocalDate();
    }
}
