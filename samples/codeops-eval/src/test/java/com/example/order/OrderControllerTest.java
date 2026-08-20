package com.example.order;

import static org.junit.jupiter.api.Assertions.assertThrows;

import java.math.BigDecimal;
import org.junit.jupiter.api.Test;

class OrderControllerTest {
    @Test
    void rejectsMissingUserAndInvalidAmountsBeforeSubmitting() {
        OrderController controller = new OrderController();
        assertThrows(IllegalArgumentException.class, () -> controller.submitHttp(new OrderSubmitRequest(null, 1, BigDecimal.ONE)));
        assertThrows(IllegalArgumentException.class, () -> controller.submitHttp(new OrderSubmitRequest("u-1", 0, BigDecimal.ONE)));
        assertThrows(IllegalArgumentException.class, () -> controller.submitHttp(new OrderSubmitRequest("u-1", 1, BigDecimal.ZERO)));
    }
}
