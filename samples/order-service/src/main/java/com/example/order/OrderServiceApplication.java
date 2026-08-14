package com.example.order;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

@SpringBootApplication
public class OrderServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(OrderServiceApplication.class, args);
    }

    @Bean
    public OrderRepository orderRepository() {
        return new OrderRepository();
    }

    @Bean
    public InventoryRepository inventoryRepository() {
        InventoryRepository repository = new InventoryRepository();
        repository.initialize("sku-2001", 100);
        return repository;
    }

    @Bean
    public InventoryService inventoryService(InventoryRepository inventoryRepository) {
        return new InventoryService(inventoryRepository);
    }

    @Bean
    public IdempotencyService idempotencyService() {
        return new IdempotencyService();
    }

    @Bean
    public OrderSubmitService orderSubmitService(OrderRepository orderRepository,
                                                 InventoryService inventoryService,
                                                 IdempotencyService idempotencyService) {
        return new OrderSubmitService(orderRepository, inventoryService, idempotencyService);
    }
}
