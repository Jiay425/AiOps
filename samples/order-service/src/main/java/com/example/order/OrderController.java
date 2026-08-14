package com.example.order;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class OrderController {

    private static final Logger log = LoggerFactory.getLogger(OrderController.class);

    private final OrderSubmitService orderSubmitService;

    public OrderController(OrderSubmitService orderSubmitService) {
        this.orderSubmitService = orderSubmitService;
    }

    public OrderSubmitResponse submit(OrderSubmitRequest request) {
        return orderSubmitService.submit(request);
    }

    public OrderSubmitHttpResponse submitHttp(OrderSubmitRequest request) {
        try {
            if (request.getRequestId() != null && !request.getRequestId().isBlank()) {
                return OrderSubmitHttpResponse.ok(orderSubmitService.submitFlashSale(request));
            }
            return OrderSubmitHttpResponse.ok(orderSubmitService.submit(request));
        } catch (IllegalArgumentException e) {
            return OrderSubmitHttpResponse.badRequest(e.getMessage());
        } catch (IllegalStateException e) {
            return OrderSubmitHttpResponse.badRequest(e.getMessage());
        }
    }

    @PostMapping("/api/orders/submit")
    public ResponseEntity<OrderSubmitResponse> submitHttpEndpoint(@RequestBody OrderSubmitRequest request) {
        try {
            OrderSubmitHttpResponse response = submitHttp(request);
            if (response.getStatusCode() >= 400) {
                log.warn("order submit rejected. statusCode={}, errorMessage={}",
                        response.getStatusCode(), response.getErrorMessage());
                return ResponseEntity.badRequest().build();
            }
            return ResponseEntity.ok(response.getBody());
        } catch (RuntimeException e) {
            log.error("order submit failed with unexpected runtime exception. endpoint=/api/orders/submit", e);
            throw e;
        }
    }

    @GetMapping("/api/orders/dependency-latency")
    public ResponseEntity<String> simulateDependencyLatency(@RequestParam(defaultValue = "1500") long durationMs)
            throws InterruptedException {
        long safeDurationMs = Math.max(1L, Math.min(durationMs, 5000L));
        Thread.sleep(safeDurationMs);
        log.info("simulated downstream inventory dependency latency. durationMs={}", safeDurationMs);
        return ResponseEntity.ok("dependency latency simulated: " + safeDurationMs + "ms");
    }
}
