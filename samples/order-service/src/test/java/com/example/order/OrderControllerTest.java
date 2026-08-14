package com.example.order;

import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

class OrderControllerTest {

    private final OrderController orderController = new OrderController(
            new OrderSubmitService(new OrderRepository()));

    @Test
    void submitHttpShouldReturnOkForValidRequest() {
        OrderSubmitRequest request = new OrderSubmitRequest("u-1001", "sku-2001", 2, new BigDecimal("19.90"));

        OrderSubmitHttpResponse response = orderController.submitHttp(request);

        assertEquals(200, response.getStatusCode());
        assertNotNull(response.getBody());
        assertEquals(new BigDecimal("39.80"), response.getBody().getTotalAmount());
    }

    @Test
    void submitHttpShouldReturnBadRequestForNullUnitPrice() {
        OrderSubmitRequest request = new OrderSubmitRequest("u-1001", "sku-2001", 2, null);
        OrderSubmitHttpResponse response = orderController.submitHttp(request);

        assertEquals(400, response.getStatusCode());
        assertEquals("Unit price and quantity must not be null", response.getErrorMessage());
    }
}
