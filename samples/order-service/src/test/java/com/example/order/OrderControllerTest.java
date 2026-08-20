package com.example.order;

import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

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

    @Test
    void submitShouldRejectNullUserId() {
        OrderSubmitRequest request = new OrderSubmitRequest(null, "sku-2001", 2, new BigDecimal("19.90"));

        assertThrows(IllegalArgumentException.class, () -> orderController.submit(request));
    }

    @Test
    void submitShouldRejectBlankUserId() {
        OrderSubmitRequest request = new OrderSubmitRequest(" ", "sku-2001", 2, new BigDecimal("19.90"));

        assertThrows(IllegalArgumentException.class, () -> orderController.submit(request));
    }

    @Test
    void submitShouldRejectZeroQuantity() {
        OrderSubmitRequest request = new OrderSubmitRequest("u-1001", "sku-2001", 0, new BigDecimal("19.90"));

        assertThrows(IllegalArgumentException.class, () -> orderController.submit(request));
    }

    @Test
    void submitShouldRejectNegativeQuantity() {
        OrderSubmitRequest request = new OrderSubmitRequest("u-1001", "sku-2001", -1, new BigDecimal("19.90"));

        assertThrows(IllegalArgumentException.class, () -> orderController.submit(request));
    }

    @Test
    void submitShouldRejectZeroUnitPrice() {
        OrderSubmitRequest request = new OrderSubmitRequest("u-1001", "sku-2001", 2, new BigDecimal("0"));

        assertThrows(IllegalArgumentException.class, () -> orderController.submit(request));
    }

    @Test
    void submitShouldRejectNegativeUnitPrice() {
        OrderSubmitRequest request = new OrderSubmitRequest("u-1001", "sku-2001", 2, new BigDecimal("-1"));

        assertThrows(IllegalArgumentException.class, () -> orderController.submit(request));
    }
}
