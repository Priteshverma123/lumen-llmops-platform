"""Lumen instruments itself with OTel too (dogfooding): its own spans can be sent to Lumen."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_done = False


def setup_telemetry(service_name: str, otlp_endpoint: str | None) -> None:
    global _done
    if _done:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    _done = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("lumen")
