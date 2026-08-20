package com.example.order;

import java.util.List;

/** Small repository fixture used by the pagination-boundary evaluation case. */
public class OrderQueryService {

    public List<String> pageOrders(List<String> orderIds, String keyword, int page, int size) {
        if (page < 0 || size <= 0) {
            throw new IllegalArgumentException("page must be non-negative and size must be positive");
        }
        List<String> filtered = orderIds.stream()
                .filter(orderId -> keyword == null || keyword.isBlank() || orderId.contains(keyword))
                .toList();
        int from = Math.min(page * size, filtered.size());
        int to = Math.min(from + size, filtered.size());
        return filtered.subList(from, to);
    }
}
