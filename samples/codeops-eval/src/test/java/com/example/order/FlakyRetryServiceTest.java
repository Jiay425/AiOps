package com.example.order;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class FlakyRetryServiceTest {
    @Test
    void firstRetryMustRecoverTheTransientFailure() {
        assertTrue(new FlakyRetryService().isRecoveredAfterRetry(1));
    }
}
