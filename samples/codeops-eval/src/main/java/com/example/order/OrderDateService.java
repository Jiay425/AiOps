package com.example.order;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.time.ZoneId;

public class OrderDateService {
    public LocalDate toOrderDate(Instant createdAt, ZoneId customerZone) {
        return createdAt.atZone(ZoneOffset.UTC).toLocalDate();
    }
}
