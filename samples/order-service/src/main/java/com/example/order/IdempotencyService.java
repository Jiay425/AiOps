package com.example.order;
import java.util.HashSet;
import java.util.Set;
public class IdempotencyService {
    private final Set<String> processedRequestIds = new HashSet<>();
    public synchronized boolean alreadyProcessed(String requestId) { return processedRequestIds.contains(requestId); }
    public synchronized void markProcessed(String requestId) { processedRequestIds.add(requestId); }
    public synchronized boolean tryMarkProcessed(String requestId) {
        if (processedRequestIds.contains(requestId)) {
            return false;
        }
        processedRequestIds.add(requestId);
        return true;
    }
}
