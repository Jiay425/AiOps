package com.example.order;

import java.util.HashSet;
import java.util.Set;

/** Deliberately vulnerable baseline: the individual methods are synchronized, but check-and-mark is split. */
public class IdempotencyService {
    private final Set<String> processed = new HashSet<>();

    public synchronized boolean alreadyProcessed(String requestId) {
        return processed.contains(requestId);
    }

    public synchronized void markProcessed(String requestId) {
        processed.add(requestId);
    }
}
