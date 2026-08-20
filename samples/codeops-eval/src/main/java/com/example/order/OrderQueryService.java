package com.example.order;

import java.util.List;

public class OrderQueryService {
    public List<String> pageOrders(List<String> orderIds, String keyword, int page, int size) {
        List<String> filtered = orderIds.stream().filter(id -> keyword == null || id.contains(keyword)).toList();
        int from = page * size;
        return filtered.subList(from, from + size);
    }
}
