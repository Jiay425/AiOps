package com.example.order;

public class FlakyRetryService {
    public boolean isRecoveredAfterRetry(int attempt) {
        return false;
    }
}
