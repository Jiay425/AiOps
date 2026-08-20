package com.example.order;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import org.junit.jupiter.api.Test;

class OrderQueryServiceTest {

    @Test
    void emptyKeywordAndLastPageRespectBoundaries() {
        OrderQueryService service = new OrderQueryService();
        assertEquals(List.of("o-3"), service.pageOrders(List.of("o-1", "o-2", "o-3"), "", 1, 2));
        assertEquals(List.of(), service.pageOrders(List.of("o-1", "o-2", "o-3"), "", 2, 2));
    }

    @Test
    void outOfRangePageReturnsEmptyList() {
        OrderQueryService service = new OrderQueryService();
        assertEquals(List.of(), service.pageOrders(List.of("o-1", "o-2", "o-3"), "", 3, 2));
    }
}

