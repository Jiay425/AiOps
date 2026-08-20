package com.example.order;

public class OrderStateService {
    public enum State { CREATED, PAID, CANCELLED }

    private State state = State.CREATED;

    public void transition(State next) {
        state = next;
    }

    public State currentState() {
        return state;
    }
}
