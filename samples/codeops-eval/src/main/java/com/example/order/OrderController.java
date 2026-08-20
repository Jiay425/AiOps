package com.example.order;

public class OrderController {
    public String submitHttp(OrderSubmitRequest request) {
        request.userId().trim();
        return "201 CREATED";
    }
}
