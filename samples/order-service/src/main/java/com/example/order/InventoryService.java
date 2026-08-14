package com.example.order;
public class InventoryService {
    private final InventoryRepository inventoryRepository;
    public InventoryService(InventoryRepository inventoryRepository) { this.inventoryRepository = inventoryRepository; }
    public synchronized void reserve(String skuId, int quantity) {
        simulateRemoteInventoryLatency();
        int currentStock = inventoryRepository.getStock(skuId);
        if (currentStock < quantity) throw new IllegalStateException("Insufficient stock for sku " + skuId);
        inventoryRepository.updateStock(skuId, currentStock - quantity);
    }
    public synchronized void release(String skuId, int quantity) {
        int currentStock = inventoryRepository.getStock(skuId);
        inventoryRepository.updateStock(skuId, currentStock + quantity);
    }
    public int stockOf(String skuId) { return inventoryRepository.getStock(skuId); }
    private void simulateRemoteInventoryLatency() { try { Thread.sleep(5); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } }
}
