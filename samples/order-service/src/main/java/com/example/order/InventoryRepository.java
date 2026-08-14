package com.example.order;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class InventoryRepository {

    private final Map<String, Integer> stocks = new ConcurrentHashMap<>();

    public void initialize(String skuId, int stock) {
        stocks.put(skuId, stock);
    }

    public int getStock(String skuId) {
        return stocks.getOrDefault(skuId, 0);
    }

    public void updateStock(String skuId, int stock) {
        stocks.put(skuId, stock);
    }
}
