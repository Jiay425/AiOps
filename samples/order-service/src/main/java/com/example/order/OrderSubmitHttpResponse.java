package com.example.order;

public class OrderSubmitHttpResponse {

    private final int statusCode;

    private final OrderSubmitResponse body;

    private final String errorMessage;

    private OrderSubmitHttpResponse(int statusCode, OrderSubmitResponse body, String errorMessage) {
        this.statusCode = statusCode;
        this.body = body;
        this.errorMessage = errorMessage;
    }

    public static OrderSubmitHttpResponse ok(OrderSubmitResponse body) {
        return new OrderSubmitHttpResponse(200, body, "");
    }

    public static OrderSubmitHttpResponse badRequest(String errorMessage) {
        return new OrderSubmitHttpResponse(400, null, errorMessage);
    }

    public int getStatusCode() {
        return statusCode;
    }

    public OrderSubmitResponse getBody() {
        return body;
    }

    public String getErrorMessage() {
        return errorMessage;
    }
}
