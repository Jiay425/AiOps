package com.example.order;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class OrderStateServiceTest {
    @Test
    void paidOrderCannotTransitionBackToCreated() {
        OrderStateService service = new OrderStateService();
        service.transition(OrderStateService.State.PAID);
        assertThrows(IllegalStateException.class, () -> service.transition(OrderStateService.State.CREATED));
    }

    @Test
    void cancelledOrderCannotTransitionToCreated() {
        OrderStateService service = new OrderStateService();
        service.transition(OrderStateService.State.CANCELLED);
        assertThrows(IllegalStateException.class, () -> service.transition(OrderStateService.State.CREATED));
    }

    @Test
    void paidOrderCanTransitionToCancelled() {
        OrderStateService service = new OrderStateService();
        service.transition(OrderStateService.State.PAID);
        service.transition(OrderStateService.State.CANCELLED);
        assertEquals(OrderStateService.State.CANCELLED, service.currentState());
    }
}

