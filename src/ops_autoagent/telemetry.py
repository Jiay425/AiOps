"""OpenTelemetry wiring for the production control plane.

The application already keeps domain-level task/event metrics in MySQL.  This
module adds transport-level traces for FastAPI and LangGraph-facing requests so
that SkyWalking can correlate HTTP latency with those durable records.  It is
intentionally opt-in: a developer's local run never attempts an OTLP network
connection unless configured explicitly.
"""

from __future__ import annotations

from typing import Any


def configure_telemetry(app: Any, settings: Any) -> Any | None:
    """Instrument ``app`` and return its tracer provider, or ``None`` when disabled."""
    if not bool(getattr(settings, "ops_otel_enabled", False)):
        return None
    endpoint = str(getattr(settings, "ops_otel_exporter_otlp_endpoint", "") or "").strip()
    if not endpoint:
        raise RuntimeError("OPS_OTEL_ENABLED requires OPS_OTEL_EXPORTER_OTLP_ENDPOINT")

    # Avoid double instrumentation during reloads or lifespan re-entry.
    existing = getattr(app.state, "ops_telemetry_provider", None)
    if existing is not None:
        return existing

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        "service.name": str(getattr(settings, "ops_otel_service_name", "ops-autoagent")),
        "service.version": "2.0.0",
        "deployment.environment": str(getattr(settings, "ops_runtime_mode", "demo")),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    # FastAPI receives this provider explicitly below.  Registering it as the
    # process provider also covers repository/tool instrumentation that gets a
    # tracer through the standard OpenTelemetry API.
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider,
                                       excluded_urls="actuator/health,actuator/prometheus")
    app.state.ops_telemetry_provider = provider
    return provider


def shutdown_telemetry(app: Any) -> None:
    """Flush exporter buffers without mutating domain data or user requests."""
    provider = getattr(app.state, "ops_telemetry_provider", None)
    if provider is not None:
        provider.shutdown()
        app.state.ops_telemetry_provider = None
